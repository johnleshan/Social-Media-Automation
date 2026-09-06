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