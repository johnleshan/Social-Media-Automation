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
import itertools
import json
import os
import string
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .browser import (
    FacebookBrowser,
    find_chrome_user_data_dir,
    import_chrome_session,
    list_chrome_profiles,
    open_login,
)
from .config import BASE_DIR, Config
from .database import STATUS_SAFE, STATUS_SKIP, STATUS_UNKNOWN, Database
from .worker import MEDIA_EXTS, Worker

WEB_DIR = os.path.join(BASE_DIR, "web")

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
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
        self.worker = Worker(self.config, self.db, log=self._push_log)
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
        """Diagnostic state for one account — surfaces the 'can't fetch the
        profile' scenario instead of failing silently."""
        profile = self.config.get_profile(name)
        if not profile:
            return {"code": "missing", "label": "Profile not found",
                    "detail": f"No account named '{name}' is configured.",
                    "dir_exists": False, "cookies": False, "stored": ""}
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
        detail = f"Session check: {stored or 'not run'}  •  Cookies present: {cookies}"
        return {"code": code, "label": label, "detail": detail,
                "dir_exists": dir_exists, "cookies": cookies, "stored": stored}

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
                import_chrome_session(profile["user_data_dir"], chrome_dir, chrome_profile, self._push_log)
            except Exception as e:
                self._push_log(f"Import failed: {e}")
                with self._lock:
                    self.import_state.update(running=False, result="failed", message=str(e))
                return
            cookies = os.path.join(profile["user_data_dir"], "Default", "Network", "Cookies")
            if not os.path.exists(cookies):
                self._push_log("IMPORTANT: Chrome's cookie file could not be copied because Chrome is "
                               "still open. Close Chrome fully, then run Import again for this account.")
                with self._lock:
                    self.import_state.update(running=False, result="failed",
                                             message="Cookie file could not be copied. Close Chrome fully and import again.")
                return
            self._push_log("Import done. Verifying the session...")
            with self._lock:
                self.import_state.update(running=False, result="done", message="Profile copied.")
            self._start_verify(name)

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
        for key in ("delay_min", "delay_max", "soft_cap", "max_cycle_posts", "min_members"):
            if key in data:
                updates[key] = _to_int(data[key], _to_int(s.get(key), 0))
        if "headless" in data:
            updates["headless"] = bool(data["headless"])
        if "scan_on_start" in data:
            updates["scan_on_start"] = bool(data["scan_on_start"])
        if "media_folder" in data and str(data.get("media_folder") or "").strip():
            updates["media_folder"] = str(data["media_folder"]).strip()
        if "caption" in data:
            updates["caption"] = str(data.get("caption") or "")
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

    # ------------------------------------------------------------------ queries
    def groups_json(self, status="all"):
        rows = self.db.get_groups(None if status == "all" else status)
        keys = ("id", "name", "member_count", "approval_signal", "times_posted",
                "last_posted_at", "status", "url")
        return {"groups": [{k: g.get(k) for k in keys} for g in rows]}

    def posts_json(self, limit=8):
        return {"posts": self.db.recent_posts(max(1, min(limit, 200)))}

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

    def state_json(self):
        profiles = [dict(p) for p in self.config.profiles]
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
        }
        return {
            "ok": True,
            "busy": self.worker.is_busy(),
            "job": self.worker.job,
            "profiles": profiles,
            "selected": sel,
            "settings": dict(self.config.settings),
            "posts_today": self.db.posts_today(),
            "post_stats": self.db.post_stats(),
            "group_counts": group_counts,
            "media_count": self._count_media(),
            "log_id": self._log_lines[-1][0] if self._log_lines else 0,
            "chrome": {"available": bool(cd), "profiles": chrome_profiles},
            "login": login,
            "import": imp,
            "verify": verify,
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
        except (BrokenPipeError, ConnectionResetError):
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
        if p == "/api/chrome_profiles":
            return self._json(200, self.st.chrome_profiles_json())
        if p == "/api/browse":
            path = (q.get("path") or [""])[0]
            return self._json(200, self.st.list_dir(path))
        if p == "/favicon.ico":
            return self._send(204, "text/plain", b"")
        if p in ("/", "/index.html", "/style.css", "/app.js"):
            code, ctype, body = self.st.serve_static(p)
            return self._send(code, ctype, body)
        return self._json(404, {"ok": False, "error": "unknown endpoint"})

    # -- POST ------------------------------------------------------------------
    def do_POST(self):
        p = urlparse(self.path).path
        data = self._read_json()
        handlers = {
            "/api/start": self.st.cmd_start,
            "/api/scan": self.st.cmd_scan,
            "/api/stop": self.st.cmd_stop,
            "/api/add_account": self.st.cmd_add_account,
            "/api/relogin": self.st.cmd_relogin,
            "/api/import_chrome": self.st.cmd_import_chrome,
            "/api/remove_account": self.st.cmd_remove_account,
            "/api/settings": self.st.cmd_settings,
            "/api/prefs": self.st.cmd_prefs,
            "/api/verify_session": self.st.cmd_verify,
            "/api/group/mark": self.st.cmd_group_mark,
            "/api/group/delete": self.st.cmd_group_delete,
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
    state = AutomationState()
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
        print("\nShutting down...")
    finally:
        if state.worker.is_busy():
            print("Stopping worker...")
            state.worker.stop()
        state.db.close()
        server.server_close()
