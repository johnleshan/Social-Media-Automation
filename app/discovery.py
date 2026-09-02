"""Group discovery via Facebook search.

Two modes:
  * search  : raw query, e.g. "investments kenya" -> any matching groups.
  * hard    : query + the keyword must appear in the group name and (optionally)
              the group must meet a minimum member count.
"""
import datetime
import re
from urllib.parse import quote_plus

SEARCH_URL = "https://www.facebook.com/search/groups?q={query}"
MY_GROUPS_JOINS_URL = "https://www.facebook.com/groups/joins/"
MY_GROUPS_FEED_URL = "https://www.facebook.com/groups/feed/"

RESERVED_GROUP_PATHS = {
    "feed", "joins", "discover", "create", "categories", "notifications",
    "search", "chats", "membership_questions", "member-requests", "settings",
    "edit", "about", "events", "media", "files", "buy_sell_discussion",
    "your_posts", "manage", "permalink", "user", "profile", "browse",
    "my_groups", "explore"
}

# JSON member patterns (Relay/GraphQL embedded states)
JSON_MEMBER_PATTERNS = [
    # Keys with nested {"count": N} or {"total_count": N}
    re.compile(
        r'"(?:group_total_members|group_members|group_members_summary|membership_summary|custom_gender_group_members_summary)":\s*\{\s*"(?:count|total_count)":\s*(\d+)',
        re.IGNORECASE
    ),
    # Keys with direct integer value e.g. "member_count": 12000
    re.compile(
        r'"(?:group_total_members_count|group_member_count|group_members_count|member_count|members_count|total_members|total_member_count|group_membership_count)":\s*(\d+)',
        re.IGNORECASE
    ),
    # Keys with formatted string value e.g. "formatted_member_count": "14.5K" or "subtitle_text": "Public group · 14.5K members"
    re.compile(
        r'"(?:formatted_member_count|member_count_text|membership_summary|subtitle_text|group_privacy_and_member_count_sub_title)":\s*(?:\{\s*"text"\s*:\s*)?"([^"]+)"',
        re.IGNORECASE
    ),
]

TEXT_MEMBER_PATTERNS = [
    # 1. "14.5K members", "14,5K members", "304K members", "1 member", "14,000 members", "14 000 members", "14.5K total members"
    re.compile(
        r'(?:^|[\s·•|,(>\[])([\d\s.,]+)\s*([KkMmBb]?)\s*(?:total\s+)?members?(?:[\s·•|,)<\]\.]|$)',
        re.IGNORECASE
    ),
    # 2. "Members · 14.5K", "Members: 14,000", "Members - 500", "Members (14.5K)", "Members\n14,520"
    re.compile(
        r'members?\s*[:·•\-–—(\s]\s*([\d\s.,]+)\s*([KkMmBb]?)(?:\)|[\s·•|,\]\.]|$)',
        re.IGNORECASE
    ),
    # 3. "Public group · 14.5K", "Private group · 14.5K", "Public · 14.5K", "Private · 14.5K"
    re.compile(
        r'(?:public|private)(?:\s+group)?\s*[·•\-–—]\s*([\d\s.,]+)\s*([KkMmBb]?)',
        re.IGNORECASE
    ),
    # 4. "14.5K people", "14K followers", "14K participants", "14.5K joined"
    re.compile(
        r'(?:^|[\s·•|,(>\[])([\d\s.,]+)\s*([KkMmBb]?)\s*(?:people|followers|participants|joined)(?:[\s·•|,)<\]\.]|$)',
        re.IGNORECASE
    ),
    # 5. "has 15,234 members", "has 15K members"
    re.compile(
        r'has\s+([\d\s.,]+)\s*([KkMmBb]?)\s*members?',
        re.IGNORECASE
    ),
]

_RAW_NUMBER_RE = re.compile(r'^[\s·•\-_]*([\d\s.,]+)\s*([KkMmBb]?)\s*$', re.IGNORECASE)


