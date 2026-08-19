"""SQLite persistence for groups, post history and rotation state."""
import os
import sqlite3
from datetime import datetime

from .config import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "bot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    id TEXT PRIMARY KEY,
    name TEXT,
    member_count INTEGER DEFAULT 0,
    url TEXT,
    status TEXT NOT NULL DEFAULT 'unknown',
    approval_signal TEXT,
    times_posted INTEGER DEFAULT 0,
    last_posted_at TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id TEXT,
    group_name TEXT,
    file_path TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS media_used (
    file_path TEXT PRIMARY KEY,
    last_used_at TEXT
);

CREATE TABLE IF NOT EXISTS run_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

STATUS_SAFE = "safe"
STATUS_SKIP = "skip"
STATUS_UNKNOWN = "unknown"


def _now():
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        self._lock = __import__("threading").Lock()

    def close(self):
        self.conn.close()

    # ---- groups ----
    def upsert_group(self, group_id, name=None, member_count=None, url=None):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM groups WHERE id = ?", (group_id,)
            ).fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO groups (id, name, member_count, url, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (group_id, name or "", member_count or 0, url or "", _now(), _now()),
                )
            else:
                self.conn.execute(
                    "UPDATE groups SET name = COALESCE(?, name), "
                    "member_count = COALESCE(?, member_count), "
                    "url = COALESCE(?, url), updated_at = ? WHERE id = ?",
                    (name, member_count, url, _now(), group_id),
                )
            self.conn.commit()

    def set_group_status(self, group_id, status, signal=""):
        with self._lock:
            self.conn.execute(
                "UPDATE groups SET status = ?, approval_signal = ?, updated_at = ? WHERE id = ?",
                (status, signal, _now(), group_id),
            )
            self.conn.commit()

    def get_group(self, group_id):
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM groups WHERE id = ?", (group_id,)
            ).fetchone()

    def get_groups(self, status=None):
        with self._lock:
            if status:
                rows = self.conn.execute(
                    "SELECT * FROM groups WHERE status = ? ORDER BY member_count DESC", (status,)
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM groups ORDER BY member_count DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def count_groups(self, status=None):
        with self._lock:
            if status:
                return self.conn.execute(
                    "SELECT COUNT(*) FROM groups WHERE status = ?", (status,)
                ).fetchone()[0]
            return self.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]

    # ---- posts ----
    def add_post(self, group_id, group_name, file_path, status, error=""):
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO posts (group_id, group_name, file_path, status, error, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (group_id, group_name, file_path, status, error, _now()),
            )
            if status == "posted":
                self.conn.execute(
                    "UPDATE groups SET times_posted = times_posted + 1, "
                    "last_posted_at = ? WHERE id = ?",
                    (_now(), group_id),
                )
            self.conn.commit()
            return cur.lastrowid

    def post_stats(self):
        with self._lock:
            row = self.conn.execute(
                "SELECT status, COUNT(*) FROM posts GROUP BY status"
            ).fetchall()
            return {r[0]: r[1] for r in row}

    def recent_posts(self, limit=200):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    # ---- media rotation ----
    def mark_media_used(self, file_path):
        with self._lock:
            self.conn.execute(
                "INSERT INTO media_used (file_path, last_used_at) VALUES (?, ?) "
                "ON CONFLICT(file_path) DO UPDATE SET last_used_at = excluded.last_used_at",
                (file_path, _now()),
            )
            self.conn.commit()

    def oldest_used_media(self):
        with self._lock:
            row = self.conn.execute(
                "SELECT file_path FROM media_used ORDER BY last_used_at ASC LIMIT 1"
            ).fetchone()
            return row["file_path"] if row else None

    # ---- run state ----
    def get_state(self, key, default=None):
        with self._lock:
            row = self.conn.execute(
                "SELECT value FROM run_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_state(self, key, value):
        with self._lock:
            self.conn.execute(
                "INSERT INTO run_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            self.conn.commit()