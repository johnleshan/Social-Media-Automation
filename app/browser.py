"""Browser session manager.

Each account is a Chrome user-data-dir (a "profile"). The user logs in once
through a visible browser window; afterwards the tool can reuse that session,
headless or visible, without ever re-authenticating. Adding or switching
accounts never requires touching code.

Uses real installed Chrome when available (Facebook's login / 2FA pages hang
in Playwright's bundled Chromium), falling back to bundled Chromium otherwise.
"""
import os
import shutil
import socket
import subprocess
import time
import urllib.request

from playwright.sync_api import sync_playwright

STEALTH_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = window.chrome || { runtime: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5]
});
const originalQuery = window.navigator.permissions && window.navigator.permissions.query;
if (originalQuery) {
    window.navigator.permissions.query = (parameters) => (
        parameters.name === 'notifications'
            ? Promise.resolve({ state: Notification.permission })
            : originalQuery(parameters)
    );
}
"""

DEFAULT_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]

CHROME_EXES = [
    os.path.join(os.environ.get("PROGRAMFILES", ""), "Google/Chrome/Application/chrome.exe"),
    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google/Chrome/Application/chrome.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]
EDGE_EXES = [
    os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Microsoft/Edge/Application/msedge.exe"),
    os.path.join(os.environ.get("PROGRAMFILES", ""), "Microsoft/Edge/Application/msedge.exe"),
]


def _resolve_channel():
    if any(os.path.exists(p) for p in CHROME_EXES):
        return "chrome"
    if any(os.path.exists(p) for p in EDGE_EXES):
        return "msedge"
    return None


# --- importing an existing logged-in Chrome session -------------------------
CHROME_USER_DATA_DIRS = [
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
]

# Subfolders that are pure cache / not needed for a working session.
SKIP_DIR_NAMES = {
    "Cache", "Code Cache", "GPUCache", "GrShaderCache", "DawnGraphiteCache",
    "DawnWebGPUCache", "ShaderCache", "component_crx_cache",
    "extensions_crx_cache", "Crashpad", "Safe Browsing", "Segmentation Platform",
    "Shared Dictionary", "blob_storage", "Sync Data", "WebStorage",
    "AutofillAiModelCache", "optimization_guide_model_store", "BrowserMetrics",
    "InterestGroups", "DIPS", "Windows",
}
# Files that are locks/singletons or unneeded bulk.
SKIP_FILE_NAMES = {
    "LOCK", "LOG", "SingletonLock", "SingletonCookie", "SingletonSocket",
    "History", "History-journal", "Top Sites", "Top Sites-journal",
    "Favicons", "Favicons-journal", "Web Data", "Web Data-journal",
    "Last Session", "Last Tabs", "Current Session", "Current Tabs",
    "First Run",
    "Login Data", "Login Data-journal", "Login Data For Account",
    "Login Data For Account-journal",
}

# File/dir names that must NEVER be copied (must always be in the skip set).
FORCED_SKIP = {"SingletonLock", "SingletonCookie", "SingletonSocket", "LOCK"}


def find_chrome_user_data_dir():
    for d in CHROME_USER_DATA_DIRS:
        if os.path.isdir(d):
            return d
    return None


def list_chrome_profiles(user_data_dir):
    """Return profile directory names found under a Chrome user-data-dir."""
    profiles = []
    if not user_data_dir or not os.path.isdir(user_data_dir):
        return profiles
    for entry in sorted(os.listdir(user_data_dir)):
        full = os.path.join(user_data_dir, entry)
        if os.path.isdir(full) and os.path.exists(os.path.join(full, "Preferences")):
            if entry == "Default" or entry.startswith("Profile "):
                profiles.append(entry)
    return profiles


def _skip_copytree(src, dst, log=None, skip_names=None):
    """Copy a profile tree, skipping caches/locks and tolerating locked files.

    Returns a list of files that could not be copied (typically because Chrome
    is still running and holding them open).
    """
    skip_names = skip_names or set()
    skipped_locked = []
    if os.path.exists(dst):
        shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    skip_dirs = {s.lower() for s in SKIP_DIR_NAMES} | {s.lower() for s in skip_names}
    skip_files = {s.lower() for s in SKIP_FILE_NAMES} | {s.lower() for s in skip_names}

    for root, dirs, names in os.walk(src):
        rel = os.path.relpath(root, src)
        dest_root = dst if rel == "." else os.path.join(dst, rel)
        dirs[:] = [d for d in dirs if d.lower() not in skip_dirs]
        os.makedirs(dest_root, exist_ok=True)
        for name in names:
            if name.lower() in skip_files:
                continue
            src_file = os.path.join(root, name)
            dst_file = os.path.join(dest_root, name)
            try:
                shutil.copy2(src_file, dst_file)
            except OSError as e:
                skipped_locked.append(src_file)
                if log:
                    log(f"skipped locked file (close Chrome for a complete copy): {src_file} ({e})")
    return skipped_locked


def import_chrome_session(dest_user_data_dir, chrome_user_data_dir, profile_dir, log=None):
    """Copy a logged-in Chrome profile into the bot's profile folder.

    Copies the profile (minus caches/locks) plus the top-level Local State so
    cookies keep working on this machine. Same-machine/OS-user only.
    """
    log = log or (lambda msg: print(msg))
    src_profile = os.path.join(chrome_user_data_dir, profile_dir)
    local_state = os.path.join(chrome_user_data_dir, "Local State")
    if not os.path.isdir(src_profile):
        raise FileNotFoundError(f"Chrome profile not found: {src_profile}")

    os.makedirs(dest_user_data_dir, exist_ok=True)
    # Clean any previous copy so stale files never linger.
    old_default = os.path.join(dest_user_data_dir, "Default")
    if os.path.isdir(old_default):
        shutil.rmtree(old_default, ignore_errors=True)

    if os.path.exists(local_state):
        shutil.copy2(local_state, os.path.join(dest_user_data_dir, "Local State"))

    log(f"Copying Chrome profile '{profile_dir}' -> bot profile...")
    skipped = _skip_copytree(
        src_profile, os.path.join(dest_user_data_dir, "Default"), log=log
    )
    if skipped:
        log(
            f"WARNING: {len(skipped)} file(s) could not be copied because Chrome "
            "is running. Close Chrome and import again for a complete session."
        )
    log("Profile copied.")
    return skipped


def chrome_running():
    """True if a Chrome/Edge process is currently running.

    A running browser holds its profile folders open, so an import would get
    locked/stale files (the session cookies live in the database that Chrome
    keeps open). Import requires Chrome to be fully closed.
    """
    for name in ("chrome.exe", "msedge.exe"):
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
                capture_output=True, text=True, timeout=10,
            ).stdout
            if name in out:
                return True
        except Exception:
            continue
    return False


def _chrome_exe_path():
    for p in CHROME_EXES + EDGE_EXES:
        if os.path.exists(p):
            return p
    return None


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_debug_endpoint(port, proc, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=2
            )
            return True
        except Exception:
            if proc.poll() is not None:
                return False
            time.sleep(0.5)
    return False


def launch_native_chrome(profile_dir, url, headless, log=None):
    """Launch the user's real Chrome (subprocess, no automation flags) on a
    profile dir with a debugging port. Returns (process, port).

    This is the reliable path on modern Chrome:
      * a native browser decrypts its own profile's cookies (app-bound
        encryption silently defeats profile copies / Playwright relaunches),
      * CDP reads those cookies live from the running process,
      * it is a normal Chrome window, so Facebook does not block login.
    """
    log = log or (lambda msg: print(msg))
    os.makedirs(profile_dir, exist_ok=True)
    exe = _chrome_exe_path()
    if not exe:
        raise RuntimeError("Could not find Chrome or Edge on this PC.")
    port = _free_port()
    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    if headless:
        cmd.append("--headless")
    cmd.append(url or "about:blank")
    log(f"Launching Chrome on profile (debug port {port})...")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not _wait_debug_endpoint(port, proc):
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(proc.pid), "/T"],
                capture_output=True, timeout=10,
            )
        except Exception:
            pass
        raise RuntimeError(
            "Chrome could not start for this profile. Close any Chrome "
            "windows that use the same profile and try again."
        )
    return proc, port


def _close_native_chrome(proc, browser=None, user_data_dir=None):
    """Gracefully close a native Chrome we launched (flush cookies), then
    force-kill any leftover processes that use that profile dir."""
    if browser is not None:
        try:
            for ctx in browser.contexts:
                for pg in list(ctx.pages):
                    try:
                        pg.close()
                    except Exception:
                        pass
        except Exception:
            pass
    deadline = time.time() + 8
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.5)
    if proc.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(proc.pid), "/T"],
                capture_output=True, timeout=10,
            )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if user_data_dir:
        try:
            _kill_chrome_on_profile(user_data_dir)
        except Exception:
            pass


def _short_path(p):
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(512)
        r = ctypes.windll.kernel32.GetShortPathNameW(p, buf, 512)
        return buf.value if r else p
    except Exception:
        return p


def _kill_chrome_on_profile(profile_dir):
    """Force-kill any Chrome/Edge process launched on the given profile dir.

    Only called at browser close (never on every launch). Runs under a hard
    deadline so a wedged Chrome can never hang the caller. We first filter by
    process name (cheap), then read the command line only of matching processes
    to find the profile path.
    """
    import psutil
    long_p = os.path.normpath(profile_dir).lower()
    short_p = _short_path(long_p).lower()
    killed = 0
    this_pid = os.getpid()
    deadline = time.time() + 8.0
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            if time.time() > deadline:
                break
            try:
                if proc.info.get("pid") == this_pid:
                    continue
                if (proc.info.get("name") or "").lower() not in ("chrome.exe", "msedge.exe"):
                    continue
                try:
                    plist = proc.cmdline() or []
                except Exception:
                    continue
                cmd = " ".join(plist).lower()
                if long_p in cmd or short_p in cmd:
                    try:
                        proc.kill()
                        killed += 1
                    except Exception:
                        try:
                            proc.terminate()
                        except Exception:
                            pass
            except Exception:
                continue
    except Exception:
        pass
    return killed


class FacebookBrowser:
    """Owns one persistent Playwright context backed by a Chrome profile dir."""

    def __init__(self, user_data_dir, headless=True, log=None):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.log = log or (lambda msg: print(msg))
        self._pw = None
        self.context = None
        self.page = None
        self.channel = None
        self._proc = None
        self._port = None
        self._browser = None

    def _emit(self, msg):
        try:
            self.log(msg)
        except Exception:
            pass

    def _launch_cdp(self, url=None):
        self._proc, self._port = launch_native_chrome(
            self.user_data_dir, url, self.headless, self._emit
        )
        self._browser = self._pw.chromium.connect_over_cdp(
            f"http://127.0.0.1:{self._port}"
        )
        self.context = self._browser.contexts[0]
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()

    def launch(self, url=None):
        os.makedirs(self.user_data_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        self._launch_cdp(url)
        return self.page

    def is_logged_in(self, timeout_ms=30000):
        """True only when a real Facebook session exists (c_user cookie).

        The `c_user` cookie is Facebook's definitive logged-in marker. URL
        checks are unreliable because Facebook renders its login form at "/"
        without redirecting.
        """
        if self.page is None:
            return False
        try:
            self.page.goto(
                "https://www.facebook.com/", wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self.page.wait_for_timeout(2500)
        except Exception:
            return False
        try:
            cookies = self.context.cookies()
            for c in cookies:
                if c["name"] == "c_user" and c.get("value"):
                    return True
        except Exception:
            pass
        return False

    def close(self):
        try:
            if self.context:
                self.context.close()
        except Exception as e:
            self._emit(f"browser close warning: {e}")
        if self._proc is not None:
            _close_native_chrome(self._proc, self._browser, self.user_data_dir)
            self._proc = None
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self._pw = None


def open_login(user_data_dir, log=None, verify=True):
    """Open a real Chrome window (no automation flags) on the profile for
    one-time Facebook login.

    Blocks while the user logs in (including two-step verification if asked),
    watching for the c_user cookie. Returns True when a session was detected,
    False otherwise. If verify is True, a quick headless check runs afterwards.
    """
    log = log or (lambda msg: print(msg))
    os.makedirs(user_data_dir, exist_ok=True)
    proc, port = launch_native_chrome(
        user_data_dir, "https://www.facebook.com/login/", headless=False, log=log
    )
    log("A normal Chrome window opened for login. Log into Facebook there "
        "(complete two-step verification if asked). It will close on its own "
        "once the session is detected.")
    pw = sync_playwright().start()
    browser = None
    logged_in = False
    try:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        ctx = browser.contexts[0]
        deadline = time.time() + 15 * 60
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                cookies = ctx.cookies()
                if any(c["name"] == "c_user" and c.get("value") for c in cookies):
                    time.sleep(5)
                    still = ctx.cookies()
                    if any(c["name"] == "c_user" and c.get("value") for c in still):
                        logged_in = True
                        break
            except Exception:
                pass
            time.sleep(2)
        if logged_in:
            log("Facebook login detected. Saving the session.")
        else:
            log("No Facebook login detected before the window was closed.")
    finally:
        _close_native_chrome(proc, browser, user_data_dir)
        try:
            if browser is not None:
                browser.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass
    log("Login window closed.")
    if verify:
        fb = FacebookBrowser(user_data_dir, headless=True, log=log)
        try:
            fb.launch()
            ok = fb.is_logged_in(timeout_ms=30000)
            log("Session verified: logged in." if ok else
                "Session NOT logged in yet. Try the login window again.")
        except Exception as e:
            log(f"Could not verify session: {e}")
        finally:
            fb.close()
    return logged_in