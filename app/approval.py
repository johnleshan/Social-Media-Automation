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


def check_group(page, group_id, log=None):
    """Check one group and return (status, signal)."""
    log = log or (lambda msg: print(msg))
    url = f"https://www.facebook.com/groups/{group_id}/"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
    except Exception as e:
        return "unknown", f"load error: {e}"

    # Private / unavailable groups never load properly.
    body_text = ""
    try:
        body_text = page.inner_text("body")
    except Exception:
        pass
    for phrase in UNVIEWABLE_PHRASES:
        if phrase in body_text.lower():
            return "skip", f"unviewable: '{phrase}'"

    # Not a member -> cannot post.
    if re.search(r"\bjoin group\b", body_text, re.IGNORECASE):
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