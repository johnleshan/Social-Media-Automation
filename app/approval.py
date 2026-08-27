"""Approval pre-check.

Determines, BEFORE posting, whether a group routes posts through admin
approval. Reads signals from the live group page and its embedded payload so we
never have to post a test post or monitor for later approval/rejection.

Statuses returned:
  safe    - posts publish immediately.
  skip    - posts require approval (or the group cannot be posted to).
  unknown - could not determine; never auto-posted.
"""
import re

APPROVAL_PHRASES = [
    "reviewed before",
    "reviewed by admins",
    "reviewed by moderators",
    "reviewed by an admin",
    "pending approval",
    "admin approval",
    "post approval is on",
    "post approval",
    "must be approved",
    "requires approval",
    "require approval",
    "require post approval",
    "requires post approval",
    "awaiting approval",
    "approved by admins",
    "approved by moderators",
    "approval is on",
    "posts are pending",
    "pending review",
    "visible after approval",
    "visible after it's approved",
    "after it is approved",
]

# Keys that Facebook's embedded JSON may expose around group posting.
APPROVAL_TRUE_PATTERNS = [
    r'post_approval["\']?\s*[:=]\s*(?:true|1)\b',
    r'approval_required["\']?\s*[:=]\s*(?:true|1)\b',
    r'require_approval["\']?\s*[:=]\s*(?:true|1)\b',
    r'post_approval_enabled["\']?\s*[:=]\s*(?:true|1)\b',
    r'postApproval["\']?\s*[:=]\s*(?:true|1)\b',
    r'approvalRequired["\']?\s*[:=]\s*(?:true|1)\b',
    r'requirePostApproval["\']?\s*[:=]\s*(?:true|1)\b',
]

APPROVAL_FALSE_PATTERNS = [
    r'post_approval["\']?\s*[:=]\s*(?:false|0)\b',
    r'approval_required["\']?\s*[:=]\s*(?:false|0)\b',
    r'require_approval["\']?\s*[:=]\s*(?:false|0)\b',
    r'post_approval_enabled["\']?\s*[:=]\s*(?:false|0)\b',
    r'postApproval["\']?\s*[:=]\s*(?:false|0)\b',
    r'approvalRequired["\']?\s*[:=]\s*(?:false|0)\b',
    r'requirePostApproval["\']?\s*[:=]\s*(?:false|0)\b',
]

UNVIEWABLE_PHRASES = [
    "this content isn't available",
    "this content is no longer available",
]

COMPOSER_LABELS = [
    "write something",
    "write a post",
    "what's on your mind",
    "write something…",
    "add a photo",
    "share something with this group",
]


def _check_html_signals(html):
    for pat in APPROVAL_TRUE_PATTERNS:
        if re.search(pat, html, re.IGNORECASE):
            return "skip", "embedded flag: approval required"
    for pat in APPROVAL_FALSE_PATTERNS:
        if re.search(pat, html, re.IGNORECASE):
            return "safe", "embedded flag: approval off"
    return None, None


def _check_text_signals(text):
    low = text.lower()
    for phrase in APPROVAL_PHRASES:
        if phrase in low:
            return "skip", f"page text: '{phrase}'"
    return None, None


def _has_composer(page):
    for label in COMPOSER_LABELS:
        try:
            el = page.query_selector(
                f'[aria-label*="{label}" i], [placeholder*="{label}" i], '
                f'[aria-label="{label}"]'
            )
            if el and el.is_visible():
                return True
        except Exception:
            continue
    return False


def check_group(page, group_id, log=None, goto=True):
    """Check one group and return (status, signal).

    goto=False classifies the page the browser is already on (used right
    after an auto-join click, saving a full revisit).
    """
    log = log or (lambda msg: print(msg))
    if goto:
        url = f"https://www.facebook.com/groups/{group_id}/"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            return "unknown", f"load error: {e}"
    try:
        try:
            page.wait_for_selector('div[role="main"]', timeout=5000)
        except Exception:
            pass
        page.wait_for_timeout(1200)
    except Exception:
        pass

    # Private / unavailable groups never load properly.
    body_text = ""
    try:
        body_text = page.inner_text("body")
    except Exception:
        pass
    for phrase in UNVIEWABLE_PHRASES:
        if phrase in body_text.lower():
            return "skip", f"unviewable: '{phrase}'"

    # Check if we are actually a member BEFORE checking post-approval signals.
    # "Leave group" means we ARE a member (page is valid for posting).
    low = body_text.lower()
    is_member = any(p in low for p in ("leave group", "leave this group"))
    has_join_button = bool(re.search(r"\bjoin group\b", body_text, re.IGNORECASE))

    # Not a member AND no leave-group signal → cannot post here.
    # But only flag as "not a member" if there's no composer visible either
    # (some public groups show a composer to non-members).
    if has_join_button and not is_member:
        if not _has_composer(page):
            return "skip", "not a member"

    html = ""
    try:
        html = page.content()
    except Exception:
        pass

    status, signal = _check_html_signals(html)
    if status:
        return status, signal

    status, signal = _check_text_signals(body_text)
    if status:
        return status, signal

    # Last resort: only call a group safe if its composer is actually present
    # and none of the above signals fired.
    if _has_composer(page):
        return "safe", "composer present, no approval signal"

    return "unknown", "no composer or approval signal found"


MEMBER_PHRASES = ["leave group", "leave this group"]
PENDING_PHRASES = ["cancel request", "request sent", "requested", "pending", "membership request"]
DECLINED_PHRASES = ["join group"]
UNVIEWABLE_PHRASES2 = ["this content isn't available", "this content is no longer available"]


def check_membership_status(page, group_id, log=None):
    """Visit a group page and determine whether we are a member.

    Returns (join_status, detail) where join_status is one of:
        joined, pending, declined, left, unviewable, unknown
    """
    log = log or (lambda msg: print(msg))
    url = f"https://www.facebook.com/groups/{group_id}/"
    try:
        # Read-only navigation. DOMContentLoaded brings back the group HTML
        # which already contains the membership text signals; we do NOT need
        # the full React render (main pane) to detect them, so we skip the
        # wait_for_selector(main) that could add up to 4s per group and rely
        # on a short settle so Playwright has committed the DOM.
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(450)
    except Exception as e:
        return "unknown", f"load error: {e}"

    body = ""
    try:
        body = (page.inner_text("body") or "").lower()
    except Exception:
        return "unknown", "could not read page"

    for phrase in UNVIEWABLE_PHRASES2:
        if phrase in body:
            return "unviewable", phrase

    for phrase in MEMBER_PHRASES:
        if phrase in body:
            return "joined", f"member signal: '{phrase}'"

    for phrase in PENDING_PHRASES:
        if phrase in body:
            return "pending", f"pending signal: '{phrase}'"

    if re.search(r"\bjoin group\b", body):
        return "declined", "join button present — not a member"

    return "unknown", "no clear membership signal found"