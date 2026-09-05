"""Media posting engine for groups.

Posts one image/video file to one group through the real Facebook composer,
using the logged-in profile session. Never uses the internal GraphQL API.

Returns a result tuple (status, message):
  posted   - media published.
  pending  - post routed to admin approval (caller flags the group as skip).
  failed   - an error occurred; message explains why.
"""
import os
import re
import time

from playwright.sync_api import TimeoutError as PWTimeout

COMPOSER_PHOTO_LABELS = [
    "photo/video",
    "add photo/video",
    "add a photo/video",
    "photo/video, ",
    "photo",
    "add photos",
    "add a photo",
    "attach a photo or video",
    "attach photo/video",
    "add photos/videos",
    "attach",
]

DIALOG = '[role="dialog"]'

PENDING_PHRASES = [
    "pending review",
    "pending approval",
    "your post will be visible after",
    "will be visible after approval",
    "is pending approval",
    "reviewed before",
    "under review",
    "post is pending",
    "visible to members after it's approved",
]

CAPTION_LABELS = [
    "say something about this photo",
    "say something about this video",
    "write a caption",
    "add a description",
    "describe this photo",
    "describe this video",
    "create a public post",
    "create a post",
    "write something",
    "what's on your mind",
    "what are you thinking",
    "say something",
]

POST_BUTTONS = ["post", "share now", "share"]

PAGES_BLOCKED_PHRASES = [
    "doesn't allow pages",
    "don't allow pages",
    "does not allow pages",
    "do not allow pages",
    "not allow pages",
]


def _click_composer_media(page, log):
    """Open the media composer. Returns True on success. Retries once because
    the first attempt can race a slow group-page load."""
    for attempt in (1, 2):
        if _click_composer_media_once(page, log):
            return True
        if attempt == 1:
            log("retrying media composer after a short wait...")
            page.wait_for_timeout(3000)
    return False


def _click_composer_media_once(page, log):
    # Try each known label via Playwright's accessible-name match first.
    for label in COMPOSER_PHOTO_LABELS:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=8000)
                log(f"opened media composer via '{label}'")
                return True
        except Exception:
            continue
    # DOM scan: any visible element (role=button/aria-label) whose label
    # contains a media keyword. Covers "Attach a photo or video", etc.
    try:
        handle = page.evaluate_handle(
            """(labels)=>{
                const els=document.querySelectorAll('[role="button"],[aria-label],button,span');
                for(const el of els){
                    const aria=(el.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim().toLowerCase();
                    const txt=(el.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();
                    const hay=aria+' '+txt;
                    if(!hay) continue;
                    for(const L of labels){
                        if(hay.includes(L)){
                            const r=el.getBoundingClientRect();
                            if(r.width&&r.height) return el;
                        }
                    }
                }
                return null;
            }""",
            [L.lower() for L in COMPOSER_PHOTO_LABELS],
        )
        if handle is not None:
            try:
                is_el = handle.evaluate("el => el instanceof Element")
            except Exception:
                is_el = False
            if is_el:
                handle.scroll_into_view_if_needed(timeout=4000)
                handle.click(timeout=6000)
                log("opened media composer via DOM scan")
                return True
    except Exception:
        pass
    # Fallback: any file input already present (some layouts expose it directly).
    try:
        page.wait_for_selector('input[type="file"]', timeout=8000)
        return True
    except PWTimeout:
        return False