def _parse_single_number(num_raw, mult=""):
    if not num_raw:
        return 0
    num_raw = str(num_raw).strip()
    mult = (mult or "").lower().strip()

    num_raw = num_raw.replace(" ", "").replace("\xa0", "")
    if not num_raw:
        return 0

    if mult and "," in num_raw and "." not in num_raw:
        parts = num_raw.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            num_raw = num_raw.replace(",", ".")
        else:
            num_raw = num_raw.replace(",", "")
    elif not mult and "." in num_raw and "," not in num_raw:
        parts = num_raw.split(".")
        if len(parts) == 2 and len(parts[1]) == 3 and int(parts[0]) > 0:
            num_raw = num_raw.replace(".", "")
    else:
        num_raw = num_raw.replace(",", "")

    try:
        num = float(num_raw)
    except ValueError:
        return 0

    if mult == "k":
        num *= 1_000
    elif mult == "m":
        num *= 1_000_000
    elif mult == "b":
        num *= 1_000_000_000

    val = int(round(num))
    return val if val > 0 else 0


def _parse_member_count(text):
    if not text:
        return 0
    text = str(text)

    # Check raw standalone number e.g. "14.5K" or "304,000"
    m_raw = _RAW_NUMBER_RE.match(text)
    if m_raw:
        val = _parse_single_number(m_raw.group(1), m_raw.group(2))
        if val > 0:
            return val

    # 1. Check JSON patterns
    for pat in JSON_MEMBER_PATTERNS:
        for m in pat.finditer(text):
            val_str = m.group(1).strip()
            if val_str.isdigit():
                val = int(val_str)
                if val > 0:
                    return val
            else:
                val = _parse_member_count(val_str)
                if val > 0:
                    return val

    # 2. Check Text patterns
    for pat in TEXT_MEMBER_PATTERNS:
        for match in pat.finditer(text):
            num_raw = match.group(1).strip()
            mult = match.group(2) if len(match.groups()) > 1 else ""
            val = _parse_single_number(num_raw, mult)
            if val > 0:
                return val

    return 0


def _extract_group_id(href):
    if not href:
        return None
    # match /groups/<id_or_slug>
    m = re.search(r"/groups/([a-zA-Z0-9._-]+)", href)
    if not m:
        return None
    slug = m.group(1).strip()
    if slug.lower() in RESERVED_GROUP_PATHS:
        return None
    return slug


_MEMBER_SUFFIX_RE = re.compile(r"\s*([\d.,]+\s*[KkMmBb]?\s*members?)", re.IGNORECASE)


def _clean_group_name(name):
    name = name or ""
    # collapse newlines / whitespace runs to single spaces
    name = re.sub(r"\s+", " ", name).strip()
    # cut trailing member-count and "last active ..." chatter
    name = _MEMBER_SUFFIX_RE.sub("", name)
    name = re.sub(r"\s*last active.*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^\s*just now\s*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*just now\s*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*·\s*(?:public|private)\s+group.*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s*·\s*(?:public|private)\b.*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"^\(\d+\)\s*", "", name).strip()
    # Drop generic list/feed titles that leak in from the groups navigation
    # (e.g. "All groups you've joined (75)", "Groups", "Your groups", ...).
    if re.fullmatch(
        r"(?:all groups you'?ve joined|groups|group|your groups|discover|explore"
        r"|suggested|home|facebook)(?:\s*\(\d+\))?\s*",
        name, flags=re.IGNORECASE,
    ):
        return ""
    return name.strip()


_REL_FAST_UNITS = {"min": 0, "mins": 0, "h": 0, "hr": 0, "hrs": 0, "hours": 0, "hour": 0}
_REL_UNITS = {"d": 1, "day": 1, "days": 1, "w": 7, "wk": 7, "week": 7, "weeks": 7,
              "mo": 30, "month": 30, "months": 30, "yr": 365, "y": 365,
              "year": 365, "years": 365}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}
