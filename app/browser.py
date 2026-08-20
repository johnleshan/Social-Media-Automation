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
import time

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

    def _emit(self, msg):
        try:
            self.log(msg)
        except Exception:
            pass

    def _launch_context(self, headless, viewport):
        channel = _resolve_channel()
        kwargs = dict(
            user_data_dir=self.user_data_dir,
            headless=headless,
            args=DEFAULT_ARGS + (["--start-maximized"] if not headless else []),
            viewport=viewport,
            locale="en-US",
        )
        if channel:
            try:
                self.context = self._pw.chromium.launch_persistent_context(
                    channel=channel, **kwargs
                )
                self.channel = channel
                self._emit(f"Using real {channel.capitalize()} session.")
                return
            except Exception as e:
                self._emit(f"Could not use {channel} ({e}); using bundled Chromium.")
        self.context = self._pw.chromium.launch_persistent_context(**kwargs)
        self.channel = None

    def launch(self, url=None):
        os.makedirs(self.user_data_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        self._launch_context(
            self.headless, {"width": 1366, "height": 900}
        )
        self.context.add_init_script(STEALTH_SCRIPT)
        pages = self.context.pages
        self.page = pages[0] if pages else self.context.new_page()
        if url:
            self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
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
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.context = None
        self.page = None
        self._pw = None


def open_login(user_data_dir, log=None, verify=True):
    """Open a visible browser for one-time Facebook login into a profile.

    Blocks while the user logs in (including completing two-step verification
    if asked). Returns after the user closes the window. If verify is True, a
    quick headless check confirms the saved session afterwards.
    """
    log = log or (lambda msg: print(msg))
    os.makedirs(user_data_dir, exist_ok=True)
    pw = sync_playwright().start()
    channel = _resolve_channel()
    kwargs = dict(
        user_data_dir=user_data_dir,
        headless=False,
        args=DEFAULT_ARGS,
        viewport={"width": 1280, "height": 850},
        locale="en-US",
    )
    context = None
    if channel:
        try:
            context = pw.chromium.launch_persistent_context(channel=channel, **kwargs)
            log(f"Using real {channel.capitalize()} session.")
        except Exception:
            context = None
    if context is None:
        context = pw.chromium.launch_persistent_context(**kwargs)
    context.add_init_script(STEALTH_SCRIPT)
    page = context.pages[0] if context.pages else context.new_page()
    log(
        "A browser opened for login. Log into Facebook. If two-step "
        "verification appears, complete it. Then close the window."
    )
    page.goto("https://www.facebook.com/login/", wait_until="domcontentloaded", timeout=60000)
    # Heartbeat: report the URL every 5s so a stuck step is visible in the log.
    import threading
    stop_beat = threading.Event()

    def heartbeat():
        try:
            while not stop_beat.is_set() and context.pages and not context.pages[0].is_closed():
                try:
                    u = page.url
                    if u and "facebook.com" in u:
                        log(f"[page] {u}")
                except Exception:
                    pass
                time.sleep(5)
        except Exception:
            pass

    threading.Thread(target=heartbeat, daemon=True).start()
    try:
        while context.pages and not context.pages[0].is_closed():
            time.sleep(1)
    except Exception:
        pass
    stop_beat.set()
    try:
        context.close()
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
                "Session NOT logged in yet. Try again or import your Chrome profile.")
        except Exception as e:
            log(f"Could not verify session: {e}")
        finally:
            fb.close()