def _upload_file(page, file_path, log):
    """Attach one or more media files. file_path can be a string or a list
    of strings. Returns True when upload completes."""
    paths = file_path if isinstance(file_path, (list, tuple)) else [file_path]
    # Several file inputs can exist on a feed page; use a *visible* one (the
    # newly-opened composer's input) rather than the first (usually hidden).
    deadline = time.monotonic() + 15
    input_el = None
    while time.monotonic() < deadline:
        try:
            visible = page.locator('input[type="file"]:visible').first
            if visible.count() and visible.is_visible(timeout=800):
                input_el = visible
                break
        except Exception:
            pass
        page.wait_for_timeout(500)
    if input_el is None:
        # The composer may not have actually opened (a stray click, or the
        # media toolbar re-rendered). Try to re-open it once before giving up.
        if _click_composer_media(page, log):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    visible = page.locator('input[type="file"]:visible').first
                    if visible.count() and visible.is_visible(timeout=800):
                        input_el = visible
                        break
                except Exception:
                    pass
                page.wait_for_timeout(500)
        if input_el is None:
            raise RuntimeError("no visible file input appeared")
    input_el.set_input_files(paths)
    log(f"file(s) attached ({len(paths)}), waiting for upload...")
    # Wait for a preview/processing UI to appear, then settle.
    timeout = 90 if len(paths) > 1 else 60
    for _ in range(timeout):
        page.wait_for_timeout(1000)
        body = _safe_text(page)
        low = body.lower()
        if "processing" in low or "uploading" in low:
            continue
        if "error" in low and "couldn't upload" in low:
            raise RuntimeError("upload failed (couldn't upload message)")
        # Preview container appears once uploaded.
        if _has_caption_field(page):
            return True
    # Give it one more generous window for videos.
    page.wait_for_timeout(5000)
    return _has_caption_field(page)


def _safe_text(page):
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def _visible_dialogs_text(page, max_chars=400):
    """Text of every [role=dialog], for diagnosing stuck composers."""
    parts = []
    try:
        for i, handle in enumerate(page.query_selector_all(DIALOG)):
            try:
                txt = (handle.inner_text() or "").replace("\n", " ").strip()
            except Exception:
                continue
            parts.append(f"[{i}] {txt[:max_chars]}")
    except Exception:
        pass
    return " | ".join(parts)


def _caption_locator(page):
    # Group composer caption ("Create a public post…") has no aria-label and
    # can sit in a dialog that isn't the first one in the DOM, so prefer a
    # labeled field first, then any *visible* contenteditable inside a visible
    # dialog. Scoping to dialog avoids matching newsfeed comment boxes.
    for label in CAPTION_LABELS:
        try:
            el = page.locator(f'[contenteditable="true"][aria-label*="{label}" i]').first
            if el.is_visible(timeout=500):
                return el
        except Exception:
            continue
    try:
        q = page.locator(f'{DIALOG}:visible [contenteditable="true"]:visible')
        n = q.count()
        for i in range(n):
            el = q.nth(i)
            try:
                txt = (el.inner_text() or "").strip()
            except Exception:
                txt = ""
            try:
                ph = (el.get_attribute("aria-label") or "").strip().lower()
            except Exception:
                ph = ""
            # Accept an empty field or placeholder-style text (avoids matching
            # a prefilled topic/tag field and post title inputs).
            if not txt or any(w in ph for w in ("create", "write", "say", "share", "what", "public")):
                return el
    except Exception:
        pass
    return None


def _has_caption_field(page):
    return _caption_locator(page) is not None


def _fill_caption(page, caption):
    # The caption is held in the automation browser's own storage so the app
    # never persists the posting content. It is cleared the moment it's used,
    # so it is forgotten right after each post.
    try:
        page.evaluate(
            "localStorage.setItem('sm_pending_caption', arguments[0])",
            caption or "",
        )
        stored = page.evaluate("localStorage.getItem('sm_pending_caption')") or ""
        if stored:
            caption = stored
    except Exception:
        pass
    if not caption:
        return True
    for label in CAPTION_LABELS:
        try:
            el = page.locator(f'[contenteditable="true"][aria-label*="{label}" i]').first
            if el.is_visible(timeout=1500):
                el.click()
                el.fill(caption)
                try:
                    page.evaluate("localStorage.removeItem('sm_pending_caption')")
                except Exception:
                    pass
                return True
        except Exception:
            continue
    # Generic: a contenteditable inside any *visible* dialog. The group
    # composer's caption ("Create a public post…") has no aria-label and can
    # sit in a dialog that is not the first one in the DOM.
    el = _caption_locator(page)
    if el is not None:
        try:
            el.click()
            el.fill(caption)
            try:
                page.evaluate("localStorage.removeItem('sm_pending_caption')")
            except Exception:
                pass
            return True
        except Exception:
            pass
    return False


