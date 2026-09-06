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
from .config import resolve_media_dir
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


def _is_browser_dead(msg):
    """True when a Playwright failure means the Chrome/CDP connection is gone.

    A dead browser shows up as 'Connection closed while reading from the
    driver' (page object unusable) or similar transport errors. Treating these
    as normal per-group failures would keep the batch running against a dead
    browser forever; callers use this to trigger a relaunch and retry."""
    msg = (msg or "").lower()
    return any(k in msg for k in (
        "connection closed",
        "connection refused",
        "browser has been closed",
        "target closed",
        "target page, context or browser has been closed",
        "socket close",
        "websocket",
        "protocol error",
        "driver",
        "execution context was destroyed",
        "net::err_connection",
    ))


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

        SKIP_STATES = (JOIN_JOINED, JOIN_PENDING, JOIN_DECLINED, "unviewable")
        targets_all = [g for g in targets_all if g.get("join_status") not in SKIP_STATES]
        if not targets_all:
            self._emit("No joinable groups left (all are already joined, pending, declined, or unavailable).")
            return

        done = set()
        try:
            raw = self.db.get_state("join_done", "") or ""
            done = set(json.loads(raw)) if raw else set()
        except Exception:
            done = set()
        targets = [g for g in targets_all if str(g["id"]) not in done]
        if targets_all and not targets:
            self._emit(
                f"All {len(done)} previously processed group(s) are done — "
                "starting a fresh pass."
            )
            done = set()
            targets = list(targets_all)
        elif done:
            self._emit(
                f"Resuming: skipping {len(done)} already-processed group(s), "
                f"{len(targets)} left to try."
            )
        try:
            self.db.set_state("join_done", json.dumps(sorted(done)))
        except Exception:
            pass

        settings = self.config.settings
        if bool(settings.get("developer_mode", False)):
            HARD_JOIN_CAP = 1_000_000
            self._emit("Developer mode ON: daily join cap disabled.")
        else:
            HARD_JOIN_CAP = 10

        jd_min = max(15, int(settings.get("join_delay_min", 30)))
        jd_max = max(jd_min, int(settings.get("join_delay_max", 90)))

        already_joined_today = self.db.joins_today()
        if already_joined_today >= HARD_JOIN_CAP:
            self.db.set_state("hard_stop", json.dumps({
                "triggered_at": datetime.now().isoformat(timespec="seconds"),
                "joined": already_joined_today,
                "cap": HARD_JOIN_CAP,
                "reason": "start",
                "message": (
                    f"You joined {already_joined_today} groups today — that is the "
                    "full safe daily limit. The app stopped before joining any more "
                    "so Facebook cannot flag your account for 'joining groups too fast'."
                ),
            }))
            self._emit("HARD STOP — DAILY JOIN CAP REACHED.")
            self._final_summary = (
                f"HARD STOP: daily join cap reached ({already_joined_today}/{HARD_JOIN_CAP}). "
                "Resume tomorrow to continue."
            )
            return

        remaining_today = HARD_JOIN_CAP - already_joined_today
        try:
            self.db.set_state("hard_stop", "")
        except Exception:
            pass

        self._emit(
            f"Auto-join: {len(targets)} group(s) queued, "
            f"{remaining_today} of {HARD_JOIN_CAP} safe join slots left today, "
            f"{jd_min}-{jd_max}s random pauses."
        )
        self.db.set_state("join_progress", json.dumps({"done": 0, "total": len(targets)}))
        self._set_stage("prepare", f"{len(targets)} group(s) queued",
                        f"Auto-joining {len(targets)} group(s)...")

        joined_n = pending_n = member_n = failed_n = 0
        run_count = 0
        for i, g in enumerate(targets, 1):
            if self._stop.is_set():
                break
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.2)
            if self._stop.is_set():
                break

            live_today = self.db.joins_today()
            if live_today >= HARD_JOIN_CAP:
                self.db.set_state("hard_stop", json.dumps({
                    "triggered_at": datetime.now().isoformat(timespec="seconds"),
                    "joined": live_today,
                    "cap": HARD_JOIN_CAP,
                    "reason": "limit",
                    "message": (
                        f"You reached the safe daily limit of {HARD_JOIN_CAP} group "
                        f"joins today ({live_today} so far)."
                    ),
                }))
                self._emit(f"HARD STOP — DAILY JOIN CAP REACHED ({live_today}/{HARD_JOIN_CAP}).")
                self._final_summary = (
                    f"HARD STOP: daily cap reached ({live_today}/{HARD_JOIN_CAP}). "
                    "Resume tomorrow to continue."
                )
                break

            name = g.get("name") or g["id"]
            self._set_stage("work", f"[{i}/{len(targets)}] {name}",
                            f"Visiting & joining: {name} ({i}/{len(targets)})")
            try:
                result, detail = join_group(self.browser.page, g["id"], self._emit)
            except Exception as e:
                result, detail = "failed", str(e)

            if result in ("joined", "pending", "already_member"):
                done.add(str(g["id"]))
                try:
                    self.db.set_state("join_done", json.dumps(sorted(done)))
                except Exception:
                    pass
                js = "joined" if result in ("joined", "already_member") else "pending"
                self.db.set_join_status(g["id"], js)
                if result in ("joined", "pending"):
                    self.db.record_join(g["id"], result)
                    run_count += 1
                if result == "joined":
                    joined_n += 1
                elif result == "pending":
                    pending_n += 1
                else:
                    member_n += 1
                self._set_stage("classify", f"[{i}/{len(targets)}] {name}",
                                f"Classifying: {name} ({i}/{len(targets)})")
                status, signal = check_group(
                    self.browser.page, g["id"], self._emit, goto=False
                )
                self.db.set_group_status(g["id"], status, signal)
                self._emit(
                    f"[join {i}/{len(targets)}] {name} -> {detail}; "
                    f"joined={js}, classified {status} ({signal})"
                )
            else:
                failed_n += 1
                self._emit(f"[join {i}/{len(targets)}] {name} -> {result} ({detail})")
                if result in ("unviewable", "no_button"):
                    self.db.set_join_status(g["id"], result)
                    done.add(str(g["id"]))
                    try:
                        self.db.set_state("join_done", json.dumps(sorted(done)))
                    except Exception:
                        pass

            if run_count >= HARD_JOIN_CAP:
                self._emit(f"HARD STOP — DAILY JOIN CAP REACHED ({run_count}/{HARD_JOIN_CAP}).")
                self._final_summary = (
                    f"HARD STOP: daily cap reached ({run_count}/{HARD_JOIN_CAP}). "
                    "Resume tomorrow to continue."
                )
                break

            self.db.set_state(
                "join_progress", json.dumps({"done": i, "total": len(targets)})
            )
            self._page_wait(random.randint(jd_min * 1000, jd_max * 1000))

        safe = self.db.count_groups(STATUS_SAFE)
        skip = self.db.count_groups(STATUS_SKIP)
        unknown = self.db.count_groups(STATUS_UNKNOWN)
        total_today = self.db.joins_today()
        self._final_summary = (
            f"{joined_n} joined · {pending_n} pending · "
            f"{member_n} already members · {failed_n} failed · "
            f"{total_today} total joins today"
        )
        self._emit(
            f"Join summary: {joined_n} joined, {pending_n} pending approval, "
            f"{member_n} already members, {failed_n} not joinable."
        )
        if self.notify:
            self.notify("Auto-Join finished", f"{joined_n} joined, {pending_n} pending, {failed_n} failed")

    # ---- join-status check ----
    def _check_join_status(self):
        """Visit each discovered group and check whether we are a member."""
        rows = self.db.get_groups()
        if not rows:
            self._emit("No groups to check — run a scan first.")
            return

        targets = [
            g for g in rows
            if g["join_status"] in (JOIN_NOT_JOINED, JOIN_PENDING)
            or g["join_status"] not in ("joined", "declined", "unviewable")
        ]
        if not targets:
            self._emit("All groups already have a confirmed join status.")
            return

        check_pause_min = 100
        check_pause_max = 300
        self._emit(f"Checking join status: {len(targets)} group(s).")
        self.db.set_state("join_progress", json.dumps({"done": 0, "total": len(targets)}))
        self._set_stage("prepare", f"{len(targets)} group(s) queued",
                        f"Checking join status for {len(targets)} group(s)...")

        joined_n = pending_n = declined_n = unknown_n = 0
        for i, g in enumerate(targets, 1):
            if self._stop.is_set():
                break
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.2)
            if self._stop.is_set():
                break
            name = g.get("name") or g["id"]
            self._set_stage("work", f"[{i}/{len(targets)}] {name}",
                            f"Checking: {name} ({i}/{len(targets)})")
            try:
                join_status, detail = check_membership_status(
                    self.browser.page, g["id"], self._emit
                )
                info = extract_group_info_from_page(self.browser.page)
                if info.get("name") or info.get("member_count"):
                    self.db.upsert_group(g["id"], name=info.get("name"), member_count=info.get("member_count"))
                    if info.get("name"):
                        name = info.get("name")
            except Exception as e:
                join_status, detail = "unknown", str(e)

            self.db.set_join_status(g["id"], join_status)
            if join_status == "joined":
                joined_n += 1
            elif join_status == "pending":
                pending_n += 1
            elif join_status == "declined":
                declined_n += 1
            else:
                unknown_n += 1
            self._emit(
                f"[check {i}/{len(targets)}] {name} -> {join_status} ({detail})"
            )

            self.db.set_state(
                "join_progress", json.dumps({"done": i, "total": len(targets)})
            )
            self._page_wait(random.randint(check_pause_min, check_pause_max))

        self._final_summary = (
            f"{joined_n} joined · {pending_n} pending · "
            f"{declined_n} declined · {unknown_n} uncheckable"
        )
        if self.notify:
            self.notify(
                "Join-status check done",
                f"{joined_n} joined, {pending_n} pending, {declined_n} declined"
            )

    # ---- sync my groups (Option A) ----
    def _sync_my_groups(self):
        self._set_stage("sync", "Reading your Groups feed",
                        "Syncing your full Facebook groups list...")
        dev = bool(self.config.settings.get("developer_mode", False))
        self._emit("Syncing your full Facebook groups list from your Groups feed...")
        try:
            mine = list_my_groups(
                self.browser.page, self._emit,
                max_scrolls=500 if dev else 100,
                no_early_stop=dev,
                should_stop=lambda: self._stop.is_set(),
                should_pause=self._pause.is_set,
            )
        except Exception as e:
            self._final_summary = f"Sync failed: {e}"
            self._emit(f"Sync failed: {e}")
            return
        for g in mine:
            member_count = g.get("member_count") or 0
            if not member_count:
                existing = self.db.get_group(g["id"])
                if existing and existing["member_count"]:
                    member_count = existing["member_count"]
                g["member_count"] = member_count
            self.db.upsert_group(
                g["id"], g["name"], member_count or None, g["url"]
            )
            self.db.set_join_status(g["id"], JOIN_JOINED)

        zero_groups = [g for g in mine if not g.get("member_count")]
        if zero_groups:
            self._emit(f"Enriching member counts for {len(zero_groups)} group(s)...")
            for idx, g in enumerate(zero_groups, 1):
                if self._stop.is_set():
                    break
                name_disp = g.get("name") or g["id"]
                self._set_stage("sync", f"[{idx}/{len(zero_groups)}] {name_disp}",
                                f"Checking member count: {name_disp} ({idx}/{len(zero_groups)})")
                try:
                    url = g.get("url") or f"https://www.facebook.com/groups/{g['id']}/"
                    self.browser.page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    self.browser.page.wait_for_timeout(1500)
                    final_url = self.browser.page.url or ""
                    info = extract_group_info_from_page(self.browser.page)
                    new_mc = info.get("member_count") or 0
                    new_name = info.get("name") or ""

                    landed_on_group = _looks_like_group_page(final_url, str(g["id"]))
                    if not landed_on_group or _is_generic_title(new_name):
                        if new_mc:
                            g["member_count"] = new_mc if landed_on_group else 0
                        continue

                    if new_mc:
                        g["member_count"] = new_mc
                    if new_name and (not g.get("name") or g.get("name") == g.get("id")):
                        g["name"] = new_name
                    self.db.upsert_group(g["id"], name=(
                        new_name or g.get("name")), member_count=new_mc or None)
                except Exception as ex:
                    pass
                if not self._stop.is_set():
                    self._page_wait(800)

        self.db.set_state("sync_result", json.dumps({
            "found": len(mine),
            "at": datetime.now().isoformat(timespec="seconds"),
            "groups": [{"id": g["id"], "name": g["name"], "member_count": g.get("member_count")}
                       for g in mine],
        }))
        self._final_summary = f"{len(mine)} group(s) synced from your Facebook account."
        self._emit(f"Synced {len(mine)} group(s) — all are treated as confirmed memberships.")
        if self.notify:
            self.notify("Groups synced", f"{len(mine)} of your groups found")

    # ---- batch post to all my groups ----
    def _blast_groups(self):
        settings = self.config.settings
        media_folder = resolve_media_dir(settings.get("media_folder", "content"))
        try:
            batch = max(1, int(self.db.get_state("blast_batch", "10") or 10))
        except Exception:
            batch = 10

        done = set()
        try:
            raw = self.db.get_state("blast_done", "") or ""
            done = set(json.loads(raw)) if raw else set()
        except Exception:
            done = set()
        targets_all = self.db.get_groups_by_join_status(JOIN_JOINED)
        targets = [g for g in targets_all if str(g["id"]) not in done]
        if targets_all and not targets:
            self._emit(
                f"All {len(done)} of your groups already posted this cycle — "
                "starting a fresh pass."
            )
            done = set()
            targets = list(targets_all)
            self.blast_posted = []
            try:
                self.db.set_state("blast_result", json.dumps([]))
            except Exception:
                pass

        batch_targets = targets[:batch]
        if not batch_targets:
            self._final_summary = "No un-posted groups to target — run sync or finish the current cycle."
            self._emit("No un-posted groups to target. Sync your groups and press Post Batch again.")
            return

        self.db.set_state("blast_progress", json.dumps({
            "done": len(done), "total": len(targets_all),
        }))
        self._set_stage("prepare",
                        f"{len(batch_targets)} group(s) in this batch "
                        f"({len(done)}/{len(targets_all)} done this cycle)",
                        f"Posting to {len(batch_targets)} group(s) this press...")

        posted_n = failed_n = 0
        for i, g in enumerate(batch_targets, 1):
            if self._stop.is_set():
                break
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.2)
            if self._stop.is_set():
                break

            file_paths = self._pick_all_media(media_folder)
            if not file_paths:
                self._emit("No media files found in the content folder.")
                self._final_summary = "No media files found in the content folder."
                break

            self._set_stage("work", f"[{i}/{len(batch_targets)}] {g.get('name') or g['id']}",
                            f"Posting to {g.get('name') or g['id']} ({i}/{len(batch_targets)})")
            status = self._post_one(g, file_paths, self._pending_caption)
            post_url = self._last_post_url
            done.add(str(g["id"]))
            if status == "posted":
                posted_n += 1
            elif status == "pending":
                pass
            else:
                failed_n += 1
            if status in ("posted", "pending"):
                self.blast_posted.append({
                    "id": str(g["id"]),
                    "name": g.get("name") or g["id"],
                    "member_count": g.get("member_count") or 0,
                    "posted_at": datetime.now().isoformat(timespec="seconds"),
                    "post_url": post_url or "",
                })
                try:
                    self.db.set_state("blast_result", json.dumps(self.blast_posted))
                except Exception:
                    pass
            try:
                self.db.set_state("blast_done", json.dumps(sorted(done)))
            except Exception:
                pass
            self.db.set_state("blast_progress", json.dumps({
                "done": len(done), "total": len(targets_all),
            }))

        self._final_summary = (
            f"{posted_n} posted · {failed_n} failed this batch · "
            f"{len(done)}/{len(targets_all)} groups covered this cycle"
        )
        self._emit(
            f"Batch finished: {posted_n} posted · {failed_n} failed this press, "
            f"{len(done)}/{len(targets_all)} of your groups covered this cycle."
        )
        if self.notify:
            self.notify("Batch posting done",
                        f"{posted_n} posted · {failed_n} failed · "
                        f"{len(done)}/{len(targets_all)} groups this cycle")

    # ---- page post ----
    def _page_post(self, page_url):
        settings = self.config.settings
        media_folder = resolve_media_dir(settings.get("media_folder", "content"))
        file_path = None
        want = getattr(self, "_page_file_override", "") or ""
        if want:
            cand = os.path.join(media_folder, want)
            if os.path.isfile(cand):
                file_path = cand
        if not file_path:
            file_path = self._pick_media(media_folder)
        if not file_path:
            self._final_summary = "No media files found in the content folder."
            self._emit("No media files found in the content folder.")
            return

        self._set_stage("work", f"Posting {os.path.basename(file_path)} to page",
                        "Posting to your Page...")
        self._emit(f"Posting {os.path.basename(file_path)} to the Page...")
        try:
            status, msg = post_media_to_page(
                self.browser.page, file_path, page_url,
                caption=self._pending_caption, log=self._emit,
            )
        except Exception as e:
            status, msg = "failed", str(e)

        self._emit(f"Page post result: {status} — {msg}")
        self._final_summary = f"Page post: {status} — {msg}"
        if self.notify:
            self.notify("Page post", f"{status}: {msg}")

    # ---- posting loop ----
    def _posting_loop(self, caption):
        settings = self.config.settings
        delay_min = max(30, int(settings.get("delay_min", 180)))
        delay_max = max(delay_min, int(settings.get("delay_max", 480)))
        dev = bool(settings.get("developer_mode", False))
        soft_cap = 0 if dev else int(settings.get("soft_cap", 150))
        max_cycle = int(settings.get("max_cycle_posts", 0))
        media_folder = settings.get("media_folder", "content")
        media_folder = resolve_media_dir(media_folder)

        today = datetime.now().strftime("%Y-%m-%d")
        cycle_count = 0
        while not self._stop.is_set():
            if self._checkpointed():
                self._emit(
                    "Facebook checkpoint detected. Resolve it in the browser, "
                    "then I will continue."
                )
                self._wait_stop(interval=10)
                continue

            if self._reached_cap(today, soft_cap):
                self._emit(
                    f"Daily soft cap reached ({soft_cap}). Waiting until you Stop."
                )
                self._wait_stop(interval=30)
                continue

            queue = self.db.get_groups(STATUS_SAFE)
            if not queue:
                self._emit("No safe groups available. Waiting for Stop or a new scan.")
                self._wait_stop(interval=30)
                continue

            if max_cycle and cycle_count >= max_cycle:
                self._emit(f"Max posts this cycle reached ({max_cycle}). Waiting.")
                self._wait_stop(interval=30)
                cycle_count = 0
                continue

            for g in queue:
                if self._stop.is_set():
                    break
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.2)
                if self._stop.is_set():
                    break
                self._set_stage("pick", f"{len(queue)} safe group(s)",
                                f"Picking next post from {g['name']}...")
                file_path = self._pick_media(media_folder)
                if not file_path:
                    self._emit("No media files found in the content folder.")
                    self._wait_stop(interval=30)
                    break
                self._post_one(g, file_path, caption)
                cycle_count += 1
                if self._stop.is_set():
                    break
                delay = random.randint(delay_min, delay_max)
                self._set_stage("cooldown", f"next post in {delay}s",
                                f"Cooling down {delay // 60}m{delay % 60}s before the next post...")
                self._emit(f"Waiting {delay // 60}m{delay % 60}s before the next post...")
                self._wait_stop(interval=1, total=delay)

        self._final_summary = f"{self._posted_this_run} post(s) published this run."
        if self.notify:
            self.notify("Posting stopped", f"{self._posted_this_run} posted this run")

    def _post_one(self, g, file_paths, caption):
        """Post one or more media files to a group. file_paths is a list."""
        paths = file_paths if isinstance(file_paths, (list, tuple)) else [file_paths]
        self._set_stage("publish", g.get("name") or g["id"],
                        f"Publishing to {g['name']}...")
        # A failed post is retried once when the failure is definitively
        # transient: no submission happened (composer never opened, file input
        # never appeared, post button never clicked), so a retry cannot
        # double-post. Browser-death ("Connection closed") is retried with a
        # fresh Chrome. Verify-first ordering guarantees these messages mean
        # nothing was published yet.
        RETRY_MESSAGES = (
            "could not find the photo/video composer",
            "no visible file input appeared",
            "could not find the post button",
            "upload did not complete in time",
        )
        for attempt in (1, 2, 3):
            post_url = None
            status = message = ""
            try:
                status, message, post_url = post_media(
                    self.browser.page, g["id"], paths,
                    self._pending_caption if not caption else caption,
                    self._emit,
                )
            except Exception as e:
                status, message = "failed", str(e)
            reason = message.lower()
            if attempt < 3:
                if _is_browser_dead(message):
                    self._emit("[recover] Chrome connection lost — relaunching browser and retrying…")
                    self._relaunch_browser()
                    self._set_stage("publish", g.get("name") or g["id"],
                                    f"Retrying {g['name']} after browser restart...")
                    continue
                if any(key in reason for key in RETRY_MESSAGES):
                    self._emit(f"[retry] transient composer failure ('{message}') — retrying {g['name']} ({attempt + 1}/3)...")
                    self._set_stage("publish", g.get("name") or g["id"],
                                    f"Retrying {g['name']} after transient composer issue...")
                    continue
            break
        self._last_post_url = post_url
        files_label = " + ".join(os.path.basename(p) for p in paths)
        self.db.add_post(g["id"], g["name"], files_label, status, message, post_url=post_url)
        if status == "posted":
            for fp in paths:
                self.db.mark_media_used(fp)
            self._posted_this_run += 1
            link = f"  {post_url}" if post_url else ""
            self._emit(f"[POSTED] {g['name']} <- {files_label}{link}")
        elif status == "pending":
            self.db.set_group_status(g["id"], STATUS_SKIP, "post routed to approval")
            self._emit(f"[PENDING] {g['name']} now flagged as require-approval.")
        else:
            self._emit(f"[FAILED] {g['name']}: {message}")
        return status

    def _relaunch_browser(self):
        """Close the dead Chrome and start a fresh session on the same profile."""
        name = getattr(self.browser, "user_data_dir", None)
        headless = getattr(self.browser, "headless", True)
        try:
            self.browser.close()
        except Exception:
            pass
        self.browser = FacebookBrowser(
            name, headless=headless, log=self._emit,
        )
        try:
            self.browser.launch()
            self.browser.page.set_default_timeout(15000)
        except Exception as e:
            self._emit(f"[recover] browser relaunch failed: {e}")

    def _pick_media(self, folder):
        files = []
        if os.path.isdir(folder):
            for root, _dirs, names in os.walk(folder):
                for name in names:
                    if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                        files.append(os.path.join(root, name))
        if not files:
            return None
        files.sort()
        usage = {r["file_path"]: r["last_used_at"]
                 for r in self.db.conn.execute(
                     "SELECT file_path, last_used_at FROM media_used").fetchall()}
        unused = [f for f in files if f not in usage]
        if unused:
            return unused[0]
        oldest = self.db.oldest_used_media()
        if oldest and os.path.exists(oldest):
            return oldest
        return files[0]

    def _pick_all_media(self, folder):
        images = []
        videos = []
        if os.path.isdir(folder):
            for root, _dirs, names in os.walk(folder):
                for name in names:
                    full = os.path.join(root, name)
                    ext = os.path.splitext(name)[1].lower()
                    if ext in IMAGE_EXTS:
                        images.append(full)
                    elif ext in VIDEO_EXTS:
                        videos.append(full)
        images.sort()
        videos.sort()
        if not images and not videos:
            return []
        picked_img = images[0] if images else None
        picked_vid = None
        if videos:
            usage = {r["file_path"]: r["last_used_at"]
                     for r in self.db.conn.execute(
                         "SELECT file_path, last_used_at FROM media_used").fetchall()}
            unused_vids = [v for v in videos if v not in usage]
            if unused_vids:
                picked_vid = unused_vids[0]
            else:
                oldest = self.db.oldest_used_media()
                for v in videos:
                    if v == oldest:
                        picked_vid = v
                        break
                if not picked_vid:
                    picked_vid = videos[0]
        result = [p for p in (picked_img, picked_vid) if p]
        return result if result else []

    def _reached_cap(self, today, soft_cap):
        if not soft_cap:
            return False
        row = self.db.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status='posted' AND created_at LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return row[0] >= soft_cap

    def _checkpointed(self):
        try:
            if not self.browser or not self.browser.page:
                return False
            url = self.browser.page.url
            if "checkpoint" in url.lower():
                return True
            body = self.browser.page.inner_text("body")[:4000].lower()
            return any(p in body for p in CHECKPOINT_PHRASES)
        except Exception:
            return False

    def _wait_stop(self, interval=1, total=None):
        deadline = time.time() + total if total else None
        while not self._stop.is_set():
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.2)
            if self._stop.is_set():
                return
            remaining = (deadline - time.time()) if deadline else float(interval)
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))
