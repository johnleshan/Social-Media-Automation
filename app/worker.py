"""Continuous run-until-stopped worker.

Owns the browser session and the posting loop. Supports three jobs:
  * run  : optionally scan for new groups, then post continuously until stop().
  * scan : discover + approval-check groups only, then close the browser.
  * join : auto-join discovered groups, then close the browser.

All browser work happens on the worker thread so Playwright's sync API stays
on a single thread and only one browser session exists at a time.

Job progress is persisted to the database (run_state) so an interrupted run
(e.g. power loss) can be resumed from where it stopped.
"""
import json
import os
import random
import re
import threading
import time
from datetime import datetime, date

from .approval import check_group, check_membership_status
from .browser import FacebookBrowser
from .database import STATUS_SAFE, STATUS_SKIP, STATUS_UNKNOWN, JOIN_NOT_JOINED, JOIN_PENDING, JOIN_JOINED, JOIN_DECLINED
from .discovery import (search_groups, list_my_groups, extract_group_info_from_page,
                        extract_group_activity)
from .joiner import join_group
from .poster import post_media, post_media_to_page, group_is_page_postable

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".wmv", ".m4v"}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

CHECKPOINT_PHRASES = [
    "confirm your identity",
    "reviewed your account",
    "suspicious activity",
    "account confirmation",
    "we need to confirm",
]

# List/feed page titles that are NOT a single group's name. When the enrichment
# bounces off a group URL onto one of these, the extracted "name" is garbage.
_GENERIC_TITLE_RE = re.compile(
    r"^(?:all groups you'?ve joined|groups|group|your groups|discover|explore"
    r"|suggested|home|facebook|notifications|unread)\b.*",
    re.IGNORECASE,
)


def _is_generic_title(name):
    name = (name or "").strip()
    if not name:
        return True
    if _GENERIC_TITLE_RE.match(name):
        return True
    # numeric-only names are usually group ids, not real names
    if re.fullmatch(r"\d[\d\s.,]*", name):
        return True
    return False


# Keyword tables for the niche filter (lower-cased, substring matching).
# The post niche is online earning / side hustles / jobs / business.
NICHE_KEYWORDS = [
    "online", "earn", "earning", "income", "hustl", "side hustle", "money",
    "digital", "cash", "business", "job", "jobs", "work", "freelanc",
    "entrepreneur", "startup", "invest", "marketing", "affiliate", "network",
    "profit", "sales", "trading", "paypal", "remit", "cashapp", "moneypay",
    "opportunit", "gig", "market", "ecommerce", "shop",
]
# Kenya / East-Africa group names rank higher (the user only wants Kenya).
KENYA_KEYWORDS = [
    "kenya", "kenyan", "nairobi", "nakuru", "mombasa", "kisumu", "eldoret",
    "thika", "naivasha", "machakos", "meru", "kitale", "garissa", "uganda",
    "ugandan", "tanzania", "tanzanian", "east africa", "africa",
]
# Other-region groups are hard-excluded; we only post to Kenyan-region groups.
OTHER_REGION_KEYWORDS = [
    "india", "indian", "pakistan", "bangladesh", "nepal", "sri lanka", "nigeria",
    "nigerian", "ghana", "ghanian", "south africa", "ethiopia", "egypt",
    "morocco", "tunisia", "united states", "u.s.", "usa", "america", "canada",
    "mexico", "brazil", "argentina", "uk", "britain", "british", "england",
    "london", "europe", "france", "french", "germany", "italy", "spain",
    "russia", "turkey", "philippines", "indonesia", "malaysia", "singapore",
    "uae", "dubai", "saudi", "qatar", "kuwait", "australia", "new zealand",
]
# Mega / highly moderated communities escalate posts to reviews and decline
# Page posts; skip them so the batch targets small, low-regulation groups.
MEGA_KEYWORDS = [
    "university", "universities", "students", "campus", "school", "college",
    "church", "churches", "gospel", "christian", "fellowship", "ministry",
    "islam", "muslim", "mosque",
]
# Clearly off-topic communities (fun, retail, maybe items, etc.).
OFF_TOPIC_KEYWORDS = [
    "memes", "jokes", "comedy", "funny", "poems", "entertainment", "movies",
    "gaming", "football", "sports", "recipes", "cooking", "fashion", "beaut",
    "music", "dancing", "photography", "second hand", "buy and sell", "maisha yo",
    "fun times",
]
# Generic / list-page names that pollute the pool (from broken sync extraction).
JUNK_NAME_TOKENS = ["notifications", "unread", "all groups", "your groups",
                    "suggested", "discover"]


