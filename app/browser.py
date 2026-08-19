"""Browser session manager.

Each account is a Chrome user-data-dir (a "profile"). The user logs in once
through a visible browser window; afterwards the tool can reuse that session,
headless or visible, without ever re-authenticating. Adding or switching
accounts never requires touching code.
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
    "--start-maximized",
]


class FacebookBrowser:
    """Owns one persistent Playwright context backed by a Chrome profile dir."""

    def __init__(self, user_data_dir, headless=True, log=None):
        self.user_data_dir = user_data_dir
        self.headless = headless
        self.log = log or (lambda msg: print(msg))
        self._pw = None
        self.context = None
        self.page = None

    def _emit(self, msg):
        try:
            self.log(msg)
        except Exception:
            pass

    def launch(self, url=None):
        os.makedirs(self.user_data_dir, exist_ok=True)
        self._pw = sync_playwright().start()
        self.context = self._pw.chromium.launch_persistent_context(
            user_data_dir=self.user_data_dir,
            headless=self.headless,
            args=DEFAULT_ARGS,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
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
            return not ("/login" in url or "login.php" in url)
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


def open_login(user_data_dir, log=None, close_after=True):
    """Open a visible browser for one-time Facebook login into a profile.

    Blocks while the user logs in. Returns after the user closes the window
    (or after close_after is used).
    """
    log = log or (lambda msg: print(msg))
    os.makedirs(user_data_dir, exist_ok=True)
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=False,
        args=[arg for arg in DEFAULT_ARGS if arg != "--start-maximized"],
        viewport={"width": 1280, "height": 850},
        locale="en-US",
    )
    context.add_init_script(STEALTH_SCRIPT)
    page = context.pages[0] if context.pages else context.new_page()
    log("Opening browser for login. Log into Facebook, then close the window.")
    page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
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
    log("Browser closed.")