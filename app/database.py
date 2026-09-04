"""SQLite persistence for groups, post history, activity signals and rotation state."""
import os
import re
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
    join_status TEXT NOT NULL DEFAULT 'not_joined',
    join_checked_at TEXT,
    times_posted INTEGER DEFAULT 0,
    last_posted_at TEXT,
    last_active_days INTEGER,
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
    post_url TEXT,
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

CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    profile TEXT,
    status TEXT NOT NULL,
    summary TEXT,
    progress_done INTEGER,
    progress_total INTEGER,
    started_at TEXT,
    finished_at TEXT
);
"""

STATUS_SAFE = "safe"
STATUS_SKIP = "skip"
STATUS_UNKNOWN = "unknown"

JOIN_NOT_JOINED = "not_joined"
JOIN_PENDING = "pending"
JOIN_JOINED = "joined"
JOIN_DECLINED = "declined"
JOIN_LEFT = "left"


def _now():
    return datetime.now().isoformat(timespec="seconds")


_GENERIC_TITLE_RE = re.compile(
    r"^(?:all groups you'?ve joined|groups|group|your groups|discover|explore"
    r"|suggested|home|facebook|notifications|unread)\b.*",
    re.IGNORECASE,
)


def _is_generic_title(name):
    name = (name or "").strip()
    if not name:
        return True
    if _GENERIC_TITLE_RE.match(name):
        return True
    if re.fullmatch(r"\d[\d\s.,]*", name):
        return True
    return False


class Database:
    def __init__(self, path=DB_PATH):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        self._lock = __import__("threading").Lock()

    def _migrate(self):
        """Add columns that were introduced after the initial schema."""
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(groups)").fetchall()}
        if "join_status" not in cols:
            self.conn.execute(
                "ALTER TABLE groups ADD COLUMN join_status TEXT NOT NULL DEFAULT 'not_joined'"
            )
        if "join_checked_at" not in cols:
            self.conn.execute("ALTER TABLE groups ADD COLUMN join_checked_at TEXT")
        if "last_active_days" not in cols:
            self.conn.execute("ALTER TABLE groups ADD COLUMN last_active_days INTEGER")
        post_cols = {r[1] for r in self.conn.execute("PRAGMA table_info(posts)").fetchall()}
        if "post_url" not in post_cols:
            self.conn.execute("ALTER TABLE posts ADD COLUMN post_url TEXT")

    def close(self):
        self.conn.close()

    # ---- groups ----
    def upsert_group(self, group_id, name=None, member_count=None, url=None,
                     last_active_days=None):
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
                curr_name = row["name"] or ""
                curr_mc = row["member_count"] or 0
                curr_url = row["url"] or ""

                # Prefer real name over empty string, raw group id, or a generic
                # list-page title (e.g. "All groups you've joined (75)") that
                # leaks in when a group-page load bounces to the groups list.
                new_name = curr_name
                if name:
                    if not _is_generic_title(name) and (
                        not curr_name
                        or _is_generic_title(curr_name)
                        or curr_name == group_id
                        or (name != group_id and len(name) >= len(curr_name))
                    ):
                        new_name = name

                # Prefer positive member count over 0 / None
                new_mc = curr_mc
                if member_count is not None and member_count > 0:
                    new_mc = member_count

                new_url = url if url else (curr_url or f"https://www.facebook.com/groups/{group_id}/")

                new_active = row["last_active_days"]
                if last_active_days is not None:
                    new_active = last_active_days

                self.conn.execute(
                    "UPDATE groups SET name = ?, "
                    "member_count = ?, "
                    "url = ?, last_active_days = ?, updated_at = ? WHERE id = ?",
                    (new_name, new_mc, new_url, new_active, _now(), group_id),
                )
            self.conn.commit()

    def set_group_status(self, group_id, status, signal=""):
        with self._lock:
            self.conn.execute(
                "UPDATE groups SET status = ?, approval_signal = ?, updated_at = ? WHERE id = ?",
                (status, signal, _now(), group_id),
            )
            self.conn.commit()

    def set_join_status(self, group_id, join_status):
        with self._lock:
            self.conn.execute(
                "UPDATE groups SET join_status = ?, join_checked_at = ? WHERE id = ?",
                (join_status, _now(), group_id),
            )
            self.conn.commit()

    def record_join(self, group_id, result):
        """Record a join attempt and track daily count."""
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            key = f"joins_today:{today}"
            row = self.conn.execute(
                "SELECT value FROM run_state WHERE key = ?", (key,)
            ).fetchone()
            count = int(row["value"]) if row else 0
            self.conn.execute(
                "INSERT INTO run_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(count + 1)),
            )
            self.conn.commit()
            return count + 1

    def joins_today(self):
        """Return number of join attempts made today."""
        with self._lock:
            today = datetime.now().strftime("%Y-%m-%d")
            key = f"joins_today:{today}"
            row = self.conn.execute(
                "SELECT value FROM run_state WHERE key = ?", (key,)
            ).fetchone()
            return int(row["value"]) if row else 0

    def get_groups_by_join_status(self, *join_statuses):
        """Return groups matching any of the given join_status values."""
        with self._lock:
            placeholders = ",".join("?" for _ in join_statuses)
            rows = self.conn.execute(
                f"SELECT * FROM groups WHERE join_status IN ({placeholders}) ORDER BY member_count DESC",
                join_statuses,
            ).fetchall()
            return [dict(r) for r in rows]

    def count_by_join_status(self, *join_statuses):
        """Count groups matching any of the given join_status values."""
        with self._lock:
            if not join_statuses:
                return self.conn.execute(
                    "SELECT COUNT(*) FROM groups"
                ).fetchone()[0]
            placeholders = ",".join("?" for _ in join_statuses)
            return self.conn.execute(
                f"SELECT COUNT(*) FROM groups WHERE join_status IN ({placeholders})",
                join_statuses,
            ).fetchone()[0]

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
    def add_post(self, group_id, group_name, file_path, status, error="", post_url=""):
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO posts (group_id, group_name, file_path, status, error, post_url, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (group_id, group_name, file_path, status, error, post_url or "", _now()),
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

    def posts_today(self):
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM posts WHERE status='posted' AND created_at LIKE ?",
                (f"{today}%",),
            ).fetchone()[0]

    def posted_today_list(self):
        """Today's successfull posts (status='posted'), newest first, as dicts."""
        today = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM posts WHERE status='posted' AND created_at LIKE ? "
                "ORDER BY id DESC",
                (f"{today}%",),
            ).fetchall()
            return [dict(r) for r in rows]

    def recent_posts(self, limit=200):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]

    def get_post(self, post_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM posts WHERE id = ?", (int(post_id),)
            ).fetchone()
            return dict(row) if row else None

    def update_post(self, post_id, **fields):
        """Update editable fields of a post row. Allowed: status, error, post_url,
        group_name, file_path."""
        allowed = {"status", "error", "post_url", "group_name", "file_path"}
        updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not updates:
            return None
        with self._lock:
            cur = self.conn.execute(
                f"UPDATE posts SET {', '.join(k + ' = ?' for k in updates)} WHERE id = ?",
                (*updates.values(), int(post_id)),
            )
            self.conn.commit()
            if cur.rowcount == 0:
                return None
            row = self.conn.execute(
                "SELECT * FROM posts WHERE id = ?", (int(post_id),)
            ).fetchone()
            return dict(row) if row else None

    def delete_post(self, post_id):
        with self._lock:
            cur = self.conn.execute("DELETE FROM posts WHERE id = ?", (int(post_id),))
            self.conn.commit()
            return cur.rowcount > 0

    def delete_posts_by_group(self, group_id):
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM posts WHERE group_id = ?", (str(group_id),)
            )
            self.conn.commit()
            return cur.rowcount

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

    # ---- history ----
    def add_history(self, job_type, profile, status, summary="",
                    progress_done=None, progress_total=None,
                    started_at=None, finished_at=None):
        """Persist a record of a finished, cancelled, failed, or interrupted job."""
        with self._lock:
            self.conn.execute(
                "INSERT INTO history "
                "(type, profile, status, summary, progress_done, progress_total, "
                " started_at, finished_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_type, profile, status, summary, progress_done, progress_total,
                 started_at or _now(), finished_at or _now()),
            )
            self.conn.commit()

    def get_history(self, limit=200):
        """Return most-recent job history, newest first."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [dict(r) for r in rows]