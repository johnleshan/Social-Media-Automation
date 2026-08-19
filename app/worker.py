"""Continuous run-until-stopped worker.

Owns the browser session and the posting loop. Supports two jobs:
  * run  : optionally scan for new groups, then post continuously until stop().
  * scan : discover + approval-check groups only, then close the browser.

All browser work happens on the worker thread so Playwright's sync API stays
on a single thread and only one browser session exists at a time.
"""
import os
import random
import threading
import time
from datetime import datetime

from .approval import check_group
from .browser import FacebookBrowser
from .database import STATUS_SAFE, STATUS_SKIP, STATUS_UNKNOWN
from .discovery import search_groups
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
    def __init__(self, config, db, log=None):
        self.config = config
        self.db = db
        self.log = log or (lambda msg: print(msg))
        self._stop = threading.Event()
        self._thread = None
        self.browser = None
        self.job = None
        self.busy = False
        self._lock = threading.Lock()

    # ---- public API ----
    def is_busy(self):
        return self.busy

    def start_run(self, profile_name, keyword="", filter_mode="search",
                  min_members=0, caption=""):
        self._begin("run", profile_name, keyword, filter_mode, min_members, caption)

    def start_scan(self, profile_name, keyword, filter_mode="search",
                   min_members=0):
        self._begin("scan", profile_name, keyword, filter_mode, min_members, "")

    def stop(self):
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=15)

    # ---- internals ----
    def _begin(self, job, profile_name, keyword, filter_mode, min_members, caption):
        with self._lock:
            if self.busy:
                self.log("A job is already running. Stop it first.")
                return False
            self.busy = True
        self._stop.clear()
        self.job = job
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

    def _run_job(self, profile_name, keyword, filter_mode, min_members, caption):
        try:
            profile = self.config.get_profile(profile_name)
            if not profile:
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
                self._emit(
                    "Not logged into Facebook on this profile. "
                    "Open the account in the GUI and log in once, then retry."
                )
                return

            if keyword:
                self._scan_groups(keyword, filter_mode, min_members)

            if self.job == "scan":
                self._emit("Scan complete.")
                return

            if not keyword and settings.get("scan_on_start", True):
                self._scan_groups("", "search", 0)

            self._emit("Posting loop started. Press Stop to end it.")
            self._posting_loop(caption)

        except Exception as e:
            self._emit(f"Worker error: {e}")
        finally:
            self._close_browser()
            with self._lock:
                self.busy = False
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
            try:
                candidates = search_groups(
                    self.browser.page, keyword, filter_mode, min_members, self._emit
                )
            except Exception as e:
                self._emit(f"Search failed: {e}")
                candidates = []
            for c in candidates:
                self.db.upsert_group(c["id"], c["name"], c["member_count"], c["url"])
        else:
            candidates = self.db.get_groups(STATUS_UNKNOWN)
            self._emit(f"Checking {len(candidates)} unclassified group(s).")

        to_check = [c for c in candidates if self._needs_check(c["id"])]
        for i, c in enumerate(to_check, 1):
            if self._stop.is_set():
                break
            status, signal = check_group(self.browser.page, c["id"], self._emit)
            self.db.set_group_status(c["id"], status, signal)
            self._emit(
                f"[check {i}/{len(to_check)}] {c.get('name') or c['id']} "
                f"-> {status} ({signal})"
            )
            self.browser.page.wait_for_timeout(random.randint(2000, 4000))

        safe = self.db.count_groups(STATUS_SAFE)
        skip = self.db.count_groups(STATUS_SKIP)
        unknown = self.db.count_groups(STATUS_UNKNOWN)
        self._emit(f"Queue ready: {safe} safe, {skip} skip, {unknown} unknown.")

    def _needs_check(self, group_id):
        g = self.db.get_group(group_id)
        return g is None or g["status"] in (STATUS_UNKNOWN,)

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
                self._emit(f"Waiting {delay // 60}m{delay % 60}s before the next post...")
                self._wait_stop(interval=1, total=delay)

    def _post_one(self, g, file_path, caption):
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
        waited = 0
        while not self._stop.is_set():
            if total and waited >= total:
                return
            time.sleep(interval)
            waited += interval