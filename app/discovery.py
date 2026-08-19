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
    aria = link.get_attribute("aria-label")
    if aria and aria.strip() and aria.strip().lower() != "group":
        return aria.strip()
    text = link.inner_text().strip()
    if text:
        return text
    return ""


def _collect_links(page, max_scrolls=8, scroll_wait=1200):
    seen = {}
    for _ in range(max_scrolls):
        links = page.query_selector_all('a[href*="/groups/"]')
        for link in links:
            href = link.get_attribute("href") or ""
            gid = _extract_group_id(href)
            if not gid:
                continue
            if gid not in seen:
                name = _group_name_from_link(link)
                card = link.evaluate("el => el.closest('div[role=button], div[aria-label], div')")
                card_text = card["innerText"] if card else ""
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
        page.mouse.wheel(0, 4000)
        page.wait_for_timeout(scroll_wait)
    return list(seen.values())


def search_groups(page, query, filter_mode="search", min_members=0, log=None):
    """Return a list of candidate group dicts matching the query/filter."""
    log = log or (lambda msg: print(msg))
    query = (query or "").strip()
    if not query:
        return []
    url = SEARCH_URL.format(query=quote_plus(query))
    log(f"Searching groups for: {query}")
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    candidates = _collect_links(page)

    if filter_mode == "hard":
        keyword = query.lower()
        matched = []
        for c in candidates:
            name = (c["name"] or "").lower()
            if keyword and keyword not in name:
                continue
            if min_members and c["member_count"] and c["member_count"] < min_members:
                continue
            matched.append(c)
        candidates = matched

    log(f"Found {len(candidates)} groups")
    return candidates