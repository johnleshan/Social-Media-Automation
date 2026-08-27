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
import threading
import time
from datetime import datetime

from .approval import check_group, check_membership_status
from .browser import FacebookBrowser
from .database import STATUS_SAFE, STATUS_SKIP, STATUS_UNKNOWN, JOIN_NOT_JOINED, JOIN_PENDING, JOIN_JOINED, JOIN_DECLINED
from .discovery import search_groups
from .joiner import join_group
from .poster import post_media

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
        # We therefore hard-stop at 10 regardless of any user setting.
        HARD_JOIN_CAP = 10

        settings = self.config.settings
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

    # ---- posting loop ----
    def _posting_loop(self, caption):
        settings = self.config.settings
        delay_min = max(30, int(settings.get("delay_min", 180)))
        delay_max = max(delay_min, int(settings.get("delay_max", 480)))
        soft_cap = int(settings.get("soft_cap", 150))
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

    def _post_one(self, g, file_path, caption):
        self._set_stage("publish", g.get("name") or g["id"],
                        f"Publishing to {g['name']}...")
        try:
            status, message = post_media(
                self.browser.page, g["id"], file_path, caption or self.config.settings.get("caption", ""),
                self._emit,
            )
        except Exception as e:
            status, message = "failed", str(e)
        self.db.add_post(g["id"], g["name"], file_path, status, message)
        if status == "posted":
            self.db.mark_media_used(file_path)
            self._posted_this_run += 1
            self._emit(f"[POSTED] {g['name']} <- {os.path.basename(file_path)}")
        elif status == "pending":
            self.db.set_group_status(g["id"], STATUS_SKIP, "post routed to approval")
            self._emit(f"[PENDING] {g['name']} now flagged as require-approval.")
        else:
            self._emit(f"[FAILED] {g['name']}: {message}")

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