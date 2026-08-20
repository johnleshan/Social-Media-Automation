"""Entry point.

Default: starts the local web UI and opens the browser.
  --gui : legacy Tkinter desktop UI.
  --cli : minimal console mode (prompts, runs until Ctrl+C).
"""
import sys


def main():
    if "--cli" in sys.argv:
        run_cli()
    elif "--gui" in sys.argv:
        from app.gui import run_gui
        run_gui()
    else:
        from app.webserver import run_web_ui
        run_web_ui()


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
