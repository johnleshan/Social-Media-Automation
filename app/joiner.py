"""Auto-join: visit discovered groups and click Join Group.

Results returned by join_group():
  joined         - membership confirmed right away.
  pending        - request sent; an admin must approve it.
  already_member - no Join button / composer visible: already in.
  unviewable     - group unavailable (deleted/private/banned).
  no_button      - page loaded but no way to join from here.
  failed:<why>   - something went wrong.
"""
import re
import time

UNVIEWABLE_PHRASES = [
    "this content isn't available",
    "this content is no longer available",
]

JOIN_LABELS = ("join group", "join", "request to join", "ask to join", "request")
PENDING_PHRASES = [
    "cancel request",
    "request sent",
    "requested",
    "pending",
    "membership request",
]
MEMBER_PHRASES = ["leave group", "leave this group"]
COMPOSER_PHRASES = [
    "write something",
    "what's on your mind",
    "add a photo",
    "start discussing",
]


def _body_text(page):
    try:
        return (page.inner_text("body") or "").lower()
    except Exception:
        return ""


def _find_join_button(page):
    buttons = page.query_selector_all(
        'div[role="button"], a[role="button"], span[role="button"], button'
    )
    for el in buttons:
        try:
            if not el.is_visible():
                continue
            parts = [
                (el.get_attribute("aria-label") or "").strip().lower(),
                (el.inner_text() or "").strip().lower(),
            ]
            # Match any label that contains one of the join phrases
            if any(
                any(lab in part for lab in JOIN_LABELS)
                for part in parts if part
            ):
                return el
        except Exception:
            continue
    return None


def _click_agree_if_present(page):
    """Some groups show a rules dialog with an Agree/Done button."""
    for el in page.query_selector_all('div[role="button"], button'):
        try:
            if not el.is_visible():
                continue
            label = (
                (el.get_attribute("aria-label") or "")
                + " "
                + (el.inner_text() or "")
            ).strip().lower()
            if label.startswith(("agree", "accept", "done")):
                el.click()
                return True
        except Exception:
            continue
    return False


def _state_after_click(page):
    low = _body_text(page)
    if any(p in low for p in PENDING_PHRASES):
        return "pending"
    if any(p in low for p in MEMBER_PHRASES):
        return "joined"
    if any(p in low for p in COMPOSER_PHRASES):
        return "joined"
    if _find_join_button(page) is None:
        # button gone but no explicit signal — most likely joined
        return "joined"
    return "unknown"


def join_group(page, group_id, log=None):
    """Visit one group and try to join. Returns (result, detail)."""
    log = log or (lambda msg: print(msg))
    url = f"https://www.facebook.com/groups/{group_id}/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector('div[role="main"]', timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1500)
    except Exception as e:
        return "failed", f"load error: {e}"

    low = _body_text(page)
    if any(p in low for p in UNVIEWABLE_PHRASES):
        return "unviewable", "content isn't available"

    btn = _find_join_button(page)
    if btn is None and re.search(r"\bjoin group\b", low):
        return "no_button", "join text present but button not clickable"
    if btn is not None:
        try:
            btn.click()
        except Exception as e:
            return "failed", f"click error: {e}"

        if _click_agree_if_present(page):
            page.wait_for_timeout(1200)

        # Poll for up to 8 seconds (was 3s) — Facebook can be slow to react
        state = "unknown"
        deadline = time.time() + 8
        while time.time() < deadline:
            page.wait_for_timeout(500)
            state = _state_after_click(page)
            if state != "unknown":
                break

        if state == "pending":
            return "pending", "request sent — waiting for admin approval"
        if state == "joined":
            return "joined", "membership confirmed"
        return "failed", "clicked join but final state unclear"

    # no Join button anywhere:
    if any(p in low for p in PENDING_PHRASES):
        return "pending", "request already awaiting admin approval"
    if any(p in low for p in MEMBER_PHRASES):
        return "already_member", "leave-group present"
    return "no_button", "no join button found"
