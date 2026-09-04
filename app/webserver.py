"""Local web UI server for Group Post Automator.

Pure stdlib (no new dependencies). Serves the single-page frontend from the
``web/`` folder and exposes a small JSON API on top of the existing
Worker / Config / Database / Browser classes.

The Worker still owns the Playwright browser session and runs on its own
thread exactly as before. This module only:
  * exposes the worker's log stream through GET /api/logs (the old 200 ms
    queue-poll pattern), and
  * tracks add-account / import / verify / login lifecycle states so the
    frontend can surface every failure mode clearly (missing profile, not
    logged in, import running/failed, login window open, ...).
"""
import faulthandler
import html
import itertools
import json
import os
import string
import subprocess
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .browser import (
    FacebookBrowser,
    find_chrome_user_data_dir,
    import_chrome_session,
    chrome_running,
    list_chrome_profiles,
    open_login,
    _kill_chrome_on_profile,
    _clean_stale_profile_locks,
)
from .config import BASE_DIR, Config
from .database import STATUS_SAFE, STATUS_SKIP, STATUS_UNKNOWN, JOIN_NOT_JOINED, JOIN_PENDING, JOIN_JOINED, JOIN_DECLINED, Database
from .worker import MEDIA_EXTS, VIDEO_EXTS, Worker
VIDEO_EXTS_SET = VIDEO_EXTS or set()


def send_toast(title: str, body: str):
    """Fire a Windows toast notification via PowerShell (detached, non-blocking)."""
    # Escape for PowerShell -Command string
    t = html.escape(title).replace("'", "''")
    b = html.escape(body).replace("'", "''")
    ps = f'''
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
    $template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
    $xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
    $text = $xml.GetElementsByTagName("text")
    $text[0].AppendChild($xml.CreateTextNode('{t}')) | Out-Null
    $text[1].AppendChild($xml.CreateTextNode('{b}')) | Out-Null
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    # optional: add audio (default sound plays automatically)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("Group Post Automator").Show($toast)
    '''
    # Run detached, hide window, no wait
    subprocess.Popen(
        ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", ps],
        creationflags=subprocess.CREATE_NO_WINDOW,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

WEB_DIR = os.path.join(BASE_DIR, "web")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".mp4": "video/mp4", ".mov": "video/quicktime", ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".wmv": "video/x-ms-wmv",
    ".m4v": "video/x-m4v",
}

LOG_LEVELS = ("posted", "pending", "failed", "check", "warn", "info")


def _classify(msg):
    """Map a worker log line to a level, mirroring the old Tkinter GUI."""
    up = msg.upper()
    low = msg.lower()
    if "[POSTED]" in up or "session verified: logged in" in low:
        return "posted"
    if "[PENDING]" in up:
        return "pending"
    if "[FAILED]" in up or "error" in low or "failed" in low or "could not" in low:
        return "failed"
    if "[check" in low or "queue ready" in low:
        return "check"
    if any(k in low for k in (
        "warn", "waiting", "checkpoint", "important", "not logged",
        "import", "login", "session",
    )):
        return "warn"
    return "info"