def _niche_score_name(name):
    """Score a raw group name for niche relevance + region.
    Returns (niche_score, region_score) or None if the name disqualifies it."""
    low = (name or "").lower().strip()
    if not low or _is_generic_title(name):
        return None
    if any(t in low for t in JUNK_NAME_TOKENS):
        return None
    if any(k in low for k in OTHER_REGION_KEYWORDS):
        return None
    if any(k in low for k in MEGA_KEYWORDS) or any(k in low for k in OFF_TOPIC_KEYWORDS):
        return None
    niche_hits = sum(2 for k in NICHE_KEYWORDS if k in low)
    region_hits = sum(3 for k in KENYA_KEYWORDS if k in low)
    if not niche_hits:
        return None
    return niche_hits, region_hits


def _looks_like_group_page(final_url, target_id):
    """Best-effort check that `final_url` is a single group page (not the
    groups feed / joins list) that at least plausibly matches the target."""
    if not final_url:
        return False
    m = re.search(r"/groups/([a-zA-Z0-9._-]+)", final_url)
    if not m:
        return False
    seg = m.group(1).lower()
    reserved = {
        "feed", "joins", "discover", "create", "categories", "notifications",
        "search", "chats", "membership_questions", "member-requests", "settings",
        "edit", "about", "events", "media", "files", "buy_sell_discussion",
        "your_posts", "manage", "permalink", "user", "profile", "browse",
        "my_groups", "explore",
    }
    if seg in reserved:
        return False
    # If the landed segment is a plain numeric id, we want it to match the
    # target group id (slug segments can't be compared directly).
    if seg.isdigit():
        return seg == str(target_id).strip()
    # Non-numeric slug: assume it's a real group (best effort).
    return True