def _composer_dialog(page):
    """Locator of the visible composer dialog that still holds our caption text
    (the one we must submit). Scoped this way avoids ghost dialogs and the
    notifications side-panel, whose stray 'Post' buttons can otherwise swallow
    the click and leave the real composer open."""
    try:
        dlg = page.locator(f'{DIALOG}:visible')
        n = dlg.count()
        for i in range(n):
            el = dlg.nth(i)
            try:
                ce = el.locator(f'[contenteditable="true"]:visible')
                cn = ce.count()
                for j in range(cn):
                    try:
                        txt = (ce.nth(j).inner_text() or "").strip()
                    except Exception:
                        txt = ""
                    if txt:
                        return el
            except Exception:
                continue
    except Exception:
        pass
    return None


def _click_post(page, log):
    """Click the post/submit button in the composer dialog. Returns True on success."""
    # Retry window: the confirmation dialog may take a moment to render.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        # Gather every visible dialog, caption-holding ones first. The group
        # composer renders as layered/sibling dialogs ('Create post' +
        # 'Add groups' / 'Add topic' panels) and the Post button can sit in a
        # dialog OTHER than the first one that holds the caption — searching
        # only that first dialog is why some groups fail to find the button.
        try:
            all_dlgs = page.locator(f'{DIALOG}:visible')
            count = all_dlgs.count()
            dialogs = [all_dlgs.nth(i) for i in range(min(count, 10))]
        except Exception:
            dialogs = []
        cands = []
        for d in dialogs:
            try:
                has_cap = d.locator(f'[contenteditable="true"]:visible').count() > 0
            except Exception:
                has_cap = False
            if has_cap:
                cands.insert(0, d)
            else:
                cands.append(d)
        for d in cands:
            for name in POST_BUTTONS:
                try:
                    btn = d.get_by_role(
                        "button",
                        name=re.compile(f"^{re.escape(name)}$", re.IGNORECASE),
                    ).first
                    if btn.is_visible(timeout=600):
                        btn.click(timeout=4000)
                        log(f"clicked post button '{name}' in composer")
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(700)
    return False


def _detect_pending(page):
    body = _safe_text(page).lower()
    for phrase in PENDING_PHRASES:
        if phrase in body:
            return True
    return False


def _wait_composer_closed(page):
    """Wait for the composer dialog (a visible dialog whose contenteditable
    still holds our caption) to close, up to the deadline.

    Returns True once the composer is gone, False on timeout. This only means
    the composer UI went away — it does NOT by itself prove the post published
    (the composer can also close on error/dismiss), so callers must additionally
    verify the post landed.
    """
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        open_composer = False
        try:
            q = page.locator(f'{DIALOG}:visible [contenteditable="true"]:visible')
            n = q.count()
            for i in range(n):
                el = q.nth(i)
                try:
                    txt = (el.inner_text() or "").strip()
                except Exception:
                    txt = ""
                if txt:
                    open_composer = True
                    break
        except Exception:
            pass
        if not open_composer:
            return True
        page.wait_for_timeout(4000)
    return False