def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class AutomationState:
    """Shared state behind the HTTP API. One instance per server run."""

    def __init__(self):
        self.config = Config()
        self.db = Database()
        self.worker = Worker(self.config, self.db, log=self._push_log, notify=send_toast)
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._log_lines = []  # list of (id, text, level)
        self.login_state = {"running": False, "profile": None}
        self.import_state = {"running": False, "profile": None, "result": None, "message": ""}
        self.verify_state = {"running": False, "profile": None, "result": None, "message": ""}

    # ------------------------------------------------------------------ log stream
    def _push_log(self, msg):
        try:
            text = str(msg)
            with self._lock:
                self._log_lines.append((next(self._ids), text, _classify(text)))
                if len(self._log_lines) > 4000:
                    del self._log_lines[:1000]
        except Exception:
            pass

    def logs_json(self, since=0):
        with self._lock:
            lines = [
                {"id": i, "text": t, "level": lv}
                for i, t, lv in self._log_lines
                if i > since
            ]
            current = self._log_lines[-1][0] if self._log_lines else since
        return {"lines": lines, "next_id": current}

    # ------------------------------------------------------------------ profiles
    def _selected_name(self):
        names = [p["name"] for p in self.config.profiles]
        last = self.config.get("last_profile", "")
        return last if last in names else (names[0] if names else None)

    def _profile_status(self, name):
        """Setup status for one account — what's ready and what still needs
        attention, so the UI can show accounts that are fully set up."""
        profile = self.config.get_profile(name)
        if not profile:
            return {"code": "missing", "label": "Profile not found",
                    "detail": f"No account named '{name}' is configured.",
                    "dir_exists": False, "cookies": False, "stored": "",
                    "ready": False, "ready_count": 0, "setup_total": 5, "setup": []}
        udd = profile["user_data_dir"]
        dir_exists = os.path.isdir(udd)
        cookies = os.path.exists(os.path.join(udd, "Default", "Network", "Cookies"))
        stored = self.db.get_state(f"session_verified:{name}", "") or ""
        if not dir_exists:
            code, label = "missing_dir", "Profile folder missing"
        elif stored == "ok":
            code, label = "ok", "Logged in (verified)"
        elif stored == "no_session":
            code, label = "no_session", "Not logged in"
        elif stored == "error":
            code, label = "error", "Session check failed"
        elif cookies:
            code, label = "never_checked", "Session not checked yet"
        else:
            code, label = "no_session", "No session files"

        s = self.config.settings
        media_folder = (s.get("media_folder") or "").strip()
        media_ok = False
        if media_folder:
            # resolve relative paths against the workspace root, same as worker
            media_path = os.path.abspath(os.path.join(BASE_DIR, media_folder))
            if os.path.isdir(media_path):
                try:
                    media_ok = any(
                        os.path.splitext(f)[1].lower() in MEDIA_EXTS
                        for f in os.listdir(media_path)
                    )
                except OSError:
                    media_ok = False
        delay_min = _to_int(s.get("delay_min"), 0)
        delay_max = _to_int(s.get("delay_max"), 0)
        delay_ok = delay_min > 0 and delay_max >= delay_min
        safe_count = self.db.count_groups(STATUS_SAFE)
        logged_ok = stored == "ok"

        media_hint = media_folder or "Set a media folder in Content"
        if media_folder:
            media_hint += f" ({os.path.abspath(os.path.join(BASE_DIR, media_folder))})"
        setup = [
            {"key": "login", "label": "Logged into Facebook", "ok": logged_ok,
             "hint": "One-time login via the opened Chrome window"},
            {"key": "folder", "label": "Profile folder", "ok": dir_exists,
             "hint": "Account profile exists on disk"},
            {"key": "media", "label": "Media folder ready", "ok": media_ok,
             "hint": media_hint},
            {"key": "groups", "label": "Safe groups", "ok": safe_count > 0,
             "hint": f"{safe_count} group(s) classified as safe — run a scan"},
            {"key": "delay", "label": "Post delay set", "ok": delay_ok,
             "hint": f"{delay_min}–{delay_max}s between posts"},
        ]
        ready = all(x["ok"] for x in setup)
        detail = f"Session check: {stored or 'not run'}  •  Cookies present: {cookies}"
        return {"code": code, "label": label, "detail": detail,
                "dir_exists": dir_exists, "cookies": cookies, "stored": stored,
                "ready": ready, "ready_count": sum(1 for x in setup if x["ok"]),
                "setup_total": len(setup), "setup": setup}

    def _system(self, selected):
        """The prominent banner shown above the dashboard."""
        if not self.config.profiles:
            return {"level": "error", "title": "No accounts set up yet",
                    "text": "Add an account (a browser opens for a one-time login) or import your "
                            "already-logged-in Chrome session. The bot cannot run without one.",
                    "actions": [{"id": "add", "label": "Add Account"},
                                {"id": "import", "label": "Import from Chrome"}]}
        if self.login_state.get("running"):
            return {"level": "info", "title": f"Login window open for '{self.login_state.get('profile')}'",
                    "text": "Complete Facebook login (and any two-step verification) in the opened "
                            "browser window, then close it. The session is verified automatically.",
                    "actions": []}
        if self.import_state.get("running"):
            return {"level": "info", "title": "Importing Chrome profile...",
                    "text": "Copying your logged-in Chrome session — this can take a minute. "
                            "Posting and scanning are paused until it finishes.",
                    "actions": []}
        if self.import_state.get("result") == "failed":
            return {"level": "error", "title": "Chrome import failed",
                    "text": self.import_state.get("message") or "Something went wrong while copying the profile.",
                    "actions": [{"id": "import", "label": "Try import again"}]}
        if self.verify_state.get("running"):
            return {"level": "info", "title": "Checking Facebook session...",
                    "text": "Opening a headless browser to confirm the selected account is logged in. "
                            "Give it a few seconds.",
                    "actions": []}
        if selected:
            st = self._profile_status(selected)
            if st["code"] in ("missing", "missing_dir", "error"):
                return {"level": "error", "title": st["label"],
                        "text": f"{st['detail']} - Re-add the account or import your Chrome session to fix it.",
                        "actions": [{"id": "import", "label": "Import from Chrome"},
                                    {"id": "remove", "label": "Remove account"}]}
            if st["code"] == "no_session":
                return {"level": "error",
                        "title": f"Account '{selected}' is not logged into Facebook",
                        "text": "This is the usual cause of \u201ccan't fetch the profile\u201d. Log in once "
                                "through the opened browser, or import your logged-in Chrome session.",
                        "actions": [{"id": "relogin", "label": "Log in now"},
                                    {"id": "import", "label": "Import from Chrome"},
                                    {"id": "verify", "label": "Re-check session"}]}
        return None

    # ------------------------------------------------------------------ helpers
    def _banner_guard(self):
        """Return an error payload if start/scan must not run right now."""
        if not self.config.profiles:
            return {"ok": False, "error": "No account set up yet. Add an account or import from Chrome first."}
        if self.worker.is_busy():
            return {"ok": False, "error": "A job is already running. Stop it first."}
        with self._lock:
            if self.login_state.get("running"):
                return {"ok": False, "error": "A login window is still open. Close it and wait for verification."}
            if self.import_state.get("running"):
                return {"ok": False, "error": "A Chrome import is still running. Wait for it to finish."}
            if self.verify_state.get("running"):
                return {"ok": False, "error": "A session check is still running. Wait a moment."}
        return None

    def _count_media(self):
        folder = self.config.settings.get("media_folder", "content")
        root = os.path.abspath(os.path.join(BASE_DIR, folder))
        n = 0
        if os.path.isdir(root):
            for _r, _dirs, names in os.walk(root):
                for name in names:
                    if os.path.splitext(name)[1].lower() in MEDIA_EXTS:
                        n += 1
        return n

    # ------------------------------------------------------------------ async jobs
    def _start_login(self, profile):
        with self._lock:
            self.login_state = {"running": True, "profile": profile["name"]}

        def job():
            try:
                # verify=False: we run a single headless check afterwards so the
                # stored result is always fresh and the UI can show it.
                open_login(profile["user_data_dir"], self._push_log, verify=False)
            except Exception as e:
                self._push_log(f"Login helper error: {e}")
            finally:
                with self._lock:
                    self.login_state["running"] = False
                self._start_verify(profile["name"])

        threading.Thread(target=job, daemon=True).start()

    def _start_verify(self, name):
        profile = self.config.get_profile(name)
        if not profile:
            return
        with self._lock:
            if self.verify_state.get("running"):
                return
            self.verify_state = {"running": True, "profile": name, "result": None, "message": ""}

        def job():
            try:
                self._push_log(f"Checking Facebook session for '{name}'...")
                fb = FacebookBrowser(profile["user_data_dir"], headless=True, log=self._push_log)
                fb.launch()
                ok = fb.is_logged_in(timeout_ms=45000)
                fb.close()
                if ok:
                    self.db.set_state(f"session_verified:{name}", "ok")
                    self._push_log("Session verified: logged in. You can start posting.")
                    with self._lock:
                        self.verify_state.update(running=False, result="ok", message="Logged in.")
                else:
                    self.db.set_state(f"session_verified:{name}", "no_session")
                    self._push_log("Not logged into Facebook on this profile. Log in once "
                                   "(opens a browser) or import your Chrome session.")
                    with self._lock:
                        self.verify_state.update(running=False, result="no_session",
                                                 message="Not logged in.")
            except Exception as e:
                self.db.set_state(f"session_verified:{name}", "error")
                self._push_log(f"Session check failed: {e}")
                with self._lock:
                    self.verify_state.update(running=False, result="error", message=str(e))

        threading.Thread(target=job, daemon=True).start()

    # ------------------------------------------------------------------ commands
    def cmd_start(self, data):
        guard = self._banner_guard()
        if guard:
            return guard
        profile = data.get("profile") or self._selected_name() or ""
        if not profile:
            return {"ok": False, "error": "No account selected."}
        keyword = (data.get("keyword") or "").strip()
        mode = data.get("mode", "search") or "search"
        min_members = _to_int(data.get("min_members"), 0)
        caption = str(data.get("caption") or "")
        folder = str(data.get("media_folder") or "").strip()
        updates = {}
        if folder:
            updates["media_folder"] = folder
        if caption:
            updates["caption"] = caption
        updates["min_members"] = min_members
        if updates:
            self.config.update_settings(**updates)
        self.config.set("last_keyword", keyword)
        self.config.set("last_filter_mode", mode)
        self.config.set("last_profile", profile)
        ok = self.worker.start_run(profile, keyword, mode, min_members, caption)
        if ok:
            self._push_log("Posting started...")
        return {"ok": ok, "busy": self.worker.is_busy(),
                "error": None if ok else "Could not start — see the log."}

    def cmd_scan(self, data):
        guard = self._banner_guard()
        if guard:
            return guard
        profile = data.get("profile") or self._selected_name() or ""
        if not profile:
            return {"ok": False, "error": "No account selected."}
        keyword = (data.get("keyword") or "").strip()
        mode = data.get("mode", "search") or "search"
        min_members = _to_int(data.get("min_members"), 0)
        self.config.update_settings(min_members=min_members)
        self.config.set("last_keyword", keyword)
        self.config.set("last_filter_mode", mode)
        self.config.set("last_profile", profile)
        ok = self.worker.start_scan(profile, keyword, mode, min_members)
        if ok:
            self._push_log("Scan started...")
        return {"ok": ok, "busy": self.worker.is_busy(),
                "error": None if ok else "Could not start — see the log."}

    def cmd_stop(self, data):
        self._push_log("Stopping after current post...")
        self.worker.stop()
        return {"ok": True}

    def cmd_pause(self, data):
        if self.worker.pause():
            self._push_log("Job paused.")
            return {"ok": True}
        return {"ok": False, "error": "Nothing to pause."}

    def cmd_unpause(self, data):
        if self.worker.unpause():
            self._push_log("Job resumed.")
            return {"ok": True}
        return {"ok": False, "error": "Not paused."}

    def cmd_join_all(self, data):
        guard = self._banner_guard()
        if guard:
            return guard
        profile = data.get("profile") or self._selected_name() or ""
        if not profile:
            return {"ok": False, "error": "No account selected."}
        if not self.db.count_groups():
            return {"ok": False, "error": "No discovered groups yet — run a scan first."}
        self.config.set("last_profile", profile)
        ok = self.worker.start_join(profile)
        if ok:
            self._push_log("Auto-join started...")
        return {"ok": ok, "busy": self.worker.is_busy(),
                "error": None if ok else "Could not start — see the log."}

    def cmd_check_join(self, data):
        guard = self._banner_guard()
        if guard:
            return guard
        profile = data.get("profile") or self._selected_name() or ""
        if not profile:
            return {"ok": False, "error": "No account selected."}
        if not self.db.count_groups():
            return {"ok": False, "error": "No discovered groups yet — run a scan first."}
        self.config.set("last_profile", profile)
        ok = self.worker.start_check(profile)
        if ok:
            self._push_log("Join-status check started...")
        return {"ok": ok, "busy": self.worker.is_busy(),
                "error": None if ok else "Could not start — see the log."}

    def cmd_sync(self, data):
        guard = self._banner_guard()
        if guard:
            return guard
        profile = data.get("profile") or self._selected_name() or ""
        if not profile:
            return {"ok": False, "error": "No account selected."}
        self.config.set("last_profile", profile)
        ok = self.worker.start_sync(profile)
        if ok:
            self._push_log("Syncing your full Facebook groups list...")
        return {"ok": ok, "busy": self.worker.is_busy(),
                "error": None if ok else "Could not start — see the log."}

    def cmd_blast(self, data):
        guard = self._banner_guard()
        if guard:
            return guard
        profile = data.get("profile") or self._selected_name() or ""
        if not profile:
            return {"ok": False, "error": "No account selected."}
        batch = _to_int(data.get("batch"), 10)
        if batch < 1:
            batch = 10
        # The caption arrives with the request (browser-held, not stored).
        caption = str(data.get("caption") or "")
        self.config.set("last_profile", profile)
        ok = self.worker.start_blast(profile, batch, caption)
        if ok:
            self._push_log(f"Batch posting started: {batch} group(s) per press...")
        return {"ok": ok, "busy": self.worker.is_busy(),
                "error": None if ok else "Could not start — see the log."}

    def cmd_page_post(self, data):
        guard = self._banner_guard()
        if guard:
            return guard
        profile = data.get("profile") or self._selected_name() or ""
        if not profile:
            return {"ok": False, "error": "No account selected."}
        page_url = str(data.get("page_url") or "").strip()
        if not page_url:
            return {"ok": False, "error": "No Page URL provided."}
        caption = str(data.get("caption") or "")
        file_name = str(data.get("file_name") or "")
        self.config.set("last_profile", profile)
        ok = self.worker.start_page_post(profile, page_url, caption, file_name)
        if ok:
            self._push_log(f"Page posting started: {page_url}")
        return {"ok": ok, "busy": self.worker.is_busy(),
                "error": None if ok else "Could not start — see the log."}

    def _interrupted_job(self):
        """A job that was running when the app last died (crash/power loss).

        A live run also writes active_job — so anything while the worker is
        busy is NOT an interruption and must never surface as one.
        """
        if self.worker.is_busy():
            return None
        raw = self.db.get_state("active_job", "") or ""
        if not raw:
            return None
        try:
            job = json.loads(raw)
        except Exception:
            return None
        if not isinstance(job, dict) or not job.get("type"):
            return None
        prog_raw = self.db.get_state("join_progress", "") or ""
        try:
            prog = json.loads(prog_raw) if prog_raw else {}
        except Exception:
            prog = {}
        job["progress"] = prog if isinstance(prog, dict) else {}

        # Record a stale (crash / power-loss) run into History exactly once so
        # the user can see it was left hanging. Keyed off the started_at so a
        # worker that finishes normally (clearing active_job) never triggers it.
        started = job.get("started_at") or ""
        mark = f"interrupt_recorded:{started}"
        if started and not self.db.get_state(mark, ""):
            self.db.set_state(mark, "1")
            self.db.add_history(
                job.get("type"),
                job.get("profile") or self._selected_name() or "",
                "interrupted",
                "Interrupted (app stopped or lost power while this run was active)",
                prog.get("done") if isinstance(prog.get("done"), int) else None,
                prog.get("total") if isinstance(prog.get("total"), int) else None,
                started,
                None,
            )
        return job

    def cmd_resume(self, data):
        job = self._interrupted_job()
        if not job:
            return {"ok": False, "error": "Nothing to resume."}
        profile = job.get("profile") or self._selected_name() or ""
        jtype = job["type"]
        if jtype == "join":
            ok = self.worker.start_join(profile)
        elif jtype == "scan":
            ok = self.worker.start_scan(profile, "", "search", 0)
        elif jtype == "check":
            ok = self.worker.start_check(profile)
        elif jtype == "sync":
            ok = self.worker.start_sync(profile)
        elif jtype == "blast":
            ok = self.worker.start_blast(profile)
        elif jtype == "run":
            ok = self.worker.start_run(profile)
        else:
            return {"ok": False, "error": f"Unknown job type '{jtype}'."}
        if ok:
            self._push_log(f"Resuming interrupted {jtype} run...")
        return {"ok": ok, "error": None if ok else "Could not start — see the log."}

    def cmd_discard_interrupted(self, data):
        self.db.set_state("active_job", "")
        self.db.set_state("join_done", "")
        self.db.set_state("join_progress", "")
        self._push_log("Interrupted-run state cleared.")
        return {"ok": True}

    def cmd_add_account(self, data):
        name = str(data.get("name") or "").strip()
        if not name:
            return {"ok": False, "error": "Enter an account name."}
        if not self.config.add_profile(name):
            return {"ok": False, "error": f"Account '{name}' already exists."}
        self.config.set("last_profile", name)
        profile = self.config.get_profile(name)
        self._push_log(f"Account '{name}' created. A browser will open for one-time login.")
        self._push_log("If two-step verification appears, complete it, then close the browser window.")
        self._start_login(profile)
        return {"ok": True}

    def cmd_relogin(self, data):
        name = data.get("name") or self._selected_name() or ""
        profile = self.config.get_profile(name)
        if not profile:
            return {"ok": False, "error": f"Unknown account '{name}'."}
        self.config.set("last_profile", name)
        self._push_log(f"Opening a browser for '{name}' — log in, then close the window.")
        self._start_login(profile)
        return {"ok": True}

    def cmd_import_chrome(self, data):
        name = str(data.get("name") or "").strip()
        chrome_profile = str(data.get("chrome_profile") or "")
        if not name:
            return {"ok": False, "error": "Enter an account name for the imported session."}
        if not chrome_profile:
            return {"ok": False, "error": "Choose which Chrome profile to import."}
        chrome_dir = find_chrome_user_data_dir()
        if not chrome_dir:
            return {"ok": False, "error": "Could not find a Chrome/Edge user-data folder. "
                                          "Make sure Chrome is installed and has been used at least once."}
        if not self.config.get_profile(name):
            self.config.add_profile(name)
        self.config.set("last_profile", name)
        profile = self.config.get_profile(name)
        self._push_log(f"Importing Chrome profile '{chrome_profile}' into '{name}'...")
        with self._lock:
            self.import_state = {"running": True, "profile": name, "result": None, "message": ""}

        def job():
            try:
                if chrome_running():
                    raise RuntimeError(
                        "Chrome is currently open. Close it fully (all windows and the "
                        "tray icon), then click Import again."
                    )
                skipped = import_chrome_session(profile["user_data_dir"], chrome_dir, chrome_profile, self._push_log)
                if skipped:
                    self._push_log(f"WARNING: {len(skipped)} locked file(s) skipped. "
                                   "Close Chrome fully and import again for a complete session.")
            except Exception as e:
                self._push_log(f"Import failed: {e}")
                with self._lock:
                    self.import_state.update(running=False, result="failed", message=str(e))
                return
            self._push_log("Import done. Verifying the session...")
            fb = FacebookBrowser(profile["user_data_dir"], headless=True, log=self._push_log)
            ok = False
            try:
                fb.launch()
                ok = fb.is_logged_in(timeout_ms=45000)
            except Exception as e:
                self._push_log(f"Session check failed: {e}")
            finally:
                fb.close()
            if ok:
                self.db.set_state(f"session_verified:{name}", "ok")
                self._push_log("Session verified: logged in. You can start posting.")
                with self._lock:
                    self.import_state.update(running=False, result="done",
                                             message="Imported and logged in.")
                return
            self._push_log(
                "The copied profile is not logged into Facebook. Modern Chrome "
                "encrypts its session cookies, so a copy can't carry the login. "
                "A normal Chrome window will open instead so you can log in once "
                "on this account (no passwords are stored by the app)."
            )
            with self._lock:
                self.import_state.update(running=False, result="login",
                                         message="Log in in the opened Chrome window.")
            self._start_login(profile)

        threading.Thread(target=job, daemon=True).start()
        return {"ok": True}

    def cmd_remove_account(self, data):
        name = str(data.get("name") or "")
        if name and self.config.get_profile(name):
            self.config.remove_profile(name)
            self._push_log(f"Removed account '{name}'. Profile files are kept on disk.")
        return {"ok": True}

    def cmd_verify(self, data):
        name = data.get("name") or self._selected_name() or ""
        profile = self.config.get_profile(name)
        if not profile:
            return {"ok": False, "error": f"Unknown account '{name}'."}
        if self.worker.is_busy():
            return {"ok": False, "error": "A job is running; the session can't be checked right now."}
        with self._lock:
            if self.login_state.get("running") or self.import_state.get("running") or self.verify_state.get("running"):
                return {"ok": False, "error": "Another browser operation is in progress. Wait a moment."}
        self._start_verify(name)
        return {"ok": True}

    def cmd_settings(self, data):
        s = self.config.settings
        updates = {}
        for key in ("delay_min", "delay_max", "soft_cap", "max_cycle_posts", "min_members",
                    "join_delay_min", "join_delay_max", "niche_max_members", "max_group_idle_days"):
            if key in data:
                updates[key] = _to_int(data[key], _to_int(s.get(key), 0))
        if "headless" in data:
            updates["headless"] = bool(data["headless"])
        if "scan_on_start" in data:
            updates["scan_on_start"] = bool(data["scan_on_start"])
        if "developer_mode" in data:
            updates["developer_mode"] = bool(data["developer_mode"])
        if "postable_only" in data:
            updates["postable_only"] = bool(data["postable_only"])
        if "niche_only" in data:
            updates["niche_only"] = bool(data["niche_only"])
        if "media_folder" in data and str(data.get("media_folder") or "").strip():
            updates["media_folder"] = str(data["media_folder"]).strip()
        if updates:
            self.config.update_settings(**updates)
            self._push_log("Settings saved.")
        return {"ok": True}

    def cmd_prefs(self, data):
        for key in ("last_profile", "last_keyword", "last_filter_mode"):
            if key in data and data[key] is not None:
                self.config.set(key, str(data[key]))
        return {"ok": True}

    def cmd_group_mark(self, data):
        gid = str(data.get("id") or "")
        status = str(data.get("status") or "")
        if status not in (STATUS_SAFE, STATUS_SKIP, STATUS_UNKNOWN):
            return {"ok": False, "error": "bad status"}
        g = self.db.get_group(gid)
        if not g:
            return {"ok": False, "error": "group not found"}
        self.db.set_group_status(gid, status, "manual")
        self._push_log(f"Marked {g['name'] or gid} as {status}.")
        return {"ok": True}

    def cmd_group_delete(self, data):
        gid = str(data.get("id") or "")
        g = self.db.get_group(gid)
        if g:
            self.db.conn.execute("DELETE FROM groups WHERE id = ?", (gid,))
            self.db.conn.commit()
            self._push_log(f"Deleted {g['name'] or gid}.")
        return {"ok": True}

    def cmd_logs_clear(self, data):
        with self._lock:
            self._log_lines.clear()
        return {"ok": True}

    # ---- posts CRUD ----
    def cmd_post_update(self, data):
        """Update a post row (status / error / post_url / group_name / file_path)."""
        try:
            post_id = int(data.get("id") or 0)
        except Exception:
            return {"ok": False, "error": "bad post id"}
        fields = {k: data.get(k) for k in (
            "status", "error", "post_url", "group_name", "file_path")}
        fields = {k: v for k, v in fields.items() if v is not None}
        updated = self.db.update_post(post_id, **fields)
        if updated is None:
            return {"ok": False, "error": "post not found"}
        self._push_log(f"Updated post #{post_id}.")
        return {"ok": True, "post": updated}

    def cmd_post_delete(self, data):
        """Delete a post row from the local history."""
        try:
            post_id = int(data.get("id") or 0)
        except Exception:
            return {"ok": False, "error": "bad post id"}
        if self.db.delete_post(post_id):
            self._push_log(f"Deleted post #{post_id} from history.")
            return {"ok": True}
        return {"ok": False, "error": "post not found"}

    def cmd_post_delete_group(self, data):
        """Delete every post row for a group."""
        gid = str(data.get("group_id") or "")
        if not gid:
            return {"ok": False, "error": "no group id"}
        n = self.db.delete_posts_by_group(gid)
        self._push_log(f"Deleted {n} post(s) for group {gid}.")
        return {"ok": True, "deleted": n}

    # ------------------------------------------------------------------ queries
    def groups_json(self, status="all"):
        rows = self.db.get_groups(None if status == "all" else status)
        keys = ("id", "name", "member_count", "approval_signal", "times_posted",
                "last_posted_at", "status", "url", "join_status", "join_checked_at")
        return {"groups": [{k: g.get(k) for k in keys} for g in rows]}

    def posts_json(self, limit=8):
        return {"posts": self.db.recent_posts(max(1, min(limit, 200)))}

    def history_json(self, limit=200):
        return {"ok": True, "history": self.db.get_history(max(1, min(limit, 1000)))}

    def chrome_profiles_json(self):
        d = find_chrome_user_data_dir()
        return {"available": bool(d), "dir": d or "",
                "profiles": list_chrome_profiles(d) if d else []}

    def list_dir(self, path):
        """Directory browser for the media-folder picker."""
        if not path or not str(path).strip():
            drives = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
            return {"ok": True, "path": "", "parent": None, "dirs": drives, "selectable": False}
        p = os.path.abspath(str(path))
        if not os.path.isdir(p):
            return {"ok": False, "error": f"Not a folder: {path}"}
        # A drive root (e.g. C:\) has no parent directory — surface "" so the
        # UI can climb back out to the drive list ("This PC") and keep full
        # navigation freedom to any drive/folder on the machine.
        if os.path.dirname(p) == p:
            parent = ""
        else:
            parent = os.path.dirname(p) if os.path.dirname(p) != p else None
        try:
            entries = sorted(os.listdir(p))
        except OSError as e:
            return {"ok": False, "error": str(e)}
        dirs = []
        for e in entries:
            try:
                if os.path.isdir(os.path.join(p, e)):
                    dirs.append(e)
            except OSError:
                continue
        return {"ok": True, "path": p, "parent": parent, "dirs": dirs, "selectable": True}

    def list_media(self):
        """List the image/video files in the configured media folder.

        Used by the frontend Content composer so the user can see and select
        which file(s) to post. Returns each media file's absolute path, its
        file name, and whether it is an image or a video.
        """
        folder = self.config.settings.get("media_folder", "content")
        root = os.path.abspath(os.path.join(BASE_DIR, folder))
        # Make sure the media folder exists so the composer's picker has a real,
        # browseable location — the UI currently shows an "empty folder" state
        # (no tiles to click) when the folder is missing.
        try:
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass
        files = []
        if os.path.isdir(root):
            for name in sorted(os.listdir(root)):
                full = os.path.join(root, name)
                if os.path.isfile(full):
                    ext = os.path.splitext(name)[1].lower()
                    if ext in MEDIA_EXTS:
                        files.append({
                            "name": name,
                            "path": full,
                            "folder": folder,
                            "is_video": ext in VIDEO_EXTS_SET,
                        })
        return {"ok": True, "folder": folder, "root": root,
                "exists": os.path.isdir(root), "files": files}

    def serve_media(self, path):
        """Serve a single image/video file (by absolute path) for previewing.

        Only media files are served — the extension must be a known image or
        video extension, which keeps this local endpoint safe.
        """
        p = os.path.abspath(str(path))
        ext = os.path.splitext(p)[1].lower()
        if ext not in MEDIA_EXTS or not os.path.isfile(p):
            return 404, "text/plain; charset=utf-8", b"not found"
        try:
            with open(p, "rb") as fh:
                return 200, MIME.get(ext, "application/octet-stream"), fh.read()
        except OSError:
            return 404, "text/plain; charset=utf-8", b"not found"

    def state_json(self):
        profiles = []
        for p in self.config.profiles:
            d = dict(p)
            d["status"] = self._profile_status(p["name"])
            profiles.append(d)
        names = [p["name"] for p in profiles]
        selected = self.config.get("last_profile", "")
        if selected not in names:
            selected = names[0] if names else ""
        with self._lock:
            login = dict(self.login_state)
            imp = dict(self.import_state)
            verify = dict(self.verify_state)
        sel = None
        if selected:
            sel = {"name": selected, "status": self._profile_status(selected)}
        cd = find_chrome_user_data_dir()
        chrome_profiles = list_chrome_profiles(cd) if cd else []
        group_counts = {
            "safe": self.db.count_groups(STATUS_SAFE),
            "skip": self.db.count_groups(STATUS_SKIP),
            "unknown": self.db.count_groups(STATUS_UNKNOWN),
            "total": self.db.count_groups(),
            "joined": self.db.count_by_join_status(JOIN_JOINED),
            "join_pending": self.db.count_by_join_status(JOIN_PENDING),
            "join_declined": self.db.count_by_join_status(JOIN_DECLINED),
            "not_joined": self.db.count_by_join_status(JOIN_NOT_JOINED),
        }
        w = self.worker
        # join progress comes from the database so it survives interruptions
        jp = None
        try:
            raw = self.db.get_state("join_progress", "") or ""
            jp = json.loads(raw) if raw else None
            if not isinstance(jp, dict):
                jp = None
        except Exception:
            jp = None
        # durable hard-stop notice (survives app restarts)
        hard_stop = None
        try:
            hs = self.db.get_state("hard_stop", "") or ""
            hard_stop = json.loads(hs) if hs else None
            if not isinstance(hard_stop, dict):
                hard_stop = None
        except Exception:
            hard_stop = None

        # durable batch-posting progress (survives app restarts)
        blast_progress = None
        try:
            bp = self.db.get_state("blast_progress", "") or ""
            blast_progress = json.loads(bp) if bp else None
            if not isinstance(blast_progress, dict):
                blast_progress = None
        except Exception:
            blast_progress = None
        try:
            bd_raw = self.db.get_state("blast_done", "") or ""
            blast_done = json.loads(bd_raw) if bd_raw else []
            if not isinstance(blast_done, list):
                blast_done = []
        except Exception:
            blast_done = []

        # groups actually posted to in the current batch cycle (id+name+members)
        try:
            br_raw = self.db.get_state("blast_result", "") or ""
            blast_result = json.loads(br_raw) if br_raw else []
            if not isinstance(blast_result, list):
                blast_result = []
        except Exception:
            blast_result = []
        inmem = getattr(w, "blast_posted", None)
        if inmem:
            blast_result = list(inmem)

        # last "scrape my groups" result (count + full found list)
        sync_result = None
        try:
            sr = self.db.get_state("sync_result", "") or ""
            sync_result = json.loads(sr) if sr else None
            if not isinstance(sync_result, dict):
                sync_result = None
        except Exception:
            sync_result = None

        return {
            "ok": True,
            "busy": w.is_busy(),
            "paused": getattr(w, "paused", False),
            "job": w.job,
            "status_text": getattr(w, "status_text", ""),
            "stage": getattr(w, "stage", None),
            "stage_detail": getattr(w, "stage_detail", ""),
            "join_progress": jp,
            "join_today": self.db.joins_today(),
            "hard_stop": hard_stop,
            "blast_progress": blast_progress,
            "blast_done": blast_done,
            "blast_result": blast_result,
            "sync_result": sync_result,
            "last_result": getattr(w, "last_result", None),
            "profiles": profiles,
            "selected": sel,
            "settings": dict(self.config.settings),
            "posts_today": self.db.posts_today(),
            "posted_today": self.db.posted_today_list(),
            "post_stats": self.db.post_stats(),
            "group_counts": group_counts,
            "media_count": self._count_media(),
            "log_id": self._log_lines[-1][0] if self._log_lines else 0,
            "chrome": {"available": bool(cd), "profiles": chrome_profiles},
            "login": login,
            "import": imp,
            "verify": verify,
            "interrupted_job": self._interrupted_job(),
            "system": self._system(selected),
        }

    def serve_static(self, relpath):
        relpath = relpath.lstrip("/") or "index.html"
        if ".." in relpath or relpath.startswith("/") or "\\" in relpath or os.path.isabs(relpath):
            return 400, "text/plain", b"bad path"
        full = os.path.join(WEB_DIR, relpath)
        if not os.path.isfile(full):
            return 404, "text/plain; charset=utf-8", b"not found"
        ctype = MIME.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as fh:
            return 200, ctype, fh.read()


