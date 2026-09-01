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


def _click_composer_media(page, log):
    """Open the media composer. Returns True on success."""
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
    """Attach the media file. Returns True when upload completes."""
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
        raise RuntimeError("no visible file input appeared")
    input_el.set_input_files(file_path)
    log("file attached, waiting for upload...")
    # Wait for a preview/processing UI to appear, then settle.
    for _ in range(60):
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


def _click_post(page, log):
    """Click the post/submit button in the dialog. Returns True on success."""
    # Retry window: the confirmation dialog may take a moment to render.
    deadline = time.monotonic() + 14
    while time.monotonic() < deadline:
        for name in POST_BUTTONS:
            try:
                btn = page.get_by_role("button", name=re.compile(f"^{re.escape(name)}$", re.IGNORECASE)).first
                if btn.is_visible(timeout=1000):
                    btn.click(timeout=4000)
                    return True
            except Exception:
                continue
        # Fallback: any button inside the dialog whose text contains post/share
        try:
            dlg = page.query_selector(DIALOG)
            if dlg:
                buttons = dlg.query_selector_all("button")
                for b in buttons:
                    txt = (b.inner_text() or "").strip().lower()
                    if txt in POST_BUTTONS or txt.startswith("post "):
                        b.click(timeout=4000)
                        return True
        except Exception:
            pass
        page.wait_for_timeout(700)
    return False


def _detect_pending(page):
    body = _safe_text(page).lower()
    for phrase in PENDING_PHRASES:
        if phrase in body:
            return True
    return False


def _detect_success(page):
    """Return True if the dialog is gone and no pending signal is present."""
    page.wait_for_timeout(3000)
    dlg = None
    try:
        dlg = page.query_selector(DIALOG)
    except Exception:
        pass
    if dlg and dlg.is_visible():
        return False
    # A "your post was posted" style confirmation is ideal but optional.
    body = _safe_text(page).lower()
    for phrase in ["your post was posted", "post is live", "shared to group"]:
        if phrase in body:
            return True
    return True


def post_media(page, group_id, file_path, caption="", log=None):
    """Post one media file to a group. Returns (status, message)."""
    log = log or (lambda msg: print(msg))
    url = f"https://www.facebook.com/groups/{group_id}/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
    except Exception as e:
        return "failed", f"could not load group page: {e}"

    if not _click_composer_media(page, log):
        return "failed", "could not find the photo/video composer"

    try:
        if not _upload_file(page, file_path, log):
            return "failed", "upload did not complete in time"
        log("upload complete")
    except RuntimeError as e:
        return "failed", str(e)

    _fill_caption(page, caption)

    if not _click_post(page, log):
        return "failed", "could not find the post button"

    page.wait_for_timeout(4000)
    if _detect_pending(page):
        return "pending", "post routed to admin approval"
    if _detect_success(page):
        return "posted", "published"
    return "failed", "unable to confirm post outcome"


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