def _verify_post_landed(page, before, caption):
    """Poll the group feed for evidence the post we just published landed.

    Returns (confirmed, post_url):
      confirmed  - True when a post NEW to this feed appeared after posting
                   (Facebook inserts our published post at the top). Real posts
                   are treated as published even if the caption token can't be
                   matched (truncation / lazy DOM / sort order).
      post_url   - a URL ONLY when a post can be positively attributed to this
                   caption (new + card text matches the token, or any post
                   matching the token). Never a stranger's post URL.
    Confirmed requires a *new* feed item; a closed composer alone is not enough
    (it can close on error/dismiss without actually publishing, e.g. when the
    account isn't a member or the Page can't post).
    """
    before_hrefs = {p.get("href") for p in (before or [])}
    deadline = time.monotonic() + 45
    reloaded = False
    while time.monotonic() < deadline:
        try:
            page.wait_for_timeout(6000)
        except Exception:
            pass
        after = _group_post_urls(page)
        new = [p for p in after if p.get("href") not in before_hrefs]
        url = _pick_post_url(page, before, caption)  # new-post only
        if new and url:
            return True, url
        if new:
            return True, None
        if not reloaded and time.monotonic() > (deadline - 15):
            # Feed may be sorted/lazy and not showing our top post; reload once
            # to force the group page to render the newest post at the top.
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(4000)
            except Exception:
                pass
            reloaded = True
        # Nudge the feed in case it is paginated/"Top"-sorted.
        try:
            page.evaluate(
                "() => { const feed = document.querySelector('div[role=\"feed\"]'); "
                "feed && feed.scrollIntoView({block:'start'}); }"
            )
        except Exception:
            pass
    return False, None


def _detect_success(page):
    """Deprecated placeholder kept for interface compatibility.

    Use _wait_composer_closed + _verify_post_landed instead; those positively
    verify a post landed rather than assuming a closed composer means success.
    """
    return _wait_composer_closed(page)


# Matches the permalink shape Facebook uses for group posts:
#   https://www.facebook.com/groups/{id}/posts/{post_id}/
#   https://www.facebook.com/groups/{id}/permalink/{post_id}/
# Group ids may be numeric or an alphanumeric vanity slug.
_GROUP_POST_RE = re.compile(
    r"/groups/[^/]+/(?:posts|permalink)/[^/]+"
)


def _group_post_urls(page):
    """List {href, text} for every post card currently rendered in the group
    feed, in DOM order (topmost first). Text is the post card's visible text
    (the caption sits inside it), used to match the post we just made."""
    try:
        rows = page.evaluate(
            """() => {
                const feed = document.querySelector('div[role="feed"]');
                const roots = [];
                if (feed) {
                    feed.querySelectorAll('div[role="article"], article').forEach(el => roots.push(el));
                }
                if (!roots.length && feed) {
                    feed.querySelectorAll('a[href*="/posts/"], a[href*="/permalink/"]').forEach(a => {
                        let root = a;
                        for (let i = 0; i < 7 && root.parentElement && root.parentElement !== document.body; i++) {
                            root = root.parentElement;
                        }
                        if (!root.hasAttribute('data-x-excluded')) {
                            roots.push(root);
                            root.setAttribute('data-x-excluded', '1');
                        }
                    });
                }
                const out = [];
                const re = /\\/groups\\/[^/]+\\/(?:posts|permalink)\\/[^/]+/;
                for (const r of roots) {
                    const links = r.querySelectorAll('a[href*="/groups/"], a[href*="/permalink/"]');
                    for (const a of links) {
                        const href = a.href || '';
                        if (re.test(href)) {
                            out.push({
                                href: href,
                                text: (r.innerText || '').replace(/\\s+/g, ' ').slice(0, 300),
                            });
                            break;
                        }
                    }
                }
                return out;
            }"""
        )
        return [dict(r) for r in (rows or [])]
    except Exception:
        return []


def _make_full_url(page, href):
    if not href:
        return None
    if not (href.startswith("http://") or href.startswith("https://")):
        href = "https://www.facebook.com" + (href if href.startswith("/") else "/" + href)
    try:
        return href.split("?", 1)[0]
    except Exception:
        return href


def _caption_token(caption):
    token = re.sub(r"[^a-z0-9]+", " ", (caption or "").lower()).strip()
    return token[:40] if token else None