_MONTH_FULL = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"])}
_MONTH_KEYWORDS = sorted(
    set(_MONTH_FULL) | {k + "." for k in _MONTHS} | set(_MONTHS), key=len, reverse=True)
_MONTH_LOOKUP = {}
_MONTH_LOOKUP.update(_MONTHS)
_MONTH_LOOKUP.update(_MONTH_FULL)
for k, v in list(_MONTHS.items()):
    _MONTH_LOOKUP[k + "."] = v


def _days_since(year, month=1, day=1, today=None):
    try:
        d = datetime.date(year, month, day)
        return max(0, (today - d).days if today else (datetime.date.today() - d).days)
    except Exception:
        return None


def _relative_to_days(label):
    """Convert a Facebook relative time token (e.g. '3 h', '2 d', '4 w', '2 mo',
    '1 yr', 'just now', 'Active 2 days ago') to days since. Returns None if
    unparseable."""
    label = label.lower().strip()
    if not label:
        return None
    if label in ("now", "just now", "active now", "now active"):
        return 0
    m = re.match(r"^(?:active|last active)?\s*(\d{1,3})\s*([a-z]+)\s*ago?$", label)
    if not m:
        m = re.match(r"^(\d{1,3})\s*([a-z]+)$", label)
    if not m:
        words = label.split()
        # 'Active 2 days ago' style where words may differ
        m = re.match(r"^active\s+(\d{1,3}|\d{1,2}(?:\.\d)?)\s+([a-z ]+?)\s+ago$", label)
        if m:
            num = float(m.group(1))
            unit = m.group(2).strip().rstrip("s")
            for key, val in list(_REL_UNITS.items()) + list(_REL_FAST_UNITS.items()):
                if unit == key:
                    if val == 0:
                        return 0
                    return int(round(num * val))
        return None
    try:
        num = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).lower().rstrip("s")
    if unit in _REL_FAST_UNITS:
        return 0
    if unit in _REL_UNITS:
        return int(round(num * _REL_UNITS[unit]))
    return None


def _absolute_to_days(text):
    """Parse an absolute date ('5 February 2021', 'Feb 2020', '12 May 2020',
    'September 2020') to days since. Returns None on failure."""
    text = text.lower().strip()
    today = datetime.date.today()
    m = re.match(r"^(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+\.?)\s+(19|20\d{2})$", text)
    if m:
        mon = _MONTH_LOOKUP.get(m.group(2))
        if mon:
            return _days_since(int(m.group(3)), mon, int(m.group(1)), today)
    m = re.match(r"^([a-z]+\.?)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(19|20\d{2})$", text)
    if m:
        mon = _MONTH_LOOKUP.get(m.group(1))
        if mon:
            return _days_since(int(m.group(3)), mon, int(m.group(2)), today)
    m = re.match(r"^([a-z]+\.?)\s+(19|20\d{2})$", text)
    if m:
        mon = _MONTH_LOOKUP.get(m.group(1))
        if mon:
            return _days_since(int(m.group(2)), mon, 1, today)
    return None


def _extract_feed_times(page):
    """Pull the timestamp anchors inside the group feed ('3 h', '2 d', 'Feb 2020')."""
    try:
        return page.evaluate(
            """() => {
                const feed = document.querySelector('div[role="feed"]') || document.body;
                const out = [];
                const re = /(?:just now|\\d{1,3}\\s*(?:minutes?|mins?|hours?|hrs|hr|days?|weeks?|months?|mo|mon|years?|yrs?|d|w)\\b|\\d{1,3}\\s*h\\b|(?:[a-z]{3,9}\\.?)\\s+\\d{1,2}(?:st|nd|rd|th)?(?:,)?\\s+(?:19|20)\\d{2}\\b|\\b(?:19|20)\\d{2}\\b)/i;
                const links = feed.querySelectorAll('a[href*="/groups/"][href*="/permalink/"], a[href*="/posts/"]');
                for (const a of links) {
                    const t = (a.innerText || '').trim();
                    if (t && re.test(t) && t.length < 25) out.push(t);
                }
                if (out.length === 0) {
                    const src = feed.innerText || '';
                    const lines = src.split(/\\n+/);
                    for (const ln of lines) {
                        const t = ln.trim();
                        if (re.test(t) && t.length < 25 && !/members?|people|joined/i.test(t)) { out.push(t); }
                    }
                }
                return out.slice(0, 60);
            }"""
        )
    except Exception:
        return []