class Handler(BaseHTTPRequestHandler):
    server_version = "GroupPostAutomator/1.0"

    def log_message(self, *args):  # keep the console clean
        pass

    # -- plumbing ------------------------------------------------------------
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Client disconnected mid-write (e.g. closed the tab or aborted a
            # poll while the page was navigating). WinError 10053/10054 and
            # broken pipes here are normal, NOT real API errors — swallow them
            # so they never surface as scary "API error" lines in the log.
            pass

    def _json(self, code, obj):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n <= 0:
                return {}
            raw = self.rfile.read(n).decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
        except Exception:
            return {}

    @property
    def st(self):
        return self.server.state

    # -- GET ------------------------------------------------------------------
    def do_GET(self):
        try:
            u = urlparse(self.path)
            p = u.path
            q = parse_qs(u.query)
            if p == "/api/state":
                return self._json(200, self.st.state_json())
            if p == "/api/logs":
                since = _to_int((q.get("since") or ["0"])[0], 0)
                return self._json(200, self.st.logs_json(since))
            if p == "/api/groups":
                status = (q.get("status") or ["all"])[0]
                return self._json(200, self.st.groups_json(status))
            if p == "/api/posts":
                limit = _to_int((q.get("limit") or ["8"])[0], 8)
                return self._json(200, self.st.posts_json(limit))
            if p == "/api/history":
                limit = _to_int((q.get("limit") or ["200"])[0], 200)
                return self._json(200, self.st.history_json(limit))
            if p == "/api/chrome_profiles":
                return self._json(200, self.st.chrome_profiles_json())
            if p == "/api/browse":
                path = (q.get("path") or [""])[0]
                return self._json(200, self.st.list_dir(path))
            if p == "/api/media":
                return self._json(200, self.st.list_media())
            if p == "/api/media_file":
                path = (q.get("path") or [""])[0]
                code, ctype, body = self.st.serve_media(path)
                return self._send(code, ctype, body)
            if p == "/favicon.ico":
                return self._send(204, "text/plain", b"")
            if p in ("/", "/index.html", "/style.css", "/app.js"):
                code, ctype, body = self.st.serve_static(p)
                return self._send(code, ctype, body)
            return self._json(404, {"ok": False, "error": "unknown endpoint"})
        except Exception as e:
            self.st._push_log(f"API error on GET {self.path}: {e}")
            return self._json(500, {"ok": False, "error": str(e)})

    # -- POST ------------------------------------------------------------------
    def do_POST(self):
        p = urlparse(self.path).path
        data = self._read_json()
        handlers = {
            "/api/start": self.st.cmd_start,
            "/api/scan": self.st.cmd_scan,
            "/api/join_all": self.st.cmd_join_all,
            "/api/check_join": self.st.cmd_check_join,
            "/api/sync": self.st.cmd_sync,
            "/api/blast": self.st.cmd_blast,
            "/api/page_post": self.st.cmd_page_post,
            "/api/resume": self.st.cmd_resume,
            "/api/discard_interrupted": self.st.cmd_discard_interrupted,
            "/api/stop": self.st.cmd_stop,
            "/api/pause": self.st.cmd_pause,
            "/api/unpause": self.st.cmd_unpause,
            "/api/add_account": self.st.cmd_add_account,
            "/api/relogin": self.st.cmd_relogin,
            "/api/import_chrome": self.st.cmd_import_chrome,
            "/api/remove_account": self.st.cmd_remove_account,
            "/api/settings": self.st.cmd_settings,
            "/api/prefs": self.st.cmd_prefs,
            "/api/verify_session": self.st.cmd_verify,
            "/api/group/mark": self.st.cmd_group_mark,
            "/api/group/delete": self.st.cmd_group_delete,
            "/api/post/update": self.st.cmd_post_update,
            "/api/post/delete": self.st.cmd_post_delete,
            "/api/post/delete_group": self.st.cmd_post_delete_group,
            "/api/logs/clear": self.st.cmd_logs_clear,
        }
        fn = handlers.get(p)
        if not fn:
            return self._json(404, {"ok": False, "error": "unknown endpoint"})
        try:
            return self._json(200, fn(data))
        except Exception as e:  # never let a backend hiccup kill the UI session
            self.st._push_log(f"API error on {p}: {e}")
            return self._json(500, {"ok": False, "error": str(e)})


def run_web_ui(host="127.0.0.1", port=0, open_browser=True):
    """Start the local server and (optionally) open the browser to the UI."""
    # If the worker ever wedges (e.g. a Playwright C call holds the GIL), the
    # whole process can stop answering HTTP. Dump all thread stacks to a file
    # every few seconds so we can see where it got stuck instead of guessing.
    try:
        dump_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "..", "data", "stack_dump.log")
        dump_path = os.path.normpath(dump_path)
        faulthandler.dump_traceback_later(3.0, repeat=True, file=open(dump_path, "w", encoding="utf-8"))
    except Exception:
        pass
    state = AutomationState()
    # A previously crashed run can leave an automation Chrome still holding a
    # profile lock, which makes the next launch take forever (Chrome delegates
    # to the old instance and never binds a fresh debug port) — the classic
    # "stuck on Launching Chrome / Server offline" symptom. Clean those up once
    # at boot, before the UI even tries to start anything. Bounded and
    # best-effort; never blocks startup for long.
    try:
        for p in state.config.profiles:
            d = p.get("user_data_dir") or ""
            if d:
                _kill_chrome_on_profile(d)
                _clean_stale_profile_locks(d)
    except Exception:
        pass
    port = port or 8756
    server = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), Handler)
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("Could not find a free port for the web UI.")
    server.state = state
    url = f"http://{host}:{server.server_port}/"
    if sys.stdout:
        print("=" * 62)
        print("  Group Post Automator  -  local web UI")
        print(f"  {url}")
        print("  Close this window or press Ctrl+C to quit.")
        print("=" * 62)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url, new=1)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if sys.stdout:
            print("\nShutting down...")
    finally:
        if state.worker.is_busy():
            if sys.stdout:
                print("Stopping worker...")
            state.worker.stop()
        state.db.close()
        server.server_close()