class Worker:
    def __init__(self, config, db, log=None, notify=None):
        self.config = config
        self.db = db
        self.log = log or (lambda msg: print(msg))
        self.notify = notify
        self._stop = threading.Event()
        self._pause = threading.Event()  # set = paused
        self._thread = None
        self.browser = None
        self.job = None
        self.busy = False
        self.paused = False
        self._lock = threading.Lock()
        self.status_text = ""
        self.stage = None
        self.stage_detail = ""
        self.last_result = None
        self._final_summary = ""
        self._posted_this_run = 0
        self._pending_caption = ""  # browser-held caption for the current run
        self.blast_posted = []  # groups actually posted in the current batch cycle
        self._last_post_url = None

    # ---- public API ----
    def is_busy(self):
        return self.busy

    def start_run(self, profile_name, keyword="", filter_mode="search",
                  min_members=0, caption=""):
        return self._begin("run", profile_name, keyword, filter_mode, min_members, caption)

    def start_scan(self, profile_name, keyword, filter_mode="search",
                   min_members=0):
        return self._begin("scan", profile_name, keyword, filter_mode, min_members, "")

    def start_join(self, profile_name):
        return self._begin("join", profile_name, "", "search", 0, "")

    def start_check(self, profile_name):
        return self._begin("check", profile_name, "", "search", 0, "")

    def start_sync(self, profile_name):
        """Sync the FULL list of groups the account is a member of from the
        live Facebook Groups feed (Option A auto-scroll scrape)."""
        return self._begin("sync", profile_name, "", "search", 0, "")

    def start_blast(self, profile_name, batch_size=10, caption=""):
        """Sync my groups, then post the ready-made content to one batch of
        groups (batch_size per press). Durable resume continues next press."""
        try:
            self.db.set_state("blast_batch", str(max(1, int(batch_size) or 10)))
        except Exception:
            pass
        return self._begin("blast", profile_name, "", "search", 0, caption)

    def start_page_post(self, profile_name, page_url, caption="", file_name=""):
        """Post a media file to an owned Facebook Page."""
        self._page_url_override = page_url
        self._page_file_override = str(file_name or "").strip()
        return self._begin("page_post", profile_name, "", "search", 0, caption)

    def stop(self):
        self._stop.set()
        self._pause.clear()  # unpause so thread can exit
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)

    def pause(self):
        """Pause the current job (stops between iterations)."""
        if self.busy and not self._stop.is_set():
            self._pause.set()
            self.paused = True
            self.status_text = "Paused"
            return True
        return False

    def unpause(self):
        """Resume a paused job."""
        if self.paused:
            self._pause.clear()
            self.paused = False
            self.status_text = "Resuming..."
            return True
        return False

    # ---- internals ----
    def _begin(self, job, profile_name, keyword, filter_mode, min_members, caption):
        with self._lock:
            if self.busy:
                self.log("A job is already running. Stop it first.")
                return False
            self.busy = True
        self._stop.clear()
        self._pause.clear()
        self.paused = False
        self.job = job
        self.status_text = "Starting..."
        self.stage = None
        self.stage_detail = ""
        self.last_result = None
        self._final_summary = ""
        self._posted_this_run = 0
        try:
            self.db.set_state("active_job", json.dumps({
                "type": job,
                "profile": profile_name,
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }))
        except Exception:
            pass
        self._thread = threading.Thread(
            target=self._run_job,
            args=(profile_name, keyword, filter_mode, min_members, caption),
            daemon=True,
        )
        self._thread.start()
        return True

    def _emit(self, msg):
        try:
            self.log(msg)
        except Exception:
            pass

    def _set_stage(self, stage, detail="", status_text=None):
        """Canonical progress checkpoint, read by the web UI journey view."""
        self.stage = stage
        self.stage_detail = detail
        if status_text is not None:
            self.status_text = status_text

    def _page_wait(self, ms):
        """Chunked page.wait_for_timeout that reacts to Stop (~0.5s) and Pause."""
        if not self.browser or not getattr(self.browser, "page", None):
            return
        end = time.time() + max(0, ms) / 1000.0
        # Park while paused (fast spin on _pause), still responsive to Stop.
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.2)
            end = time.time() + max(0, ms) / 1000.0  # restart wait after resume
        while not self._stop.is_set():
            remaining_ms = int((end - time.time()) * 1000)
            if remaining_ms <= 0:
                return
            if self._pause.is_set():
                end = time.time() + max(0, ms) / 1000.0
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.2)
                end = time.time() + max(0, ms) / 1000.0
            self.browser.page.wait_for_timeout(min(250, remaining_ms))

    def _run_job(self, profile_name, keyword, filter_mode, min_members, caption):
        error = None
        started_at = datetime.now().isoformat(timespec="seconds")
        self._pending_caption = str(caption or "").strip()
        try:
            self._set_stage("launch", f"Preparing Chrome ({profile_name})",
                            "Launching Chrome...")
            profile = self.config.get_profile(profile_name)
            if not profile:
                self._set_stage("done", "", f"No account named '{profile_name}'.")
                self._final_summary = f"No account named '{profile_name}'."
                self._emit(f"No account profile named '{profile_name}'. Add it first.")
                return
            settings = self.config.settings
            self.browser = FacebookBrowser(
                profile["user_data_dir"],
                headless=bool(settings.get("headless", True)),
                log=self._emit,
            )
            self.browser.launch()
            try:
                self.browser.page.set_default_timeout(15000)
            except Exception:
                pass
            if not self.browser.is_logged_in():
                self._set_stage("done", "", "Not logged into Facebook.")
                self._final_summary = "Not logged into Facebook on this profile."
                self._emit(
                    "Not logged into Facebook on this profile. "
                    "Open the account in the GUI and log in once, then retry."
                )
                return

            if keyword:
                self._scan_groups(keyword, filter_mode, min_members)

            if self.job == "join":
                self._join_groups()
                self._emit("Auto-join complete.")
                return

            if self.job == "check":
                self._check_join_status()
                self._emit("Join-status check complete.")
                return

            if self.job == "sync":
                self._sync_my_groups()
                self._emit("Group sync complete.")
                return

            if self.job == "blast":
                self._blast_groups()
                self._emit("Batch posting complete.")
                return

            if self.job == "page_post":
                self._page_post(self._page_url_override)
                self._emit("Page posting complete.")
                return

            if self.job == "scan":
                self._emit("Scan complete.")
                return

            if not keyword and settings.get("scan_on_start", True):
                self._scan_groups("", "search", 0)

            self._emit("Posting loop started. Press Stop to end it.")
            self._posting_loop(caption)

        except Exception as e:
            error = e
            self._emit(f"Worker error: {e}")
            self._final_summary = f"Job failed: {e}"
            if self.notify:
                self.notify("Job error", str(e)[:120])
        finally:
            self._close_browser()
            summary = self._final_summary or (
                f"Job failed: {error}" if error else "Finished."
            )
            cancelled = self._stop.is_set()
            if cancelled:
                status = "cancelled"
            elif error or summary.startswith(("No account", "Not logged", "Job failed")):
                status = "failed"
            else:
                status = "finished"
            try:
                prog_raw = self.db.get_state("join_progress", "") or ""
                prog = json.loads(prog_raw) if prog_raw else {}
                if not isinstance(prog, dict):
                    prog = {}
            except Exception:
                prog = {}
            self.db.add_history(
                self.job,
                profile_name,
                status,
                summary,
                prog.get("done") if isinstance(prog.get("done"), int) else None,
                prog.get("total") if isinstance(prog.get("total"), int) else None,
                started_at,
                datetime.now().isoformat(timespec="seconds"),
            )
            self.stage = "done"
            self.stage_detail = ""
            self.status_text = summary
            self.last_result = {
                "job": self.job,
                "ok": error is None and not summary.startswith(("No account", "Not logged")),
                "title": {
                    "join": "Auto-Join finished",
                    "scan": "Scan finished",
                    "run": "Posting run ended",
                    "check": "Join-status check finished",
                    "sync": "Group sync finished",
                    "blast": "Batch posting finished",
                }.get(self.job, "Job finished"),
                "summary": summary,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "seen": False,
            }
            with self._lock:
                self.busy = False
            try:
                self.db.set_state("active_job", "")
            except Exception:
                pass
            self._emit("Worker stopped.")

    def _close_browser(self):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None

    # ---- discovery + approval ----
    def _scan_groups(self, keyword, filter_mode, min_members):
        if keyword:
            self._set_stage("search", f'"{keyword}"',
                            f'Searching Facebook for "{keyword}"...')
            try:
                candidates = search_groups(
                    self.browser.page, keyword, filter_mode, min_members, self._emit
                )
            except Exception as e:
                self._set_stage("done", "", "Search failed.")
                self._final_summary = f"Search failed: {e}"
                self._emit(f"Search failed: {e}")
                self._emit("Scan aborted. Fix the issue above, then scan again.")
                return
            for c in candidates:
                self.db.upsert_group(c["id"], c["name"], c["member_count"], c["url"])
        else:
            self._set_stage("search", "unclassified groups",
                            "Checking unclassified groups...")
            candidates = self.db.get_groups(STATUS_UNKNOWN)
            self._emit(f"Checking {len(candidates)} unclassified group(s).")

        to_check = [c for c in candidates if self._needs_check(c["id"])]
        for i, c in enumerate(to_check, 1):
            if self._stop.is_set():
                break
            name = c.get("name") or c["id"]
            self._set_stage("check", f"[{i}/{len(to_check)}] {name}",
                            f"Checking safety: {name} ({i}/{len(to_check)})")
            status, signal = check_group(self.browser.page, c["id"], self._emit)
            try:
                info = extract_group_info_from_page(self.browser.page)
                if info.get("name") or info.get("member_count"):
                    self.db.upsert_group(c["id"], name=info.get("name"), member_count=info.get("member_count"))
            except Exception:
                pass
            self.db.set_group_status(c["id"], status, signal)
            self._emit(
                f"[check {i}/{len(to_check)}] {c.get('name') or c['id']} "
                f"-> {status} ({signal})"
            )
            self._page_wait(random.randint(600, 1200))

        safe = self.db.count_groups(STATUS_SAFE)
        skip = self.db.count_groups(STATUS_SKIP)
        unknown = self.db.count_groups(STATUS_UNKNOWN)
        self._final_summary = (
            f"{len(candidates)} group(s) found · {len(to_check)} checked · "
            f"{safe} safe"
        )
        self._emit(f"Queue ready: {safe} safe, {skip} skip, {unknown} unknown.")
        if self.notify:
            self.notify("Scan finished", f"Found {len(candidates)} groups, {len(to_check)} checked")

    def _needs_check(self, group_id):
        g = self.db.get_group(group_id)
        return g is None or g["status"] in (STATUS_UNKNOWN,)

    # ---- auto-join ----
    def _join_groups(self):
        rows = self.db.get_groups()
        targets_all = [g for g in rows if g["status"] != STATUS_SAFE]
        if not targets_all:
            self._emit("No groups to join — run a group scan first.")
            return

        SKIP_STATES = (JOIN_JOINED