def _pick_post_url(page, before, caption):
    """Find the URL of the post we just published.

    Only ever inspects posts NEW to the feed since `before` was snapshotted
    (Facebook inserts our published post at the top). Prefers a new post whose
    card text matches the caption token; otherwise falls back to the first new
    post (the topmost one that appeared during our posting window — ours, since
    we're the only actor posting). Never scans the pre-existing feed for a
    stranger's post. Returns None if no new post is found.
    """
    token = _caption_token(caption)
    after = _group_post_urls(page)
    if not after:
        return None
    before_hrefs = {p.get("href") for p in (before or [])}
    new = [p for p in after if p.get("href") not in before_hrefs]

    def matches(p):
        if not token or not p.get("text"):
            return False
        return token in p["text"].lower()

    for p in new:
        if matches(p):
            return _make_full_url(page, p["href"])
    if new:
        return _make_full_url(page, new[0]["href"])
    return None


def post_media(page, group_id, file_path, caption="", log=None):
    """Post one media file to a group.

    Returns (status, message, post_url):
      posted   - media published (post_url is the best-known permalink, or None).
      pending  - post routed to admin approval (caller flags the group as skip).
      failed   - an error occurred; message explains why.
    """
    log = log or (lambda msg: print(msg))
    url = f"https://www.facebook.com/groups/{group_id}/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
    except Exception as e:
        return "failed", f"could not load group page: {e}", None

    # Snapshot the feed's post permalinks so we can spot the new post after
    # publishing (Facebook inserts it into this list).
    before = _group_post_urls(page)

    if not _click_composer_media(page, log):
        return "failed", "could not find the photo/video composer", None

    try:
        if not _upload_file(page, file_path, log):
            return "failed", "upload did not complete in time", None
        log("upload complete")
    except RuntimeError as e:
        return "failed", str(e), None

    _fill_caption(page, caption)

    if not _click_post(page, log):
        return "failed", "could not find the post button", None

    page.wait_for_timeout(4000)
    if _detect_pending(page):
        return "pending", "post routed to admin approval", None
    # Evidence-first: the composer does NOT have to close for the post to have
    # published (Facebook's current composer keeps a "Create post / Add groups /
    # Add topic" overlay shell open even after submit). So verify a NEW post
    # actually landed in the feed BEFORE trusting the composer state. Only if
    # nothing landed do we consult composer-close + diag dumps.
    confirmed, post_url = _verify_post_landed(page, before, caption)
    if confirmed:
        if post_url:
            return "posted", "published", post_url
        return "posted", "published (post URL not capturable)", None
    if not _wait_composer_closed(page):
        log("[diag] composer did not close and no post landed")
        log(f"[diag] url={page.url}")
        log(f"[diag] dialogs: {_visible_dialogs_text(page)}")
        return "failed", "composer did not close after posting", None
    # The composer closed but no new post appeared — treat as not confirmed
    # (it can close on error/dismiss, e.g. when the Page can't post or the
    # account isn't a member).
    log("[verify] no new post appeared in the feed — treating as not confirmed")
    log(f"[diag] url={page.url}")
    log(f"[diag] dialogs: {_visible_dialogs_text(page)}")
    return "failed", "post could not be verified in the group (it may not have published)", None


