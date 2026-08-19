"""Media posting engine for groups.

Posts one image/video file to one group through the real Facebook composer,
using the logged-in profile session. Never uses the internal GraphQL API.

Returns a result tuple (status, message):
  posted   - media published.
  pending  - post routed to admin approval (caller flags the group as skip).
  failed   - an error occurred; message explains why.
"""
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
]

POST_BUTTONS = ["post", "share now", "share"]


def _click_composer_media(page, log):
    """Open the media composer. Returns True on success."""
    for label in COMPOSER_PHOTO_LABELS:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(label), re.IGNORECASE)).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=8000)
                log(f"opened media composer via '{label}'")
                return True
        except Exception:
            continue
    # Fallback: any file input already present (some layouts expose it directly).
    try:
        page.wait_for_selector('input[type="file"]', timeout=8000)
        return True
    except PWTimeout:
        return False


def _upload_file(page, file_path, log):
    """Attach the media file. Returns True when upload completes."""
    try:
        input_el = page.wait_for_selector('input[type="file"]', timeout=15000)
    except PWTimeout:
        raise RuntimeError("no file input appeared")
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


def _has_caption_field(page):
    for label in CAPTION_LABELS:
        try:
            el = page.locator(f'[contenteditable="true"][aria-label*="{label}" i]').first
            if el.is_visible(timeout=1000):
                return True
        except Exception:
            continue
    # Generic: a contenteditable inside the dialog
    try:
        dlg = page.query_selector(DIALOG)
        if dlg:
            return len(dlg.query_selector_all('[contenteditable="true"]')) > 0
    except Exception:
        pass
    return False


def _fill_caption(page, caption):
    if not caption:
        return True
    for label in CAPTION_LABELS:
        try:
            el = page.locator(f'[contenteditable="true"][aria-label*="{label}" i]').first
            if el.is_visible(timeout=1500):
                el.click()
                el.fill(caption)
                return True
        except Exception:
            continue
    try:
        el = page.locator(f'{DIALOG} [contenteditable="true"]').first
        if el.is_visible(timeout=1500):
            el.click()
            el.fill(caption)
            return True
    except Exception:
        pass
    return False


def _click_post(page, log):
    """Click the post/submit button in the dialog. Returns True on success."""
    for name in POST_BUTTONS:
        try:
            btn = page.get_by_role("button", name=re.compile(f"^{re.escape(name)}$", re.IGNORECASE)).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=8000)
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
                    b.click(timeout=8000)
                    return True
    except Exception:
        pass
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