"""Configuration management for the bot.

Stores everything in a single JSON file (config.json) so users can manage
accounts and settings without touching code.

Path layout differs between source runs and installed (PyInstaller) runs:

* source:  BASE_DIR = project root; data/profiles/content next to the code.
* frozen:  BASE_DIR = read-only bundle root (web assets); all user data
           (config.json, bot.db, Chrome profiles, media) lives under
           %LOCALAPPDATA%\\GroupPostAutomator so the program folder stays writable-free.
"""
import json
import os
import shutil
import sys
from copy import deepcopy

_FROZEN = getattr(sys, "frozen", False)


def is_frozen():
    """True when running from a packaged (PyInstaller) build."""
    return _FROZEN

# Read-only bundle root. Source: the project root. Frozen: PyInstaller's
# extraction dir (_MEIPASS) which also holds the bundled web/ assets.
BASE_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
if not _FROZEN:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Writable base for everything the user creates: config, db, profiles, content.
USER_DATA_ROOT = BASE_DIR
if _FROZEN:
    USER_DATA_ROOT = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
        "GroupPostAutomator",
    )

DATA_DIR = os.path.join(USER_DATA_ROOT, "data")
PROFILES_DIR = os.path.join(USER_DATA_ROOT, "profiles")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")


def _load_seed_root():
    """Path of the project workspace that produced this build, or None.

    In installed (frozen) builds the bundle carries a small file
    'seed_source.txt' (written at build time by packaging/build.ps1). On the
    machine that built it, the folder still exists, so a first launch can adopt
    the existing accounts/groups/profiles and the installed app starts fully
    set up — exactly like running python main.py. On any other machine the path
    won't exist, so nothing is imported and the app starts fresh."""
    if not _FROZEN:
        return None
    marker = os.path.join(BASE_DIR, "seed_source.txt")
    if not os.path.isfile(marker):
        return None
    try:
        with open(marker, "r", encoding="utf-8-sig") as fh:
            root = fh.read().strip()
    except OSError:
        return None
    if root and os.path.isdir(os.path.join(root, "data")):
        return root
    return None


def migrate_source_setup():
    """One-time import of an existing project setup into the installed app.

    Only runs in installed builds and only when the installed app has no
    config.json yet (i.e. this is the first launch). Copies:
      * data/config.json + data/bot.db   (accounts, groups, settings, history)
      * profiles/<name>                  (logged-in Chrome sessions)
    then rewrites each profile's user_data_dir to the local install location."""
    if not _FROZEN:
        return
    if os.path.exists(CONFIG_PATH):
        return  # already initialised (user may have added an account)
    root = _load_seed_root()
    if not root:
        return
    src_data = os.path.join(root, "data")
    src_profiles = os.path.join(root, "profiles")
    try:
        ensure_dirs()
        # Bot DB + config first (cheap), so groups/accounts appear immediately.
        for fn in ("bot.db", "config.json"):
            src = os.path.join(src_data, fn)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(DATA_DIR, fn))
        # Chrome profiles: copy each logged-in session so the install is
        # self-contained; skip live/locked dirs and keep going.
        if os.path.isdir(src_profiles):
            for name in os.listdir(src_profiles):
                src = os.path.join(src_profiles, name)
                dst = os.path.join(PROFILES_DIR, name)
                if os.path.isdir(src) and not os.path.exists(dst):
                    try:
                        shutil.copytree(src, dst, ignore_dangling_symlinks=True,
                                        dirs_exist_ok=True)
                    except OSError:
                        continue
        # Point profiles at the local copy.
        cfg_path = os.path.join(DATA_DIR, "config.json")
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                for prof in data.get("profiles", []):
                    dst = os.path.join(PROFILES_DIR, prof.get("name", ""))
                    if os.path.isdir(dst):
                        prof["user_data_dir"] = dst
                with open(cfg_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, ensure_ascii=False)
            except (OSError, json.JSONDecodeError):
                pass
        print(f"[migrate] imported existing setup from {root}", flush=True)
    except OSError:
        pass


def resolve_media_dir(media_folder):
    """Resolve the configured media folder against the writable user-data root
    so a relative default ('content') survives the move to %LOCALAPPDATA% in
    installed builds, while absolute paths are honored as-is."""
    return os.path.abspath(
        os.path.join(USER_DATA_ROOT, media_folder or "content")
    )

DEFAULTS = {
    "profiles": [],
    "settings": {
        "media_folder": "content",
        "caption": "",
        "delay_min": 180,
        "delay_max": 480,
        "soft_cap": 150,
        "headless": True,
        "scan_on_start": True,
        "min_members": 0,
        "max_cycle_posts": 0,
        "developer_mode": False,
        "postable_only": False,
        "niche_only": False,
        "niche_max_members": 150000,
        "max_group_idle_days": 21,
    },
    "last_profile": "",
    "last_keyword": "",
    "last_filter_mode": "search",
}


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(PROFILES_DIR, exist_ok=True)


class Config:
    def __init__(self, path=CONFIG_PATH):
        self.path = path
        migrate_source_setup()
        ensure_dirs()
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            except (json.JSONDecodeError, OSError):
                data = {}
        else:
            data = {}
        merged = deepcopy(DEFAULTS)
        for section, values in data.items():
            if isinstance(values, dict) and isinstance(merged.get(section), dict):
                merged[section].update(values)
            else:
                merged[section] = values
        return merged

    def save(self):
        ensure_dirs()
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)

    # ---- profiles ----
    @property
    def profiles(self):
        return self.data["profiles"]

    def get_profile(self, name):
        for p in self.profiles:
            if p["name"] == name:
                return p
        return None

    def add_profile(self, name, user_data_dir=None):
        if self.get_profile(name):
            return False
        user_data_dir = user_data_dir or os.path.join(PROFILES_DIR, name)
        self.data["profiles"].append(
            {"name": name, "user_data_dir": os.path.abspath(user_data_dir)}
        )
        self.save()
        return True

    def remove_profile(self, name):
        self.data["profiles"] = [p for p in self.profiles if p["name"] != name]
        if self.data["last_profile"] == name:
            self.data["last_profile"] = ""
        self.save()

    # ---- settings ----
    @property
    def settings(self):
        return self.data["settings"]

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def update_settings(self, **kwargs):
        self.data["settings"].update(kwargs)
        self.save()