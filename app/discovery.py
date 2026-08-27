"""Group discovery via Facebook search.

Two modes:
  * search  : raw query, e.g. "investments kenya" -> any matching groups.
  * hard    : query + the keyword must appear in the group name and (optionally)
              the group must meet a minimum member count.
"""
import re
from urllib.parse import quote_plus

SEARCH_URL = "https://www.facebook.com/search/groups?q={query}"

MEMBER_RE = re.compile(r"([\d.,]+)\s*([KkMm]?)\s*members", re.IGNORECASE)
MEMBER_SHORT_RE = re.compile(r"([\d.,]+)\s*([KkMm])", re.IGNORECASE)


def _parse_member_count(text):
    text = text or ""
    m = MEMBER_RE.search(text)
    if not m:
        m = MEMBER_SHORT_RE.search(text)
    if not m:
        return 0
    num_str = m.group(1).replace(",", "")
    try:
        num = float(num_str)
    except ValueError:
        return 0
    mult = m.group(2).lower()
    if mult == "k":
        num *= 1_000
    elif mult == "m":
        num *= 1_000_000
    return int(num)


def _extract_group_id(href):
    nums = re.findall(r"\d+", href or "")
    if not nums:
        return None
    return max(nums, key=len)


def _group_name_from_link(link):
    aria = (link.get_attribute("aria-label") or "").strip()
    low = aria.lower()
    # avatar links are labelled "Profile photo of <group name>"
    if low.startswith("profile photo of"):
        return aria[len("profile photo of"):].strip()
    if aria and low != "group":
        return aria
    text = link.inner_text().strip()
    if text:
        return text
    return ""


def _collect_links(page, max_scrolls=8, scroll_wait=900):
    seen = {}
    stale_rounds = 0
    for _ in range(max_scrolls):
        added = 0
        links = page.query_selector_all('a[href*="/groups/"]')
        for link in links:
            href = link.get_attribute("href") or ""
            gid = _extract_group_id(href)
            if not gid:
                continue
            if gid not in seen:
                name = _group_name_from_link(link)
                # Climb from the link until a block that mentions "members" is
                # found and use that text for the member count. Done inside the
                # browser so the result is a plain string (no serialization of
                # DOM nodes, which varies across Playwright versions).
                card_text = link.evaluate(
                    "el => {"
                    "  const re = /([\\d.,]+)\\s*[KkMm]?\\s*members?/;"
                    "  let n = el;"
                    "  while (n) {"
                    "    const t = n.innerText || '';"
                    "    if (re.test(t)) return t;"
                    "    n = n.parentElement;"
                    "  }"
                    "  return '';"
                    "}"
                )
                card_text = card_text if isinstance(card_text, str) else ""
                if not name:
                    # pull the first line of the surrounding card text
                    first = next(
                        (l.strip() for l in card_text.splitlines() if l.strip()), ""
                    )
                    name = first
                seen[gid] = {
                    "id": gid,
                    "name": name,
                    "member_count": _parse_member_count(card_text),
                    "url": f"https://www.facebook.com/groups/{gid}/",
                }
                added += 1
        if added == 0:
            stale_rounds += 1
            if stale_rounds >= 2:
                break  # page stopped offering new groups — don't scroll on
        else:
            stale_rounds = 0
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(scroll_wait)
    return list(seen.values())


LOGIN_WALL_PHRASES = [
    "log in to facebook",
    "email address or mobile number",
    "create new account",
    "forgotten password",
]
NOT_FOUND_PHRASES = ["not found", "content isn't available"]


def _body_text(page):
    try:
        return (page.inner_text("body") or "")[:4000]
    except Exception:
        return ""


def search_groups(page, query, filter_mode="search", min_members=0, log=None):
    """Return a list of candidate group dicts matching the query/filter.

    Raises RuntimeError with an actionable message when the search cannot run
    (session not logged in, or Facebook returned a blocked/404 page) instead of
    silently returning zero groups.
    """
    log = log or (lambda msg: print(msg))
    query = (query or "").strip()
    if not query:
        return []
    url = SEARCH_URL.format(query=quote_plus(query))
    log(f"Searching groups for: {query}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        raise RuntimeError(f"search page would not load: {e}")
    # results render via JS — wait for the first group link instead of a fixed nap
    try:
        page.wait_for_selector('a[href*="/groups/"]', timeout=15000)
    except Exception:
        pass
    page.wait_for_timeout(2500)

    body = _body_text(page)
    low = body.lower()
    if any(p in low for p in LOGIN_WALL_PHRASES):
        raise RuntimeError(
            "Facebook is showing the login page. The account session is not "
            "logged in. Log in and re-check the session, then scan again."
        )
    if any(p in low for p in NOT_FOUND_PHRASES) or page.title() == "":
        raise RuntimeError(
            "Facebook returned 'Not Found' for the search page. The session "
            "may be logged out or rate-limited. Re-check the session and retry."
        )

    candidates = _collect_links(page)
    log(f"Found {len(candidates)} candidate group(s) on the search page")

    if min_members:
        before = len(candidates)
        candidates = [
            c for c in candidates
            if not (c["member_count"] and c["member_count"] < min_members)
        ]
        log(f"Min-members ({min_members}) filter: kept {len(candidates)}/{before}")

    if filter_mode == "hard":
        words = [w for w in re.split(r"\s+", query.lower()) if w]
        matched = []
        for c in candidates:
            name = (c["name"] or "").lower()
            if words and not all(w in name for w in words):
                continue
            matched.append(c)
        candidates = matched
        log(f"{len(candidates)} group(s) match the filter")

    log(f"Found {len(candidates)} groups")
    return candidates