def extract_group_activity(page):
    """Estimate a group's activity from its live page.

    Returns {'days': int|None, 'raw': str|None} where `days` is the number of
    days since the newest signal found (posts in the feed or a header
    'Active x ago' label). None means no usable signal was found.
    """
    today = datetime.date.today()
    candidates = []

    # 1. Header/subtitle 'Active ... ago' / 'last active ...'. FB puts the
    #    activity label in the header area near the group name; scan the h1 and
    #    the top of the body to cover subtitle placements.
    try:
        pieces = []
        h1 = page.query_selector("h1, [role='heading'][aria-level='1']")
        if h1:
            pieces.append((_safe_dom_text(h1) or "")[:4000])
        pieces.append((_safe_dom_text(page.locator("body")) or "")[:4000])
        text = "\n".join(p for p in pieces if p)
        for m in re.finditer(
            r"(?:active|last active)\s+(?:([\d.]{1,3})\s*([a-z]+)|(this\s+week|this\s+month|this\s+year|this\s+day|today|now))\s*(?:ago)?\b",
            text.lower(),
        ):
            if m.group(3):
                w = m.group(3)
                days = {"this day": 0, "today": 0, "now": 0, "this week": 7,
                        "this month": 30, "this year": 365}.get(w, 7)
                candidates.append((days, m.group(0)))
            else:
                d = _relative_to_days(f"{m.group(1)} {m.group(2)} ago")
                if d is not None:
                    candidates.append((d, m.group(0)))
    except Exception:
        pass

    # 2. Feed timestamps (most recent visible posts)
    try:
        for token in _extract_feed_times(page):
            low = token.lower()
            if low in ("now", "just now"):
                candidates.append((0, low))
                continue
            d = _relative_to_days(low)
            if d is not None:
                candidates.append((d, low))
                continue
            d = _absolute_to_days(low)
            if d is not None:
                candidates.append((d, low))
    except Exception:
        pass

    # 3. 'Active this month'/'this week' style header plus bare years as a
    #    coarse upper bound: a 2020 group is dead even without explicit posts.
    for m in re.finditer(r"\b(19|20\d{2})\b", (_safe_dom_text(page) or "")[:20000]):
        try:
            yr = int(m.group(1) or m.group(0))
        except ValueError:
            continue
        if 1995 <= yr <= today.year:
            candidates.append((_days_since(yr, 12, 31, today), str(yr)))

    if not candidates:
        return {"days": None, "raw": None}
    days = max(0, min(c for c, _raw in candidates))
    raw = min(candidates, key=lambda x: x[0])[1]
    return {"days": days, "raw": raw}


def _safe_dom_text(node):
    try:
        return node.inner_text() or ""
    except Exception:
        return ""


