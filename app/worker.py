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
            self._thread.join(timeout=15)

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
            time.sleep(0.3)
            end = time.time() + max(0, ms) / 1000.0  # restart wait after resume
        while not self._stop.is_set():
            remaining_ms = int((end - time.time()) * 1000)
            if remaining_ms <= 0:
                return
            # bailing out early on pause lets the outer loop respect it promptly
            if self._pause.is_set():
                end = time.time() + max(0, ms) / 1000.0
                while self._pause.is_set() and not self._stop.is_set():
                    time.sleep(0.3)
                end = time.time() + max(0, ms) / 1000.0
            self.browser.page.wait_for_timeout(min(400, remaining_ms))

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
            # Bound all Playwright waits so a slow/hung group page can never
            # freeze the app (a hung Playwright call holds the GIL and stalls
            # the HTTP server too). Explicit fails time out and get skipped.
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
            # Classify how the run ended for the history log:
            #  - "cancelled"  -> user pressed Stop mid-run
            #  - "failed"     -> an error / not logged in / no account
            #  - "finished"   -> ran to a natural end
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
                if self.job == "join":
                    # progress list stays so a later run can skip finished groups
                    pass
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
            self._page_wait(random.randint(800, 1800))

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

        # Pre-filter groups we already know we cannot/should not join again:
        # already a member, request already pending, declined, or known-unviewable
        # (unavailable to us). Re-visiting these wastes time and delays the run.
        SKIP_STATES = (JOIN_JOINED, JOIN_PENDING, JOIN_DECLINED, "unviewable")
        targets_all = [g for g in targets_all if g.get("join_status") not in SKIP_STATES]
        if not targets_all:
            self._emit("No joinable groups left (all are already joined, pending, declined, or unavailable).")
            return

        # resume support: skip groups already processed by a previous run
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

        # ---- HARD SAFETY STOP ----
        # This is a permanent, non-configurable ceiling designed to protect the
        # account BEFORE Facebook can flag it. Research and community data agree:
        # staying under ~10 group joins/day on an established account (~5 on a
        # new one) never triggers a restriction; going past ~20-30 in a day is
        # a clear bot signal that causes a "joining too fast" block (24-48h).
        # We therefore hard-stop at 10 regardless of any user setting —
        # unless Developer mode is enabled, which lifts ALL caps.
        settings = self.config.settings
        if bool(settings.get("developer_mode", False)):
            HARD_JOIN_CAP = 1_000_000
            self._emit("Developer mode ON: daily join cap disabled.")
        else:
            HARD_JOIN_CAP = 10

        jd_min = max(15, int(settings.get("join_delay_min", 30)))
        jd_max = max(jd_min, int(settings.get("join_delay_max", 90)))

        # Check the hard cap BEFORE starting (prevention, not reaction).
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
            self._emit(
                f"HARD STOP — DAILY JOIN CAP REACHED."
            )
            self._emit(
                f"You have joined {already_joined_today}/{HARD_JOIN_CAP} groups today "
                "(the safe daily limit for your account)."
            )
            self._emit(
                "Stopping now BEFORE Facebook can flag this as automated behavior. "
                "Joining more today risks a temporary 'joining too fast' block."
            )
            self._emit(
                "Nothing is lost — join progress is saved. Run Auto-Join again "
                "tomorrow and it will continue from where it stopped."
            )
            self._final_summary = (
                f"HARD STOP: daily join cap reached ({already_joined_today}/{HARD_JOIN_CAP}). "
                "Resume tomorrow to continue."
            )
            return

        remaining_today = HARD_JOIN_CAP - already_joined_today
        effective_max = remaining_today  # cap this run at what's left today

        # A new join run that actually proceeds clears any past hard-stop notice.
        try:
            self.db.set_state("hard_stop", "")
        except Exception:
            pass

        self._emit(
            f"Auto-join: {len(targets)} group(s) queued, "
            f"{remaining_today} of {HARD_JOIN_CAP} safe join slots left today "
            f"(HARD cap — will stop there automatically), "
            f"{jd_min}-{jd_max}s random pauses."
        )
        self._emit(
            "HARD SAFETY STOP ACTIVE: The app will never join more than "
            f"{HARD_JOIN_CAP} groups in one day, no matter what. "
            "This protects your account from a Facebook 'joining too fast' "
            "restriction. Run again tomorrow to continue."
        )
        self.db.set_state("join_progress", json.dumps({"done": 0, "total": len(targets)}))
        self._set_stage("prepare", f"{len(targets)} group(s) queued",
                        f"Auto-joining {len(targets)} group(s)...")

        joined_n = pending_n = member_n = failed_n = 0
        run_count = 0
        for i, g in enumerate(targets, 1):
            if self._stop.is_set():
                break
            # Pause spin
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.3)
            if self._stop.is_set():
                break

            # ==== HARD STOP: enforce the daily safety cap BEFORE every join ====
            # Re-read the live counter each iteration so progress is accurate.
            live_today = self.db.joins_today()
            if live_today >= HARD_JOIN_CAP:
                self.db.set_state("hard_stop", json.dumps({
                    "triggered_at": datetime.now().isoformat(timespec="seconds"),
                    "joined": live_today,
                    "cap": HARD_JOIN_CAP,
                    "reason": "limit",
                    "message": (
                        f"You reached the safe daily limit of {HARD_JOIN_CAP} group "
                        f"joins today ({live_today} so far). The app stopped here so "
                        "Facebook cannot flag your account for 'joining groups too fast'."
                    ),
                }))
                self._emit(
                    f"HARD STOP — DAILY JOIN CAP REACHED ({live_today}/{HARD_JOIN_CAP})."
                )
                self._emit(
                    "Stopping before joining more could get your account flagged "
                    "for 'joining groups too fast'."
                )
                self._emit(
                    "Join progress is saved. Run Auto-Join again tomorrow and it "
                    "will continue automatically from here."
                )
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
                # Persist join status IMMEDIATELY — this is the source of truth
                js = "joined" if result in ("joined", "already_member") else "pending"
                self.db.set_join_status(g["id"], js)
                # Only a join the APP actually performed today counts toward the
                # 10/day hard cap. "already_member" means the group was joined in
                # the past (no Join button existed), so it did NOT consume a
                # request slot today. "unviewable"/"no_button"/"failed" never count.
                if result in ("joined", "pending"):
                    self.db.record_join(g["id"], result)
                    run_count += 1
                if result == "joined":
                    joined_n += 1
                elif result == "pending":
                    pending_n += 1
                else:
                    member_n += 1
                # AUTO-CLASSIFY every group we (successfully) joined. The app
                # decides safe/skip itself right here using the approval
                # heuristics — the user never has to classify. A wrong result
                # only ever trends conservative (skip/unknown → never auto-post),
                # so re-scraping the just-loaded page is safe even if sidebar
                # "join group" text lingers. The user can still mark a group
                # unsafe later to override this automatic decision.
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
                # Persist permanent outcomes so a future run skips them instead of
                # wasting time re-visiting groups we can never join.
                # unviewable / no_button mean the group is not joinable by this
                # account/proxy right now — treat as terminal to avoid repeats.
                if result in ("unviewable", "no_button"):
                    self.db.set_join_status(g["id"], result)
                    done.add(str(g["id"]))
                    try:
                        self.db.set_state("join_done", json.dumps(sorted(done)))
                    except Exception:
                        pass

            if run_count >= HARD_JOIN_CAP:
                # Cap consumed by this successful join. Stop immediately instead
                # of sleeping + re-checking, so the user sees a clean, prompt stop.
                self._emit(
                    f"HARD STOP — DAILY JOIN CAP REACHED ({run_count}/{HARD_JOIN_CAP})."
                )
                self._emit(
                    "Stopping before joining more could get your account flagged "
                    "for 'joining groups too fast'."
                )
                self._emit(
                    "Join progress is saved. Run Auto-Join again tomorrow and it "
                    "will continue automatically from here."
                )
                self._final_summary = (
                    f"HARD STOP: daily cap reached ({run_count}/{HARD_JOIN_CAP}). "
                    "Resume tomorrow to continue."
                )
                self.db.set_state("hard_stop", json.dumps({
                    "triggered_at": datetime.now().isoformat(timespec="seconds"),
                    "joined": run_count,
                    "cap": HARD_JOIN_CAP,
                    "reason": "limit",
                    "message": (
                        f"You reached the safe daily limit of {HARD_JOIN_CAP} group "
                        f"joins today ({run_count} so far). The app stopped here so "
                        "Facebook cannot flag your account for 'joining groups too fast'."
                    ),
                }))
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
        self._emit(f"Daily join count: {total_today}/{HARD_JOIN_CAP} used today (HARD cap).")
        self._emit(f"Queue ready: {safe} safe, {skip} skip, {unknown} unknown.")
        if total_today >= HARD_JOIN_CAP:
            self._emit(
                "HARD STOP: Today's join cap is fully used. Run Auto-Join again "
                "tomorrow to continue where it stopped."
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

        # Only check groups that are not already confirmed as members,
        # plus groups that were recently joined but never verified.
        targets = [
            g for g in rows
            if g["join_status"] in (JOIN_NOT_JOINED, JOIN_PENDING)
            or g["join_status"] not in ("joined", "declined", "unviewable")
        ]
        if not targets:
            self._emit("All groups already have a confirmed join status.")
            return

        settings = self.config.settings
        # Checking membership status is READ-ONLY (we only read the page; we do
        # NOT send join requests or take any account-visible action). The long
        # human-looking joins delay is pointless here, so we keep only a very
        # short, harmless pause between page loads.
        check_pause_min = 100
        check_pause_max = 300
        self._emit(
            f"Checking join status: {len(targets)} group(s)."
        )
        self.db.set_state("join_progress", json.dumps({"done": 0, "total": len(targets)}))
        self._set_stage("prepare", f"{len(targets)} group(s) queued",
                        f"Checking join status for {len(targets)} group(s)...")

        joined_n = pending_n = declined_n = unknown_n = 0
        for i, g in enumerate(targets, 1):
            if self._stop.is_set():
                break
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.3)
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
        self._emit(
            f"Join-status summary: {joined_n} confirmed members, "
            f"{pending_n} pending approval, {declined_n} declined, "
            f"{unknown_n} unknown."
        )
        if self.notify:
            self.notify(
                "Join-status check done",
                f"{joined_n} joined, {pending_n} pending, {declined_n} declined"
            )

    # ---- sync my groups (Option A) ----
    def _sync_my_groups(self):
        """Scrape the FULL list of groups this account belongs to from the live
        Facebook Groups feed and store them as confirmed memberships.

        Only a member (already on the feed) can appear here, so every synced
        group is a real membership regardless of how it was joined. We store
        them in the DB and mark join_status = 'joined' so the batch poster and
        the rest of the UI treat them as posting targets.
        """
        self._set_stage("sync", "Reading your Groups feed",
                        "Syncing your full Facebook groups list...")
        dev = bool(self.config.settings.get("developer_mode", False))
        self._emit("Syncing your full Facebook groups list from your Groups feed...")
        if dev:
            self._emit("Developer mode ON: scrolling the whole feed with no "
                       "early-stop so every group is captured.")
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
                # The feed often omits member counts — reuse the count we already
                # have in the DB for known groups instead of overwriting it.
                existing = self.db.get_group(g["id"])
                if existing and existing["member_count"]:
                    member_count = existing["member_count"]
                g["member_count"] = member_count
            self.db.upsert_group(
                g["id"], g["name"], member_count or None, g["url"]
            )
            # The feed only lists groups we already belong to -> confirmed member.
            self.db.set_join_status(g["id"], JOIN_JOINED)

        # Enrich missing member counts for any remaining 0-member groups
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
                    self.browser.page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    # Give the React shell time to render the group header before
                    # extracting; 600ms was too short on busy/cold pages and the
                    # "N members" text sometimes had not painted yet.
                    self.browser.page.wait_for_timeout(2500)
                    final_url = self.browser.page.url or ""
                    info = extract_group_info_from_page(self.browser.page)
                    new_mc = info.get("member_count") or 0
                    new_name = info.get("name") or ""

                    # Sanity: only accept the result if we actually landed on a
                    # single group page. Facebook sometimes bounces us to the
                    # /groups/ feed or the joins list (title like "All groups
                    # you've joined (N)"), where the extracted name/count is
                    # garbage for THIS group. In that case keep the stored row.
                    landed_on_group = _looks_like_group_page(final_url, str(g["id"]))
                    if not landed_on_group or _is_generic_title(new_name):
                        if new_mc:
                            g["member_count"] = new_mc if landed_on_group else 0
                        self._emit(f"[details {idx}/{len(zero_groups)}] {g['name']} -> "
                                   f"skipped (landed on list page '{new_name[:40]}' / {final_url[:60]})")
                        continue

                    if new_mc:
                        g["member_count"] = new_mc
                    if new_name and (not g.get("name") or g.get("name") == g.get("id")):
                        g["name"] = new_name
                    self.db.upsert_group(g["id"], name=(
                        new_name or g.get("name")), member_count=new_mc or None)
                    mc_str = f"{new_mc:,} members" if new_mc else "not found"
                    self._emit(f"[details {idx}/{len(zero_groups)}] {g['name']} -> {mc_str}")
                except Exception as ex:
                    self._emit(f"[details {idx}/{len(zero_groups)}] {g.get('name') or g['id']} -> could not load ({ex})")
                # Brief pause between live page visits to avoid triggering
                # Facebook's rate limiter (which shows the login wall).
                if not self._stop.is_set():
                    self._page_wait(1200)

        # Persist a readable result so the UI can show "Found N groups" and the
        # full list without needing to scroll the raw feed again.
        self.db.set_state("sync_result", json.dumps({
            "found": len(mine),
            "at": datetime.now().isoformat(timespec="seconds"),
            "groups": [{"id": g["id"], "name": g["name"], "member_count": g.get("member_count")}
                       for g in mine],
        }))
        self._final_summary = f"{len(mine)} group(s) synced from your Facebook account."
        self._emit(f"Synced {len(mine)} group(s) — all are treated as confirmed memberships.")
        self._emit("Refresh the Groups / My Groups view to see the full list.")
        if self.notify:
            self.notify("Groups synced", f"{len(mine)} of your groups found")

    # ---- batch post to all my groups ----
    def _blast_groups(self):
        """Post the ready-made content to one batch (batch_size) of the account's
        groups, one post per group per press, then stop. Durable resume continues
        on the next press (like auto-join resume)."""
        settings = self.config.settings
        media_folder = os.path.abspath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            settings.get("media_folder", "content"),
        ))
        try:
            batch = max(1, int(self.db.get_state("blast_batch", "10") or 10))
        except Exception:
            batch = 10

        # 1. Target pool = every confirmed membership from the last sync (no
        #    forced re-sync here — that would scrape every group and be very
        #    slow). Run "Scan My Groups"/sync separately to refresh the list.
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

        # No pre-flight restrictions: post to all groups, only 10/cycle + no repeat.
        blocked = []

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
                time.sleep(0.3)
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
            # Every attempted group is consumed for this cycle (no repeats).
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

        skip_note = f" · {len(blocked)} skipped (closed to Pages)" if blocked else ""
        self._final_summary = (
            f"{posted_n} posted · {failed_n} failed this batch{skip_note} · "
            f"{len(done)}/{len(targets_all)} groups covered this cycle"
        )
        self._emit(
            f"Batch finished: {posted_n} posted · {failed_n} failed this press, "
            f"{len(done)}/{len(targets_all)} of your groups covered this cycle"
            f"{skip_note}. Press Post Batch again to continue."
        )
        if self.notify:
            self.notify("Batch posting done",
                        f"{posted_n} posted · {failed_n} failed{skip_note} · "
                        f"{len(done)}/{len(targets_all)} groups this cycle")

    # ---- page post ----
    def _page_post(self, page_url):
        """Post a media file to an owned Facebook Page."""
        settings = self.config.settings
        media_folder = os.path.abspath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            settings.get("media_folder", "content"),
        ))
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
        media_folder = os.path.abspath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), media_folder
        ))

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
                    time.sleep(0.3)
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

    def _rank_niche(self, groups):
        """In-memory Kenya + niche + size pre-filter.

        Returns (kept, dropped_count). Kept groups are niche-relevant, not
        explicitly another region, not mega/off-topic, not over the member-size
        cap, and carry a real (non-junk) name. Sort: niche relevance desc, then
        smallest-first so the batch hits low-regulation groups first.
        """
        max_members = int(self.config.settings.get("niche_max_members", 0) or 0)
        kept, dropped = [], 0
        for g in groups:
            score = _niche_score_name(g.get("name") or "")
            if score is None:
                dropped += 1
                continue
            mc = int(g.get("member_count") or 0)
            if max_members and mc and mc > max_members:
                dropped += 1
                continue
            niche_hits, region_hits = score
            kept.append((-(niche_hits + region_hits), mc or 0, g))
        kept.sort(key=lambda t: (t[0], t[1], str(t[2].get("name") or t[2]["id"]).lower()))
        return [t[2] for t in kept], dropped

    def _filter_page_postable(self, groups, set_stage=None):
        """Partition groups into those a Page can actually post to. Loads each
        group page once and keeps only groups where the Page sees an open
        photo/video composer (no 'doesn't allow Pages' gate). While the live
        page is already open we also refresh the real name/member count (fixes
        the polluting 'Notifications' names) and read the group's activity so
        inactive groups (no recent posts) can be skipped. A load error is
        treated as postable (optimistic) so a transient failure can't skip a
        whole batch. Returns (postable, blocked)."""
        settings = self.config.settings
        max_idle = int(settings.get("max_group_idle_days", 0) or 0)
        postable, blocked = [], []
        n = len(groups)
        for i, g in enumerate(groups, 1):
            if self._stop.is_set():
                break
            name = g.get("name") or g["id"]
            if set_stage:
                set_stage("prepare", f"[{i}/{n}] {name}",
                          f"Checking if Pages can post to {name}...")
            try:
                ok = group_is_page_postable(self.browser.page, g["id"], self._emit)
            except Exception:
                ok = True
            if not ok:
                blocked.append(g)
                self._emit(f"[SKIP] {name}: not open to Page posts")
                continue
            # Refresh real name/count from the live group page and persist it,
            # so the pool's stale list-page names get progressively fixed.
            try:
                info = extract_group_info_from_page(self.browser.page, fallback_about=False)
                if info.get("name") or info.get("member_count"):
                    self.db.upsert_group(
                        g["id"], name=info.get("name"),
                        member_count=info.get("member_count"),
                    )
                    if info.get("name") and not _is_generic_title(info["name"]):
                        g["name"] = info["name"]
                    if info.get("member_count"):
                        g["member_count"] = info["member_count"]
            except Exception:
                pass
            # Activity gate: skip groups whose newest signal is older than the
            # idle limit. Unknown (no signal) is kept — the group may still be
            # active but the page didn't expose timestamps.
            if max_idle:
                try:
                    act = extract_group_activity(self.browser.page)
                    if act.get("days") is not None:
                        self.db.upsert_group(g["id"], last_active_days=act["days"])
                        if act["days"] > max_idle:
                            blocked.append(g)
                            self._emit(
                                f"[SKIP] {g.get('name') or g['id']}: last activity "
                                f"{act['days']}d ago (>{max_idle}d) — group inactive"
                            )
                            continue
                except Exception:
                    pass
            postable.append(g)
        return postable, blocked

    def _post_one(self, g, file_paths, caption):
        """Post one or more media files to a group. file_paths is a list."""
        paths = file_paths if isinstance(file_paths, (list, tuple)) else [file_paths]
        self._set_stage("publish", g.get("name") or g["id"],
                        f"Publishing to {g['name']}...")
        post_url = None
        try:
            status, message, post_url = post_media(
                self.browser.page, g["id"], paths,
                self._pending_caption if not caption else caption,
                self._emit,
            )
        except Exception as e:
            status, message = "failed", str(e)
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
        """Return one image + one video from the folder (or whatever is
        available). Rotates which video is picked based on media_used so
        each post gets a different combination."""
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
        # Pick one image (first/only).
        picked_img = images[0] if images else None
        # Pick one video, rotating based on last-used.
        picked_vid = None
        if videos:
            usage = {r["file_path"]: r["last_used_at"]
                     for r in self.db.conn.execute(
                         "SELECT file_path, last_used_at FROM media_used").fetchall()}
            unused_vids = [v for v in videos if v not in usage]
            if unused_vids:
                picked_vid = unused_vids[0]
            else:
                # All used — pick the one used longest ago.
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
        """Sleep up to `total` seconds (or forever) in <=1s slices so Stop
        takes effect quickly no matter how long the caller asked to wait.
        Also pauses if the user clicked Pause."""
        deadline = time.time() + total if total else None
        while not self._stop.is_set():
            # Pause spin: wait here until unpaused or stopped
            while self._pause.is_set() and not self._stop.is_set():
                time.sleep(0.3)
            if self._stop.is_set():
                return
            remaining = (deadline - time.time()) if deadline else float(interval)
            if remaining <= 0:
                return
            time.sleep(min(1.0, remaining))