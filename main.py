"""Entry point.

Default: starts the local web UI and opens the browser. The web app now runs
inside a small supervisor that watches it and restarts it automatically if it
crashes (e.g. a native Playwright/Greenlet segfault that used to leave the web
UI stuck on "Server offline"). The web server + worker themselves are unchanged;
only the process that hosts them is supervised.
  --gui : legacy Tkinter desktop UI (run directly, not supervised).
  --cli : minimal console mode (run directly, not supervised).
"""
import os
import subprocess
import sys
import time

_APP_CHILD_ENV = "GPA_APP_CHILD"
_SUPERVISOR_PID_ENV = "GPA_SUPERVISOR_PID"


def _is_child():
    return os.environ.get(_APP_CHILD_ENV) == "1"


def _run_child():
    env = dict(os.environ)
    env[_APP_CHILD_ENV] = "1"
    # The child watches THIS pid (the supervisor interpreter), not its immediate
    # parent, because the venv launcher sits between them and outlives nothing.
    env[_SUPERVISOR_PID_ENV] = str(os.getpid())
    root = os.path.dirname(os.path.abspath(__file__))
    return subprocess.Popen(
        [sys.executable, os.path.abspath(__file__)],
        cwd=root,
        env=env,
    )


def _spawn_parent_watchdog():
    """The child checks that its supervisor parent is still alive; if the parent
    is killed (e.g. the user closes the old way), the child shuts itself down
    instead of lingering as an orphan holding the port and Chrome profile locks."""
    try:
        import threading
        import psutil
        pids = [int(x) for x in
                (os.environ.get(_SUPERVISOR_PID_ENV, "").strip().split(",")
                 if os.environ.get(_SUPERVISOR_PID_ENV) else [])
                if x.strip().isdigit()]
        if not pids:
            pids = [os.getppid()]

        def watch():
            while True:
                try:
                    if not any(psutil.pid_exists(p) for p in pids):
                        os._exit(0)
                except Exception:
                    pass
                time.sleep(2)

        threading.Thread(target=watch, daemon=True).start()
    except Exception:
        pass


def _signal_handlers():
    # Let Ctrl+C reach the child too (both share the console), and avoid the
    # supervisor dying before it can wait for the child to exit.
    try:
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:
        pass


def run_supervisor():
    """Run the actual app in a child process and keep it alive: if the child
    crashes (native fault, our most common failure), restart it after a short
    backoff so the user is never stuck on 'Server offline' and never has to
    manually restart the app."""
    print("=" * 62)
    print("  Group Post Automator  -  supervisor")
    print("  The app auto-restarts if it ever crashes.")
    print("  Close this window or press Ctrl+C to quit.")
    print("=" * 62, flush=True)
    _signal_handlers()
    crash_count = 0
    child = None
    try:
        while True:
            child = _run_child()
            code = child.wait()
            if code == 0:
                crash_count = 0
                break
            crash_count += 1
            print(f"App exited unexpectedly (code {code}). Restarting...", flush=True)
            if crash_count >= 30:
                print("Too many crashes in a row. Stopping the supervisor.")
                break
            for _ in range(10):  # ~2s interruptible backoff
                time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if child is not None and child.poll() is None:
            try:
                child.terminate()
            except Exception:
                pass


def main():
    if "--cli" in sys.argv:
        run_cli()
    elif "--gui" in sys.argv:
        from app.gui import run_gui
        run_gui()
    elif _is_child():
        from app.webserver import run_web_ui
        _spawn_parent_watchdog()
        # Never auto-open the browser from the child: webbrowser.open() inside
        # the process that hosts the Playwright worker natively crashes. The URL
        # is printed and the user opens it manually (opt-in via GPA_OPEN_BROWSER=1).
        run_web_ui(open_browser=False)
    else:
        run_supervisor()


def run_cli():
    """Minimal command-line mode: prompts, then runs until Ctrl+C."""
    from app.config import Config
    from app.database import Database
    from app.worker import Worker

    config = Config()
    db = Database()

    names = [p["name"] for p in config.profiles]
    if not names:
        print("No accounts configured. Add one from the GUI first.")
        return
    print("Accounts:", ", ".join(names))
    profile = input(f"Account [{names[0]}]: ").strip() or names[0]

    keyword = input("Search / keyword (empty to use saved groups): ").strip()
    mode = input("Filter mode [search/hard]: ").strip().lower() or "search"
    if mode not in ("search", "hard"):
        mode = "search"

    worker = Worker(config, db, log=print)
    ok = worker.start_run(profile, keyword, mode, int(config.settings.get("min_members", 0)), config.settings.get("caption", ""))
    if not ok:
        return
    print("Running. Press Ctrl+C to stop.")
    try:
        while worker.is_busy():
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopping...")
        worker.stop()
    db.close()


if __name__ == "__main__":
    main()