def extract_group_info_from_page(page, fallback_about=True):
    """Extract (name, member_count) from a live group page."""
    name = ""
    member_count = 0

    # Ensure page header or main area has loaded
    try:
        page.wait_for_selector('h1, [role="heading"][aria-level="1"], div[role="main"]', timeout=3500)
    except Exception:
        pass

    # 1. Group Name
    try:
        h1 = page.query_selector('h1, div[role="main"] h1, [role="heading"][aria-level="1"]')
        if h1:
            name = (h1.inner_text() or "").strip()
    except Exception:
        pass

    if not name:
        try:
            og_title = page.evaluate(
                "() => document.querySelector('meta[property=\"og:title\"]')?.getAttribute('content') || ''"
            )
            if og_title and og_title.lower() != "facebook":
                name = og_title.split("|")[0].split("·")[0].strip()
        except Exception:
            pass

    if not name:
        try:
            title = page.title() or ""
            clean_title = re.sub(r"^\(\d+\)\s*", "", title)
            clean_title = clean_title.split("|")[0].split("·")[0].strip()
            if clean_title and clean_title.lower() not in ("facebook", "groups", "log in to facebook", "log in"):
                name = clean_title
        except Exception:
            pass

    name = _clean_group_name(name)

    # 2. Member Count - Targeted DOM extraction (header, subtitle, member links, tab items)
    try:
        dom_member_text = page.evaluate("""() => {
            // 1. Direct member links: <a href="/groups/.../members/">...</a> or /people
            const memberLinks = Array.from(document.querySelectorAll('a[href*="/members"], a[href*="/people"], a[role="tab"]'));
            for (const a of memberLinks) {
                const t = (a.innerText || '') + ' ' + (a.getAttribute('aria-label') || '');
                if (/(?:[\\d\\s.,]+[KkMmBb]?\\s*(?:total\\s+)?members?|members?[\\s:·•\\-–—(\\s]+[\\d\\s.,]+|(?:public|private)(?:\\s+group)?\\s*[·•\\-–—]\\s*[\\d\\s.,]+|[\\d\\s.,]+[KkMmBb]?\\s*(?:people|followers|participants|joined))/i.test(t)) {
                    return t;
                }
            }
            // 2. Subtitle / Header container near h1
            const h1 = document.querySelector('h1, [role="heading"][aria-level="1"]');
            if (h1) {
                let curr = h1;
                for (let i = 0; i < 15 && curr && curr.tagName !== 'BODY'; i++) {
                    const text = curr.innerText || '';
                    if (/(?:[\\d\\s.,]+[KkMmBb]?\\s*(?:total\\s+)?members?|members?[\\s:·•\\-–—(\\s]+[\\d\\s.,]+|(?:public|private)(?:\\s+group)?\\s*[·•\\-–—]\\s*[\\d\\s.,]+|[\\d\\s.,]+[KkMmBb]?\\s*(?:people|followers|participants|joined))/i.test(text)) {
                        return text;
                    }
                    curr = curr.parentElement;
                }
            }
            // 3. Header area / role="main"
            const main = document.querySelector('div[role="main"]');
            if (main) {
                const text = main.innerText || '';
                if (/members?/i.test(text)) return text.slice(0, 10000);
            }
            return '';
        }""")
        if dom_member_text:
            member_count = _parse_member_count(dom_member_text)
    except Exception:
        pass

    # 3. Member Count - Meta tags
    if not member_count:
        try:
            meta_desc = page.evaluate(
                "() => document.querySelector('meta[name=\"description\"], meta[property=\"og:description\"], meta[name=\"twitter:description\"]')?.getAttribute('content') || ''"
            )
            if meta_desc:
                member_count = _parse_member_count(meta_desc)
        except Exception:
            pass

    # 4. Member Count - Embedded JSON in HTML scripts
    if not member_count:
        try:
            scripts_text = page.evaluate("""() => {
                const scripts = Array.from(document.querySelectorAll('script[type="application/json"], script:not([src])'));
                return scripts.map(s => s.textContent || '').join('\\n');
            }""")
            if scripts_text:
                member_count = _parse_member_count(scripts_text)
        except Exception:
            pass

    # 5. Member Count - Body text
    if not member_count:
        try:
            body = (page.inner_text("body") or "")[:15000]
            member_count = _parse_member_count(body)
        except Exception:
            pass

    # 6. Member Count - Full HTML content
    if not member_count:
        try:
            html = page.content()
            member_count = _parse_member_count(html)
        except Exception:
            pass

    # 7. Fallback to /about/ sub-page if still 0
    if not member_count and fallback_about:
        try:
            current_url = page.url or ""
            if "/groups/" in current_url and not current_url.rstrip("/").endswith("/about"):
                about_url = current_url.split("?")[0].rstrip("/") + "/about/"
                page.goto(about_url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1200)
                about_info = extract_group_info_from_page(page, fallback_about=False)
                if about_info.get("member_count"):
                    member_count = about_info["member_count"]
                if not name and about_info.get("name"):
                    name = about_info["name"]
        except Exception:
            pass

    return {"name": name, "member_count": member_count}