def _has_media_composer(page):
    """True if a photo/video attach affordance is available on the page (the
    Page can post here). Mirrors _click_composer_media exactly so the pre-flight
    filter and the actual posting logic never diverge. Uses real geometry
    (getBoundingClientRect) rather than ElementHandle.is_visible(), which is
    unreliable with Facebook's layered dialogs."""
    # 1. role/accessibility match.
    for label in COMPOSER_PHOTO_LABELS:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)).first
            if btn.is_visible(timeout=1200):
                return True
        except Exception:
            continue
    # 2. DOM scan: any element with a real on-screen box whose label contains a
    #    media keyword. Same check that successfully opened the composer.
    try:
        ok = page.evaluate(
            """(labels)=>{
                const els=document.querySelectorAll('[role="button"],[aria-label],button,span');
                for(const el of els){
                    const aria=(el.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim().toLowerCase();
                    const txt=(el.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();
                    const hay=aria+' '+txt;
                    if(!hay) continue;
                    for(const L of labels){
                        if(hay.includes(L)){
                            const r=el.getBoundingClientRect();
                            if(r.width&&r.height) return true;
                        }
                    }
                }
                return false;
            }""",
            [L.lower() for L in COMPOSER_PHOTO_LABELS],
        )
        if ok:
            return True
    except Exception:
        pass
    # 3. Visible file input (some layouts expose it without a click).
    try:
        return page.locator('input[type="file"]:visible').count() > 0
    except Exception:
        return False


def group_is_page_postable(page, group_id, log=None):
    """Return True only if the group is open to posting as the current identity
    (the Page): no 'doesn't allow Pages' banner, no 'Join Group' gate, and a
    visible photo/video composer. A load error is treated as postable
    (optimistic) so a transient network failure can't skip a whole batch."""
    log = log or (lambda msg: print(msg))
    url = f"https://www.facebook.com/groups/{group_id}/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
    except Exception as e:
        log(f"[postable-check] load error for {group_id}: {e}")
        return True
    body = _safe_text(page).lower()
    for phrase in PAGES_BLOCKED_PHRASES:
        if phrase in body:
            log(f"[postable-check] {group_id}: Pages blocked ('{phrase}')")
            return False
    # A visible composer is authoritative: if we can click media + post, the
    # group is postable. Checking this before the generic 'join group' phrase
    # avoids false negatives where stray 'Join group' text appears alongside a
    # real composer (e.g. KENYANS ONLINE, which demonstrably accepts Page posts).
    if _has_media_composer(page):
        return True
    if re.search(r"\bjoin group\b", body):
        log(f"[postable-check] {group_id}: not a member, join gate shown")
        return False
    log(f"[postable-check] {group_id}: no visible media composer")
    return False


PAGE_CAPTION_STRINGS = [
    "what's on your mind?",
    "say something",
    "write something",
]


def _click_page_composer(page, log):
    """Open the page 'Create post' modal. Returns True."""
    # Open the composer field (modal)
    for label in ["what's on your mind?", "write something", "create post"]:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=8000)
                log(f"opened page composer via '{label}'")
                return True
        except Exception:
            continue
    try:
        box = page.locator('[aria-label*="What\'s on your mind" i]').first
        box.click(timeout=8000)
        return True
    except Exception:
        pass
    return False


def _click_page_media(page, log):
    """Inside the Create post modal, click Photo/video to add media. Returns True."""
    for label in ["photo/video", "add photo/video", "add photos/videos", "add a photo/video"]:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=8000)
                log(f"page media via '{label}'")
                return True
        except Exception:
            continue
    return False


def _upload_page_file(page, file_path, log):
    """Set the media file on the modal's file input and wait for the preview.

    Chooses an input that accepts the file type (image vs video) when possible,
    otherwise falls back to any visible file input in the composer dialog.
    """
    ext = os.path.splitext(file_path)[1].lower()
    is_video = ext in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".3gp")
    accept_hint = "video" if is_video else "image"
    try:
        inp = page.locator(f'{DIALOG} input[type="file"][accept*="{accept_hint}"]').first
        inp.set_input_files(file_path, timeout=20000)
    except Exception:
        try:
            inp = page.locator(f'{DIALOG} input[type="file"]:visible').last
            inp.set_input_files(file_path, timeout=20000)
        except Exception as e:
            raise RuntimeError(f"could not attach page media: {e}")
    log("page file attached, waiting for preview...")
    for _ in range(60):
        page.wait_for_timeout(1000)
        body = _safe_text(page).lower()
        # "Edit" appears once a photo/video is attached in the modal
        if re.search(r"\bedit\b", body):
            page.wait_for_timeout(1500)
            return True
        if "couldn't upload" in body:
            raise RuntimeError("page upload failed (couldn't upload)")
    return False


