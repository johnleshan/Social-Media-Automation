"""Modern Tkinter GUI for the bot (ttkbootstrap dark theme).

Thin control layer over the Worker. The worker runs in a background thread and
streams log lines into a queue; the GUI polls the queue with `after()` so the
UI never blocks.
"""
import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, simpledialog

import ttkbootstrap as ttk
from ttkbootstrap.dialogs import Messagebox
from ttkbootstrap import ScrolledText

from .browser import (
    FacebookBrowser,
    find_chrome_user_data_dir,
    import_chrome_session,
    list_chrome_profiles,
    open_login,
)
from .config import Config
from .database import STATUS_SAFE, STATUS_SKIP, STATUS_UNKNOWN, Database
from .worker import Worker

FONT = "Segoe UI"

LOG_TAGS = {
    "posted": "#4ade80",
    "pending": "#fbbf24",
    "failed": "#f87171",
    "check": "#60a5fa",
    "warn": "#fbbf24",
    "error": "#f87171",
    "info": "#cbd5e1",
}


class App(ttk.Window):
    def __init__(self):
        super().__init__(themename="darkly")
        self.title("Group Post Automator")
        self.geometry("1020x780")
        self.minsize(900, 640)

        self.config = Config()
        self.db = Database()
        self.log_q = queue.Queue()
        self.worker = Worker(self.config, self.db, log=self.log_q.put)

        self._build_ui()
        self._refresh_accounts()
        self.keyword_var.set(self.config.get("last_keyword", ""))
        self.after(200, self._poll)

    # ---------- UI construction ----------
    def _build_ui(self):
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)

        # Header
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 6))
        ttk.Label(
            header, text="Group Post Automator",
            font=(FONT, 20, "bold"),
        ).pack(side="left")
        ttk.Label(
            header, text="  Local Facebook group automation",
            font=(FONT, 10), bootstyle="secondary",
        ).pack(side="left", pady=(6, 0))
        ttk.Separator(outer).pack(fill="x", pady=6)

        # Account
        acct = self._card(outer, "ACCOUNT")
        ttk.Label(acct, text="Profile:", font=(FONT, 10)).pack(side="left")
        self.acct_combo = ttk.Combobox(acct, state="readonly", width=22)
        self.acct_combo.pack(side="left", padx=8)
        ttk.Button(acct, text="+ Add Account", bootstyle="info-outline", command=self.add_account).pack(side="left", padx=(6, 4))
        ttk.Button(acct, text="Import from Chrome", bootstyle="primary", command=self.import_from_chrome).pack(side="left", padx=4)
        ttk.Button(acct, text="Manage", bootstyle="secondary-outline", command=self.manage_accounts).pack(side="left", padx=4)

        # Discovery
        disc = self._card(outer, "GROUP DISCOVERY")
        ttk.Label(disc, text="Search / keyword", font=(FONT, 9), bootstyle="secondary").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.keyword_var = tk.StringVar()
        self.keyword_entry = ttk.Entry(disc, textvariable=self.keyword_var, width=42)
        self.keyword_entry.grid(row=0, column=1, sticky="we", padx=4)
        self.mode_var = tk.StringVar(value=self.config.get("last_filter_mode", "search"))
        ttk.Radiobutton(disc, text="Search", value="search", variable=self.mode_var, bootstyle="info-toolbutton").grid(row=0, column=2, padx=(10, 2))
        ttk.Radiobutton(disc, text="Hard filter", value="hard", variable=self.mode_var, bootstyle="info-toolbutton").grid(row=0, column=3)
        ttk.Label(disc, text="Min members", font=(FONT, 9), bootstyle="secondary").grid(row=0, column=4, sticky="e", padx=(14, 4))
        self.min_members_var = tk.StringVar(value=str(self.config.settings.get("min_members", 0)))
        ttk.Spinbox(disc, from_=0, to=10_000_000, increment=100, textvariable=self.min_members_var, width=9).grid(row=0, column=5, padx=4)
        self.scan_btn = ttk.Button(disc, text="Scan Groups", bootstyle="info", command=self.scan)
        self.scan_btn.grid(row=0, column=6, padx=(12, 0))
        disc.columnconfigure(1, weight=1)

        # Content & controls
        ctrl = self._card(outer, "CONTENT & CONTROLS")
        ttk.Label(ctrl, text="Media folder", font=(FONT, 9), bootstyle="secondary").grid(row=0, column=0, sticky="w", padx=(0, 6))
        self.folder_var = tk.StringVar(value=self.config.settings.get("media_folder", "content"))
        ttk.Entry(ctrl, textvariable=self.folder_var, width=52).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(ctrl, text="Browse...", bootstyle="secondary-outline", command=self.browse_folder).grid(row=0, column=2, padx=4)
        ttk.Button(ctrl, text="Settings", bootstyle="secondary-outline", command=self.open_settings).grid(row=0, column=3, padx=(10, 0))
        ctrl.columnconfigure(1, weight=1)

        ttk.Label(ctrl, text="Caption", font=(FONT, 9), bootstyle="secondary").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=(8, 0))
        self.caption_var = tk.StringVar(value=self.config.settings.get("caption", ""))
        ttk.Entry(ctrl, textvariable=self.caption_var, width=66).grid(row=1, column=1, columnspan=2, sticky="we", padx=4, pady=(8, 0))

        btnrow = ttk.Frame(ctrl)
        btnrow.grid(row=2, column=0, columnspan=4, sticky="w", pady=(14, 0))
        self.start_btn = ttk.Button(btnrow, text="Start Posting", bootstyle="success", width=16, command=self.start)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(btnrow, text="Stop", bootstyle="danger", width=10, command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.status_lbl = ttk.Label(btnrow, text="Idle", bootstyle="secondary")
        self.status_lbl.pack(side="left", padx=20)

        # Log + group tabs
        bottom = ttk.Panedwindow(outer, orient="vertical")
        bottom.pack(fill="both", expand=True, pady=(10, 0))

        log_frame = ttk.Labelframe(bottom, text="  LOG  ", padding=6)
        bottom.add(log_frame, weight=3)
        self.log_text = ScrolledText(
            log_frame, height=11, wrap="word", font=(FONT, 9),
            bootstyle="dark", state="disabled",
        )
        self.log_text.pack(fill="both", expand=True)
        for tag, color in LOG_TAGS.items():
            self.log_text.tag_config(tag, foreground=color)

        tabs = ttk.Labelframe(bottom, text="  GROUPS  ", padding=6)
        bottom.add(tabs, weight=2)
        self.notebook = ttk.Notebook(tabs, bootstyle="dark")
        self.notebook.pack(fill="both", expand=True)
        self.safe_tree = self._make_tab(self.notebook, "Safe Groups", STATUS_SAFE)
        self.skip_tree = self._make_tab(self.notebook, "Skip (approval)", STATUS_SKIP)
        self.unknown_tree = self._make_tab(self.notebook, "Review", STATUS_UNKNOWN)

    def _card(self, parent, title):
        card = ttk.Labelframe(parent, text=f"  {title}  ", padding=10)
        card.pack(fill="x", pady=(6, 0))
        return card

    def _make_tab(self, notebook, title, status):
        frame = ttk.Frame(notebook, padding=4)
        notebook.add(frame, text=title)
        cols = ("name", "members", "signal", "posted", "last")
        tree = ttk.Treeview(
            frame, columns=cols, show="headings", height=6, bootstyle="dark"
        )
        tree.heading("name", text="Group")
        tree.heading("members", text="Members")
        tree.heading("signal", text="Signal")
        tree.heading("posted", text="Times posted")
        tree.heading("last", text="Last posted")
        tree.column("name", width=260)
        tree.column("members", width=90, anchor="center")
        tree.column("signal", width=220)
        tree.column("posted", width=90, anchor="center")
        tree.column("last", width=140)
        tree.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")

        btnrow = ttk.Frame(frame)
        btnrow.pack(fill="x", pady=(6, 0))
        ttk.Button(btnrow, text="Refresh", bootstyle="secondary-outline", command=lambda: self._load_tab(tree, status)).pack(side="left", padx=2)
        if status in (STATUS_UNKNOWN, STATUS_SKIP):
            ttk.Button(btnrow, text="Mark Safe", bootstyle="success-outline", command=lambda: self._set_sel(tree, STATUS_SAFE)).pack(side="left", padx=2)
        if status in (STATUS_UNKNOWN, STATUS_SAFE):
            ttk.Button(btnrow, text="Mark Skip", bootstyle="warning-outline", command=lambda: self._set_sel(tree, STATUS_SKIP)).pack(side="left", padx=2)
        ttk.Button(btnrow, text="Delete", bootstyle="danger-outline", command=lambda: self._del_sel(tree)).pack(side="left", padx=2)
        return tree

    # ---------- account management ----------
    def _refresh_accounts(self):
        names = [p["name"] for p in self.config.profiles]
        self.acct_combo["values"] = names
        last = self.config.get("last_profile", "")
        if last in names:
            self.acct_combo.set(last)
        elif names:
            self.acct_combo.set(names[0])

    def _current_profile(self):
        return self.acct_combo.get()

    def add_account(self):
        name = simpledialog.askstring("Add Account", "Account name (e.g. main, work):")
        if not name:
            return
        name = name.strip()
        if not name:
            return
        if not self.config.add_profile(name):
            Messagebox.show_warning("Add Account", f"Profile '{name}' already exists.", parent=self)
            return
        self._refresh_accounts()
        self.acct_combo.set(name)
        self._log("info", f"Account '{name}' created. A browser will open for one-time login.")
        self._log("info", "If two-step verification appears, complete it, then close the browser window.")
        profile = self.config.get_profile(name)
        t = threading.Thread(target=open_login, args=(profile["user_data_dir"], self.log_q.put), daemon=True)
        t.start()

    def manage_accounts(self):
        names = [p["name"] for p in self.config.profiles]
        win = ttk.Toplevel(self)
        win.title("Manage Accounts")
        win.geometry("360x320")
        lb = tk.Listbox(win, bg="#1c1f26", fg="#e6e6e6", selectbackground="#3066be")
        lb.pack(fill="both", expand=True, padx=8, pady=8)
        for n in names:
            lb.insert("end", n)

        def remove():
            sel = lb.curselection()
            if not sel:
                return
            n = lb.get(sel[0])
            if Messagebox.show_question(f"Remove account '{n}'? Profile files are kept on disk.", "Remove", parent=win) == "Yes":
                self.config.remove_profile(n)
                self._refresh_accounts()
                win.destroy()

        def relogin():
            sel = lb.curselection()
            if not sel:
                return
            n = lb.get(sel[0])
            profile = self.config.get_profile(n)
            t = threading.Thread(target=open_login, args=(profile["user_data_dir"], self.log_q.put), daemon=True)
            t.start()

        def imp():
            win.destroy()
            self.import_from_chrome()

        btnrow = ttk.Frame(win)
        btnrow.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btnrow, text="Re-login (open window)", bootstyle="info-outline", command=relogin).pack(side="left", padx=2)
        ttk.Button(btnrow, text="Import from Chrome", bootstyle="primary", command=imp).pack(side="left", padx=2)
        ttk.Button(btnrow, text="Remove", bootstyle="danger-outline", command=remove).pack(side="left", padx=2)
        ttk.Button(btnrow, text="Close", bootstyle="secondary", command=win.destroy).pack(side="right", padx=2)

    # ---------- actions ----------
    def import_from_chrome(self):
        chrome_dir = find_chrome_user_data_dir()
        if not chrome_dir:
            Messagebox.show_error(
                "Could not find a Chrome/Edge user-data folder. "
                "Make sure Chrome is installed and has been used at least once.",
                "Import from Chrome", parent=self,
            )
            return
        profiles = list_chrome_profiles(chrome_dir)
        if not profiles:
            Messagebox.show_error("No Chrome profiles found.", "Import from Chrome", parent=self)
            return

        name = simpledialog.askstring("Import from Chrome", "Account name (e.g. main):")
        if not name:
            return
        name = name.strip()
        if not name:
            return

        if len(profiles) == 1:
            chosen = profiles[0]
        else:
            pick = ttk.Toplevel(self)
            pick.title("Choose Chrome profile")
            pick.geometry("340x180")
            ttk.Label(pick, text="Which Chrome profile has the account?").pack(padx=8, pady=8)
            var = tk.StringVar(value=profiles[0])
            combo = ttk.Combobox(pick, textvariable=var, values=profiles, state="readonly")
            combo.pack(padx=8, pady=4)
            result = {}

            def ok():
                result["profile"] = var.get()
                pick.destroy()

            ttk.Button(pick, text="Use this profile", bootstyle="primary", command=ok).pack(pady=8)
            self.wait_window(pick)
            if "profile" not in result:
                return
            chosen = result["profile"]

        proceed = Messagebox.show_question(
            "Tip: close Chrome fully first for a clean copy.\n\n"
            f"Import profile '{chosen}' into account '{name}'?",
            "Import from Chrome", parent=self,
        )
        if proceed != "Yes":
            return
        self._log("info", f"Importing Chrome profile '{chosen}' into '{name}'...")
        self.config.add_profile(name)
        profile = self.config.get_profile(name)

        def job():
            try:
                import_chrome_session(profile["user_data_dir"], chrome_dir, chosen, self.log_q.put)
            except Exception as e:
                self.log_q.put(f"Import failed: {e}")
                return
            cookies = os.path.join(profile["user_data_dir"], "Default", "Network", "Cookies")
            if not os.path.exists(cookies):
                self.log_q.put(
                    "IMPORTANT: Chrome's cookie file could not be copied because "
                    "Chrome is still open. Close Chrome fully, then run Import "
                    "again for this account."
                )
                return
            self.log_q.put("Import done. Verifying the session...")
            try:
                fb = FacebookBrowser(profile["user_data_dir"], headless=True, log=self.log_q.put)
                fb.launch()
                ok = fb.is_logged_in(timeout_ms=45000)
                fb.close()
                self.log_q.put(
                    "Session verified: logged in. You can start posting."
                    if ok else
                    "Imported, but the session is not logged into Facebook."
                )
            except Exception as e:
                self.log_q.put(f"Verification error: {e}")

        t = threading.Thread(target=job, daemon=True)
        t.start()
        self._refresh_accounts()
        self.acct_combo.set(name)

    def browse_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.folder_var.set(d)

    def open_settings(self):
        s = self.config.settings
        win = ttk.Toplevel(self)
        win.title("Settings")
        win.geometry("380x330")
        row = 0
        fields = {}
        for key, label, lo, hi in [
            ("delay_min", "Min delay (sec)", 30, 86400),
            ("delay_max", "Max delay (sec)", 30, 86400),
            ("soft_cap", "Daily soft cap (0=off)", 0, 10000),
            ("max_cycle_posts", "Max posts per cycle (0=off)", 0, 10000),
        ]:
            ttk.Label(win, text=label, font=(FONT, 9), bootstyle="secondary").grid(row=row, column=0, sticky="w", padx=10, pady=5)
            var = tk.StringVar(value=str(s.get(key, lo)))
            ttk.Entry(win, textvariable=var, width=16).grid(row=row, column=1, padx=10, pady=5)
            fields[key] = var
            row += 1
        self.headless_var = tk.BooleanVar(value=bool(s.get("headless", True)))
        ttk.Checkbutton(win, text="Hidden (headless) mode", variable=self.headless_var, bootstyle="round-toggle").grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=3)
        row += 1
        self.scan_on_start_var = tk.BooleanVar(value=bool(s.get("scan_on_start", True)))
        ttk.Checkbutton(win, text="Re-check unclassified groups on start", variable=self.scan_on_start_var, bootstyle="round-toggle").grid(row=row, column=0, columnspan=2, sticky="w", padx=10, pady=3)
        row += 1

        def save():
            vals = {}
            for key, var in fields.items():
                try:
                    vals[key] = int(var.get())
                except ValueError:
                    vals[key] = 0
            vals["headless"] = self.headless_var.get()
            vals["scan_on_start"] = self.scan_on_start_var.get()
            self.config.update_settings(**vals)
            win.destroy()
            self._log("info", "Settings saved.")

        btnrow = ttk.Frame(win)
        btnrow.grid(row=row, column=0, columnspan=2, pady=12)
        ttk.Button(btnrow, text="Save", bootstyle="success", command=save).pack(side="left", padx=6)
        ttk.Button(btnrow, text="Cancel", bootstyle="secondary", command=win.destroy).pack(side="left", padx=6)

    def scan(self):
        profile = self._current_profile()
        if not profile:
            Messagebox.show_warning("Scan", "Add and select an account first.", parent=self)
            return
        keyword = self.keyword_var.get().strip()
        if not keyword:
            self._log("warn", "No keyword entered; scanning unclassified groups only.")
        min_members = self._spin_int(self.min_members_var.get())
        self._save_gui_prefs(keyword, min_members)
        ok = self.worker.start_scan(profile, keyword, self.mode_var.get(), min_members)
        if ok:
            self._set_busy(True)
            self._log("info", "Scan started...")

    def start(self):
        profile = self._current_profile()
        if not profile:
            Messagebox.show_warning("Start", "Add and select an account first.", parent=self)
            return
        keyword = self.keyword_var.get().strip()
        min_members = self._spin_int(self.min_members_var.get())
        self.config.update_settings(media_folder=self.folder_var.get(), caption=self.caption_var.get())
        self._save_gui_prefs(keyword, min_members)
        ok = self.worker.start_run(profile, keyword, self.mode_var.get(), min_members, self.caption_var.get())
        if ok:
            self._set_busy(True)
            self._log("info", "Posting started...")

    def stop(self):
        self._log("warn", "Stopping after current post...")
        self.worker.stop()

    def _save_gui_prefs(self, keyword, min_members):
        self.config.set("last_keyword", keyword)
        self.config.set("last_filter_mode", self.mode_var.get())
        self.config.set("last_profile", self._current_profile())
        self.config.update_settings(min_members=min_members)

    @staticmethod
    def _spin_int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    # ---------- tab helpers ----------
    def _load_tab(self, tree, status):
        tree.delete(*tree.get_children())
        for g in self.db.get_groups(status):
            tree.insert("", "end", iid=g["id"], values=(
                g["name"] or g["id"],
                g["member_count"],
                g["approval_signal"] or "",
                g["times_posted"],
                (g["last_posted_at"] or "")[:16],
            ))

    def _selected_groups(self, tree):
        rows = []
        for iid in tree.selection():
            g = self.db.get_group(iid)
            if g:
                rows.append(g)
        return rows

    def _set_sel(self, tree, status):
        for g in self._selected_groups(tree):
            self.db.set_group_status(g["id"], status, "manual")
            self._log("info", f"Marked {g['name'] or g['id']} as {status}.")
        self._refresh_tabs()

    def _del_sel(self, tree):
        for g in self._selected_groups(tree):
            self.db.conn.execute("DELETE FROM groups WHERE id = ?", (g["id"],))
            self.db.conn.commit()
            self._log("info", f"Deleted {g['name'] or g['id']}.")
        self._refresh_tabs()

    def _refresh_tabs(self):
        self._load_tab(self.safe_tree, STATUS_SAFE)
        self._load_tab(self.skip_tree, STATUS_SKIP)
        self._load_tab(self.unknown_tree, STATUS_UNKNOWN)

    # ---------- logging / polling ----------
    def _log(self, kind, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        tag = kind if kind in LOG_TAGS else "info"
        self.log_text.insert("end", f"[{ts}] {msg}\n", tag)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _set_busy(self, busy):
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.scan_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")

    def _poll(self):
        try:
            while True:
                msg = self.log_q.get_nowait()
                kind = "info"
                up = msg.upper()
                if "[POSTED]" in up:
                    kind = "posted"
                elif "[PENDING]" in up:
                    kind = "pending"
                elif "[FAILED]" in up or "error" in msg.lower() or "failed" in msg.lower():
                    kind = "failed"
                elif "[check" in msg.lower() or "queue ready" in msg.lower():
                    kind = "check"
                elif "warn" in msg.lower() or "waiting" in msg.lower() or "checkpoint" in msg.lower() or "important" in msg.lower():
                    kind = "warn"
                self._log(kind, msg)
        except queue.Empty:
            pass
        busy = self.worker.is_busy()
        self._set_busy(busy)
        if busy:
            posted = self.db.posts_today()
            self.status_lbl.configure(text=f"Running... {posted} posted today", bootstyle="info")
        else:
            self.status_lbl.configure(text="Idle", bootstyle="secondary")
        self.after(200, self._poll)

    def on_close(self):
        if self.worker.is_busy():
            if Messagebox.show_question("A job is running. Stop it and quit?", "Quit", parent=self) != "Yes":
                return
            self.worker.stop()
        self.config.update_settings(media_folder=self.folder_var.get(), caption=self.caption_var.get())
        self.db.close()
        self.destroy()


def run_gui():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()