def _collect_links(page, max_scrolls=8, scroll_wait=900, no_early_stop=False,
                   should_stop=None, should_pause=None):
    """Scrape group links/cards from a page that loads more on scroll.

    `should_stop()` and `should_pause()` (optional) let a caller interrupt a
    long dev-mode sweep (500 scrolls) so the worker can react to Stop/Pause,
    instead of being wedged inside Playwright for minutes.
    """
    should_stop = should_stop or (lambda: False)
    should_pause = should_pause or (lambda: False)
    seen = {}
    stale_rounds = 0
    for r in range(max_scrolls):
        if should_stop():
            break
        added = 0
        raw_items = page.evaluate("""() => {
            const links = Array.from(document.querySelectorAll('a[href*="/groups/"]'));
            const results = [];
            const memberRe = /(?:[\\d\\s.,]+[KkMmBb]?\\s*(?:total\\s+)?members?|members?[\\s:·•\\-–—(\\s]+[\\d\\s.,]+|(?:public|private)(?:\\s+group)?\\s*[·•\\-–—]\\s*[\\d\\s.,]+|[\\d\\s.,]+[KkMmBb]?\\s*(?:people|followers|participants|joined))/i;
            const stopTags = new Set(['BODY', 'HTML']);

            for (const link of links) {
                const href = link.getAttribute('href') || '';
                if (!href) continue;

                let aria = (link.getAttribute('aria-label') || '').trim();
                let inner = (link.innerText || '').trim();
                
                let cardText = '';
                let n = link;
                for (let i = 0; i < 12 && n && !stopTags.has(n.tagName); i++) {
                    const role = n.getAttribute('role') || '';
                    if (role === 'feed' || role === 'main') break;
                    const t = n.innerText || '';
                    if (memberRe.test(t)) {
                        cardText = t;
                        break;
                    }
                    if (!cardText && t.length > 5 && t.length < 500) {
                        cardText = t;
                    }
                    n = n.parentElement;
                }

                if (!memberRe.test(cardText) && link.parentElement) {
                    const parentText = link.parentElement.innerText || '';
                    if (memberRe.test(parentText)) {
                        cardText = parentText;
                    }
                }

                results.push({
                    href: href,
                    aria: aria,
                    inner: inner,
                    cardText: cardText
                });
            }
            return results;
        }""")

        raw_items = raw_items if isinstance(raw_items, list) else []
        for item in raw_items:
            href = item.get("href") or ""
            gid = _extract_group_id(href)
            if not gid:
                continue

            aria = item.get("aria") or ""
            inner = item.get("inner") or ""
            card_text = item.get("cardText") or ""

            # Name extraction
            name = ""
            if aria:
                low_aria = aria.lower()
                if low_aria.startswith("profile photo of"):
                    name = aria[len("profile photo of"):].strip()
                elif low_aria not in ("group", "groups", "facebook"):
                    name = aria
            if not name and inner:
                name = inner
            if not name and card_text:
                first = next((l.strip() for l in card_text.splitlines() if l.strip()), "")
                name = first
            name = _clean_group_name(name)

            member_count = _parse_member_count(card_text)

            if gid not in seen:
                seen[gid] = {
                    "id": gid,
                    "name": name or gid,
                    "member_count": member_count,
                    "url": f"https://www.facebook.com/groups/{gid}/",
                }
                added += 1
            else:
                # Upgrade existing if we found better data
                if (not seen[gid]["name"] or seen[gid]["name"] == gid or seen[gid]["name"] == "Just now") and name and name != gid and name != "Just now":
                    seen[gid]["name"] = name
                if seen[gid]["member_count"] == 0 and member_count > 0:
                    seen[gid]["member_count"] = member_count

        if added == 0:
            stale_rounds += 1
            if stale_rounds >= 3 and not no_early_stop:
                break
        else:
            stale_rounds = 0

        page.evaluate(
            "() => {"
            "  const els = [...document.querySelectorAll('div')].filter(el => {"
            "    const s = getComputedStyle(el);"
            "    return (s.overflowY === 'auto' || s.overflowY === 'scroll')"
            "      && el.scrollHeight > el.clientHeight + 50;"
            "  }).sort((a, b) => b.scrollHeight - a.scrollHeight);"
            "  if (els.length > 0) {"
            "    els[0].scrollTop += 3000;"
            "  }"
            "  window.scrollBy(0, 2000);"
            "  if (document.scrollingElement) {"
            "    document.scrollingElement.scrollTop += 2000;"
            "  }"
            "}"
        )
        # Chunked wait so a long dev-mode sweep stays responsive to Stop/Pause.
        remaining_ms = max(0, scroll_wait)
        while remaining_ms > 0 and not should_stop():
            if should_pause():
                import time as _time
                _time.sleep(0.3)
                continue
            page.wait_for_timeout(min(500, remaining_ms))
            remaining_ms -= 500
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
    """Return a list of candidate group dicts matching the query/filter."""
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