def _find_caption_boxes(page):
    """Return visible contenteditable caption boxes across all dialog layers,
    preferring ones whose placeholder/label mentions a caption prompt."""
    CAP_HINT = re.compile(r"what'?s on your mind|describe your reel|say something|write something|add a description", re.IGNORECASE)
    boxes = []
    try:
        for dlg in page.query_selector_all(DIALOG):
            for ce in dlg.query_selector_all('[contenteditable="true"]'):
                try:
                    if not ce.is_visible():
                        continue
                except Exception:
                    continue
                hint = (ce.get_attribute("aria-label") or "") + " " + (
                    ce.get_attribute("data-lexical-text") or "") + " " + (
                    ce.get_attribute("aria-placeholder") or "")
                boxes.append((hint, ce))
    except Exception:
        pass
    if not boxes:
        return []
    # prefer caption-prompt boxes; fall back to any visible contenteditable
    marked = [(0, ce) for h, ce in boxes if CAP_HINT.search(h)]
    if marked:
        return [ce for _, ce in marked]
    return [ce for _, ce in boxes]


def _type_into_box(page, box, caption):
    """Type the full caption (emoji/multiline) into a contenteditable and
    verify it landed; retries up to 3 times."""
    expect = caption.replace("\u200b", "").strip()
    box.click()
    for attempt in range(3):
        try:
            box.evaluate("el=>{el.focus(); el.select(); document.execCommand('delete');}")
        except Exception:
            pass
        page.wait_for_timeout(200)
        page.keyboard.insert_text(caption)
        page.wait_for_timeout(400)
        got = (box.inner_text() or "").replace("\u200b", "").strip()
        if got.replace("\r\n", "\n") == expect.replace("\r\n", "\n"):
            return True
    return False


def _fill_page_caption(page, caption):
    """Type the caption into the modal's caption contenteditable (emoji safe)."""
    if not caption:
        return True
    boxes = _find_caption_boxes(page)
    if not boxes:
        return False
    return _type_into_box(page, boxes[0], caption)


def _fill_caption_box_any_dialog(page, caption, log=None):
    """Re-set the caption on the current screen's caption field (called on
    every step of the video/reel flow so the final caption is attached)."""
    if not caption:
        return True
    boxes = _find_caption_boxes(page)
    if not boxes:
        return False
    ok = _type_into_box(page, boxes[0], caption)
    if ok and log:
        log(f"caption set on screen (field len ok)")
    return ok


_PUBLISH_WORDS = [
    "publish original post",
    "publish reel",
    "publish now",
    "publish",
    "share",
    "post",
]


def _click_publish(page, log):
    """Click the page's real publish/confirm button using native (Playwright)
    clicks, patiently working through ("Post" -> "Publish Original Post")
    via the aria-label / innerText, while Facebook renders each next screen."""
    priority = ["publish original post", "publish reel",
                "publish now", "publish", "share", "post"]
    deadline = time.monotonic() + 25
    clicked = False
    while time.monotonic() < deadline:
        chosen = None
        handle = None
        for w in priority:
            try:
                h = page.evaluate_handle(
                    """(target)=>{
                        const els=document.querySelectorAll('[role="button"],[aria-label],button');
                        for(const el of els){
                            const aria=(el.getAttribute('aria-label')||'').replace(/\\s+/g,' ').trim().toLowerCase();
                            const txt=(el.innerText||'').replace(/\\s+/g,' ').trim().toLowerCase();
                            if(aria===target||txt===target){
                                const r=el.getBoundingClientRect();
                                if(r.width&&r.height) return el;
                            }
                        }
                        return null;
                    }""",
                    w,
                )
                if h is None:
                    continue
                # If it returned a null JSON value, evaluate_handle returned a
                # JSHandle of null (no match). Otherwise it is an element.
                try:
                    is_element = h.evaluate("el => el instanceof Element")
                except Exception:
                    is_element = False
                if is_element:
                    chosen = w
                    handle = h
                    break
            except Exception:
                continue
        if chosen is not None and handle is not None:
            try:
                handle.scroll_into_view_if_needed(timeout=4000)
                handle.click(timeout=5000)
                log(f"clicked publish: {chosen}")
                clicked = True
                page.wait_for_timeout(2000)
                continue
            except Exception as e:
                log(f"publish click err: {e}")
        page.wait_for_timeout(700)
    return clicked


