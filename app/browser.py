"""Browser session manager.

Each account is a Chrome user-data-dir (a "profile"). The user logs in once
through a visible browser window; afterwards the tool can reuse that session,
headless or visible, without ever re-authenticating. Adding or switching
accounts never requires touching code.

Uses real installed Chrome when available (Facebook's login / 2FA pages hang
in Playwright's bundled Chromium), falling back to bundled Chromium otherwise.
"""
import os
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
        """Best-effort check that we have a working Facebook session."""
        if self.page is None:
            return False
        try:
            self.page.goto(
                "https://www.facebook.com/", wait_until="domcontentloaded",
                timeout=timeout_ms,
            )
            self.page.wait_for_timeout(2500)
            url = self.page.url
            return not ("/login" in url or "login.php" in url or "two_step" in url)
        except Exception:
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


def open_login(user_data_dir, log=None):
    """Open a visible browser for one-time Facebook login into a profile.

    Blocks while the user logs in (including completing two-step verification
    if asked). Returns after the user closes the window.
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
    try:
        while context.pages and not context.pages[0].is_closed():
            time.sleep(1)
    except Exception:
        pass
    try:
        context.close()
    except Exception:
        pass
    try:
        pw.stop()
    except Exception:
        pass
    log("Login window closed.")