def list_my_groups(page, log=None, max_scrolls=100, scroll_wait=650, no_early_stop=False,
                   should_stop=None, should_pause=None):
    """Scrape the FULL list of groups the account is a member of.

    Checks /groups/joins/ (dedicated list with group cards and member counts)
    and /groups/feed/ to ensure full coverage.
    """
    log = log or (lambda msg: print(msg))
    groups_map = {}

    for url in (MY_GROUPS_JOINS_URL, MY_GROUPS_FEED_URL):
        try:
            log(f"Loading groups from {url}...")
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector('a[href*="/groups/"]', timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)

            low = _body_text(page).lower()
            if any(p in low for p in LOGIN_WALL_PHRASES):
                raise RuntimeError(
                    "Facebook is showing the login page. The account session is not "
                    "logged in. Log in and re-check the session, then sync again."
                )
            if any(p in low for p in NOT_FOUND_PHRASES) or page.title() == "":
                log(f"Page {url} returned not found or empty title; trying next source...")
                continue

            collected = _collect_links(
                page, max_scrolls=max_scrolls, scroll_wait=scroll_wait,
                no_early_stop=no_early_stop,
                should_stop=should_stop, should_pause=should_pause,
            )
            for g in collected:
                gid = g["id"]
                if gid not in groups_map:
                    groups_map[gid] = g
                else:
                    if (not groups_map[gid]["name"] or groups_map[gid]["name"] == gid) and g["name"] and g["name"] != gid:
                        groups_map[gid]["name"] = g["name"]
                    if groups_map[gid]["member_count"] == 0 and g["member_count"] > 0:
                        groups_map[gid]["member_count"] = g["member_count"]

            log(f"Found {len(groups_map)} total group(s) so far")
            # If joins redirected to feed or gave all groups, we can stop early
            if len(groups_map) > 0 and not no_early_stop:
                if url == MY_GROUPS_JOINS_URL and page.url.rstrip("/").endswith("/groups/feed"):
                    break
        except RuntimeError:
            raise
        except Exception as e:
            log(f"Note: Could not scrape {url}: {e}")
            continue

    log(f"Scraped your Groups feed: {len(groups_map)} group(s)")
    return list(groups_map.values())