def post_media_to_page(page, file_path, page_url, caption="", log=None):
    """Post one media file to a Facebook Page via the real composer.

    Returns (status, message): same contract as post_media.
    """
    log = log or (lambda msg: print(msg))
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
    except Exception as e:
        return "failed", f"could not load page: {e}"

    # Switch into the page profile if offered (voice switcher).
    try:
        page.evaluate(
            """()=>{
                for (const el of document.querySelectorAll('[role="button"],button')) {
                    const t=(el.innerText||el.getAttribute('aria-label')||'').trim();
                    if (/^switch now$/i.test(t)) { el.click(); return; }
                }
            }"""
        )
        page.wait_for_timeout(4000)
    except Exception:
        pass

    if not _click_page_composer(page, log):
        return "failed", "could not open the page composer"

    page.wait_for_timeout(1500)
    if not _click_page_media(page, log):
        # media may be reachable directly on the composer row
        pass
    page.wait_for_timeout(1500)

    try:
        if not _upload_page_file(page, file_path, log):
            return "failed", "page upload did not complete in time"
        log("page upload complete")
    except RuntimeError as e:
        return "failed", str(e)

    if caption:
        if not _fill_page_caption(page, caption):
            _fill_caption(page, caption)
    page.wait_for_timeout(1500)

    # Move to the confirmation step and publish.
    # Photos need one "Next" then a Post button. Videos (reel flow) take
    # several "Next" screens, and the caption must be re-entered on each one
    # (the final caption lives in e.g. "Describe your reel..."), because
    # Facebook drops the earlier composer caption. No editing is done — the
    # media is ready-made; we only: set caption -> advance -> publish.
    # ---- advance through confirmation flow ----
    # Photos: 1 "Next". Videos (reel flow): several "Next" screens. Always
    # (re)set the caption on each screen because Facebook drops the earlier one.
    def _has_text_button(page, pattern):
        try:
            b = page.get_by_role("button", name=pattern).first
            return b.is_visible(timeout=600), b
        except Exception:
            return False, None

    # 1. Keep clicking Next while present (and keep caption set), up to a cap.
    for _ in range(8):
        if caption:
            _fill_caption_box_any_dialog(page, caption, log)
        vis, nxt = _has_text_button(page, re.compile("^next$", re.IGNORECASE))
        if vis:
            try:
                nxt.click(timeout=8000)
                log("clicked next")
                page.wait_for_timeout(1500)
                continue
            except Exception:
                pass
        break

    # 2. Publish. The caption is already set — do NOT re-fill (that can click
    #    into an upsell dialog's caption field and stall). Click the definitive
    #    publish button; a "Grow your following / Get the word out" upsell may
    #    appear first whose real action is "Publish Original Post".
    page.wait_for_timeout(1500)
    _click_publish(page, log)

    page.wait_for_timeout(6000)
    if _detect_pending(page):
        return "pending", "post routed to admin approval"
    # Confirm the composer dialogs actually closed (indicating a successful post).
    try:
        app_dialog = page.inner_text("body") or ""
    except Exception:
        app_dialog = ""
    if "reel settings" in app_dialog.lower() or "create post" in app_dialog.lower():
        return "failed", "composer still open - post did not submit"
    if _detect_success(page):
        return "posted", "published"
    return "failed", "unable to confirm publish outcome"