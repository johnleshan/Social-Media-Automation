"""Tkinter GUI for the bot.

Thin control layer over the Worker. The worker runs in a background thread and
streams log lines into a queue; the GUI polls the queue with `after()` so the
UI never blocks.
"""
import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

from .browser import open_login
from .config import Config
from .database import STATUS_SAFE, STATUS_SKIP, STATUS_UNKNOWN, Database
from .worker import Worker

LOG_COLORS = {
    "posted": "#1a7f37",
    "pending": "#b35900",
    "failed": "#b30000",
    "error": "#b30000",
    "check": "#0b5394",
    "warn": "#b35900",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Group Post Automator")
        self.geometry("980x720")
        self.minsize(860, 600)

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
        pad = {"padx": 6, "pady": 4}
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # Top: account bar
        acct = ttk.LabelFrame(outer, text="Account", padding=8)
        acct.pack(fill="x", **pad)
        ttk.Label(acct, text="Profile:").pack(side="left")
        self.acct_combo = ttk.Combobox(acct, state="readonly", width=24)
        self.acct_combo.pack(side="left", **pad)
        ttk.Button(acct, text="Add Account", command=self.add_account).pack(side="left", **pad)
        ttk.Button(acct, text="Manage Accounts", command=self.manage_accounts).pack(side="left", **pad)

        # Discovery
        disc = ttk.LabelFrame(outer, text="Group Discovery", padding=8)
        disc.pack(fill="x", **pad)
        ttk.Label(disc, text="Search / keyword:").grid(row=0, column=0, sticky="w")
        self.keyword_var = tk.StringVar()
        self.keyword_entry = ttk.Entry(disc, textvariable=self.keyword_var, width=40)
        self.keyword_entry.grid(row=0, column=1, sticky="we", padx=4)
        self.mode_var = tk.StringVar(value=self.config.get("last_filter_mode", "search"))
        ttk.Radiobutton(disc, text="Search string", value="search", variable=self.mode_var).grid(row=0, column=2)
        ttk.Radiobutton(disc, text="Hard filter", value="hard", variable=self.mode_var).grid(row=0, column=3)
        ttk.Label(disc, text="Min members:").grid(row=0, column=4, sticky="e")
        self.min_members_var = tk.StringVar(value=str(self.config.settings.get("min_members", 0)))
        ttk.Spinbox(disc, from_=0, to=10_000_000, increment=100, textvariable=self.min_members_var, width=10).grid(row=0, column=5, padx=4)
        self.scan_btn = ttk.Button(disc, text="Scan Groups", command=self.scan)
        self.scan_btn.grid(row=0, column=6, padx=6)
        disc.columnconfigure(1, weight=1)

        # Content / controls
        ctrl = ttk.LabelFrame(outer, text="Content & Controls", padding=8)
        ctrl.pack(fill="x", **pad)
        ttk.Label(ctrl, text="Media folder:").grid(row=0, column=0, sticky="w")
        self.folder_var = tk.StringVar(value=self.config.settings.get("media_folder", "content"))
        ttk.Entry(ctrl, textvariable=self.folder_var, width=46).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(ctrl, text="Browse...", command=self.browse_folder).grid(row=0, column=2)
        ttk.Button(ctrl, text="Settings...", command=self.open_settings).grid(row=0, column=3, padx=6)

        ttk.Label(ctrl, text="Caption:").grid(row=1, column=0, sticky="w")
        self.caption_var = tk.StringVar(value=self.config.settings.get("caption", ""))
        ttk.Entry(ctrl, textvariable=self.caption_var, width=60).grid(row=1, column=1, columnspan=2, sticky="we", padx=4)
        ctrl.columnconfigure(1, weight=1)

        btnrow = ttk.Frame(ctrl)
        btnrow.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.start_btn = ttk.Button(btnrow, text="Start Posting", command=self.start)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(btnrow, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.status_lbl = ttk.Label(btnrow, text="Idle", foreground="#666666")
        self.status_lbl.pack(side="left", padx=20)

        # Log + group tabs
        bottom = ttk.PanedWindow(outer, orient="vertical")
        bottom.pack(fill="both", expand=True, **pad)

        log_frame = ttk.LabelFrame(bottom, text="Log", padding=4)
        bottom.add(log_frame, weight=3)
        self.log_text = tk.Text(log_frame, height=12, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.tag_config("posted", foreground=LOG_COLORS["posted"])
        self.log_text.tag_config("pending", foreground=LOG_COLORS["pending"])
        self.log_text.tag_config("failed", foreground=LOG_COLORS["failed"])
        self.log_text.tag_config("check", foreground=LOG_COLORS["check"])
        self.log_text.tag_config("warn", foreground=LOG_COLORS["warn"])
        self.log_text.tag_config("error", foreground=LOG_COLORS["error"])

        tabs = ttk.LabelFrame(bottom, text="Groups", padding=4)
        bottom.add(tabs, weight=2)
        self.notebook = ttk.Notebook(tabs)
        self.notebook.pack(fill="both", expand=True)
        self.safe_tree = self._make_tab(self.notebook, "Safe Groups", STATUS_SAFE)
        self.skip_tree = self._make_tab(self.notebook, "Skip (approval)", STATUS_SKIP)
        self.unknown_tree = self._make_tab(self.notebook, "Review", STATUS_UNKNOWN)

    def _make_tab(self, notebook, title, status):
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)
        cols = ("name", "members", "signal", "posted", "last")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=6)
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
        btnrow.pack(fill="x", pady=4)
        ttk.Button(btnrow, text="Refresh", command=lambda: self._load_tab(tree, status)).pack(side="left", padx=2)
        if status == STATUS_UNKNOWN:
            ttk.Button(btnrow, text="Mark Safe", command=lambda: self._set_sel(tree, STATUS_SAFE)).pack(side="left", padx=2)
            ttk.Button(btnrow, text="Mark Skip", command=lambda: self._set_sel(tree, STATUS_SKIP)).pack(side="left", padx=2)
            ttk.Button(btnrow, text="Delete", command=lambda: self._del_sel(tree)).pack(side="left", padx=2)
        elif status == STATUS_SAFE:
            ttk.Button(btnrow, text="Mark Skip", command=lambda: self._set_sel(tree, STATUS_SKIP)).pack(side="left", padx=2)
            ttk.Button(btnrow, text="Delete", command=lambda: self._del_sel(tree)).pack(side="left", padx=2)
        else:
            ttk.Button(btnrow, text="Mark Safe", command=lambda: self._set_sel(tree, STATUS_SAFE)).pack(side="left", padx=2)
            ttk.Button(btnrow, text="Delete", command=lambda: self._del_sel(tree)).pack(side="left", padx=2)
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
            messagebox.showwarning("Add Account", f"Profile '{name}' already exists.")
            return
        self._refresh_accounts()
        self.acct_combo.set(name)
        self._log("info", f"Account '{name}' created. A browser will open for one-time login.")
        profile = self.config.get_profile(name)
        t = threading.Thread(target=open_login, args=(profile["user_data_dir"], self.log_q.put), daemon=True)
        t.start()

    def manage_accounts(self):
        names = [p["name"] for p in self.config.profiles]
        win = tk.Toplevel(self)
        win.title("Manage Accounts")
        win.geometry("320x280")
        lb = tk.Listbox(win)
        lb.pack(fill="both", expand=True, padx=8, pady=8)
        for n in names:
            lb.insert("end", n)
        def remove():
            sel = lb.curselection()
            if not sel:
                return
            n = lb.get(sel[0])
            if messagebox.askyesno("Remove", f"Remove account '{n}'? Profile files are kept on disk."):
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
        ttk.Button(win, text="Re-login", command=relogin).pack(side="left", padx=8)
        ttk.Button(win, text="Remove", command=remove).pack(side="left", padx=8)
        ttk.Button(win, text="Close", command=win.destroy).pack(side="right", padx=8)

    # ---------- actions ----------
    def browse_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.folder_var.set(d)

    def open_settings(self):
        s = self.config.settings
        win = tk.Toplevel(self)
        win.title("Settings")
        win.geometry("360x300")
        row = 0
        fields = {}
        for key, label, lo, hi in [
            ("delay_min", "Min delay (sec)", 30, 86400),
            ("delay_max", "Max delay (sec)", 30, 86400),
            ("soft_cap", "Daily soft cap (0=off)", 0, 10000),
            ("max_cycle_posts", "Max posts per cycle (0=off)", 0, 10000),
        ]:
            ttk.Label(win, text=label).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            var = tk.StringVar(value=str(s.get(key, lo)))
            ttk.Entry(win, textvariable=var, width=16).grid(row=row, column=1, padx=8)
            fields[key] = var
            row += 1
        self.headless_var = tk.BooleanVar(value=bool(s.get("headless", True)))
        ttk.Checkbutton(win, text="Hidden (headless) mode", variable=self.headless_var).grid(row=row, column=0, columnspan=2, sticky="w", padx=8)
        row += 1
        self.scan_on_start_var = tk.BooleanVar(value=bool(s.get("scan_on_start", True)))
        ttk.Checkbutton(win, text="Re-check unclassified groups on start", variable=self.scan_on_start_var).grid(row=row, column=0, columnspan=2, sticky="w", padx=8)
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
        ttk.Button(win, text="Save", command=save).grid(row=row, column=0, padx=8, pady=12)
        ttk.Button(win, text="Cancel", command=win.destroy).grid(row=row, column=1)

    def scan(self):
        profile = self._current_profile()
        if not profile:
            messagebox.showwarning("Scan", "Add and select an account first.")
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
            messagebox.showwarning("Start", "Add and select an account first.")
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
        tag = kind if kind in LOG_COLORS else None
        self.log_text.insert("end", f"[{ts}] {msg}\n", (tag,) if tag else ())
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

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
                elif "warn" in msg.lower() or "waiting" in msg.lower() or "checkpoint" in msg.lower():
                    kind = "warn"
                self._log(kind, msg)
        except queue.Empty:
            pass
        busy = self.worker.is_busy()
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.scan_btn.configure(state="disabled" if busy else "normal")
        self.stop_btn.configure(state="normal" if busy else "disabled")
        if busy:
            posted = self.db.posts_today()
            self.status_lbl.configure(text=f"Running... {posted} posted today", foreground="#0b5394")
        else:
            self.status_lbl.configure(text="Idle", foreground="#666666")
        self.after(200, self._poll)

    def on_close(self):
        if self.worker.is_busy():
            if not messagebox.askyesno("Quit", "A job is running. Stop it and quit?"):
                return
            self.worker.stop()
        self.config.update_settings(media_folder=self.folder_var.get(), caption=self.caption_var.get())
        self.db.close()
        self.destroy()


def run_gui():
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()