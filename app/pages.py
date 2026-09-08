"""Facebook Page discovery + identity switching.

Pages are scraped live from the logged-in browser session (there is no public
API for the pages an account manages). ``list_account_pages`` opens the
"Your Pages" landing and collects the Page cards; ``activate_page`` visits a
Page and accepts Facebook's "Switch now" prompt, which flips the *whole
session's voice* to that Page — so group syncing, posting, scanning and
joining all run as the Page, exactly like the manual page-post path.

Storage note: ``pages`` and ``active_page`` live in each account's config
entry (config.json), *not* the database. That keeps the schema frozen, so a
v1 -> v2 upgrade carries the data across with no migration.
"""
import re

PAGES_LANDING = "https://www.facebook.com/pages/?category=your_pages"

# Anchor paths that are clearly navigation, not a personal page.
RESERVED_PATH_SEGMENTS = {
    "home", "groups", "friends", "photos", "watch", "marketplace", "pages",
    "bookmarks", "messages", "notifications", "login", "settings", "me",
    "profile", "events", "games", "stories", "live", "reels", "find_friends",
    "help", "search", "saved", "videos", "notes", "likes", "checkpoint",
    "confirm", "privacy", "about", "policy", "recommendations", "fundraisers",
    "places", "selltab", "shops", "story", "watchparty", "comment", "m",
    "php", "l.php", "tr", "hashtag", "share", "dialog", "redirect", "browse",
    "category", "your_pages", "bookmark", "recent", "activity",
}

_SCRAPE_JS = r"""
() => {
  const seen = new Set();
  const out = [];
  const near = (a) => {
    let el = a, t = "";
    for (let i = 0; i < 7 && el && el !== document.body; i++) {
      el = el.parentElement;
      if (!el) break;
      t = (el.innerText || "").replace(/\s+/g, " ").trim();
      if (t.length > 2) break;
    }
    return t.slice(0, 140);
  };
  for (const a of document.querySelectorAll("a[href]")) {
    const href = a.href || "";
    let host = "";
    try { host = new URL(href).hostname.replace(/^(www|m|mbasic)\./, "").toLowerCase(); }
    catch (e) { continue; }
    if (host !== "facebook.com" && !host.endsWith(".facebook.com")) continue;
    if (seen.has(href)) continue;
    seen.add(href);
    out.push({ href: href, text: near(a) });
  }
  return out;
}
""".strip()

_ID_RE = re.compile(r"profile\.php\?id=(\d+)")
_SLUG_RE = re.compile(r"facebook\.com/(?:pages?/)?([A-Za-z0-9._-]+)/?$")


def _page_from_url(href):
    """Turn an anchor href into {name, url, id} or None if it is not a Page."""
    href = (href or "").strip()
    m = _ID_RE.search(href)
    if m:
        pid = m.group(1)
        return {
            "id": pid,
            "url": f"https://www.facebook.com/profile.php?id={pid}",
            "name": None,
        }
    m = _SLUG_RE.search(href)
    if m:
        slug = m.group(1)
        if slug.lower() in RESERVED_PATH_SEGMENTS or slug.isdigit():
            return None
        if len(slug) > 60:
            return None
        return {
            "id": slug,
            "url": f"https://www.facebook.com/{slug}",
            "name": slug,
        }
    return None


def _display_name(text):
    """First meaningful line of a Page card's text (Facebook's layout puts the
    Page name first), or None."""
    for line in (text or "").splitlines():
        line = line.strip().strip("\u2022").strip()
        if len(line) >= 2 and line.lower() not in ("switch", "switch now"):
            return line[:60]
    return None


def _extract_pages(page, log):
    pages = {}
    try:
        anchors = page.evaluate(_SCRAPE_JS)
    except Exception as e:
        log(f"Could not read the Pages screen: {e}")
        return []
    for a in anchors or []:
        href = (a.get("href") or "").strip()
        if not href:
            continue
        info = _page_from_url(href)
        if not info:
            continue
        name = _display_name(a.get("text") or "") or info["name"]
        if not name:
            continue
        key = info["url"].lower()
        # prefer the entry that found a real display name
        if key in pages and pages[key]["name"] != pages[key].get("id"):
            continue
        pages[key] = {"name": name, "url": info["url"], "id": info["id"]}
    return sorted(pages.values(), key=lambda p: (p["name"] or "").lower())


def list_account_pages(page, log):
    """Scrape the Pages an account manages. Returns [{name, url, id}, ...]."""
    try:
        page.goto(PAGES_LANDING, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
    except Exception as e:
        log(f"Could not open the Pages manager: {e}")
        return []
    pages = _extract_pages(page, log)
    log(
        f"Found {len(pages)} Page(s) on this account."
        if pages else
        "No Pages found yet. Create a Page on Facebook, then Find my pages again."
    )
    return pages


_ACTIVATE_JS = r"""
() => {
  const nodes = document.querySelectorAll(
    '[role="button"], button, [aria-label], a'
  );
  for (const el of nodes) {
    const label = (el.getAttribute("aria-label") || el.innerText || "").trim();
    if (/^switch now$/i.test(label)) { el.click(); return true; }
  }
  return false;
}
""".strip()


def activate_page(page, page_url, log):
    """Visit ``page_url`` and accept the 'Switch now' prompt so the whole
    session speaks as that Page. Returns True when the Page was reached."""
    page_url = str(page_url or "").strip()
    if not page_url:
        return False
    try:
        page.goto(page_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
    except Exception as e:
        log(f"Could not open the Page to switch into it: {e}")
        return False
    clicked = False
    try:
        clicked = bool(page.evaluate(_ACTIVATE_JS))
    except Exception:
        clicked = False
    if clicked:
        page.wait_for_timeout(4000)
        log("Switched into the Page (Switch now accepted).")
    else:
        log("Page opened; no 'Switch now' prompt (already speaking as this Page).")
    return True