# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for Group Post Automator (onedir build).

Build from the project root:
    venv\\Scripts\\pyinstaller packaging\\app.spec --noconfirm

Produces: dist\\GroupPostAutomator\\GroupPostAutomator.exe
"""
import os
import sys

# SPECPATH = directory containing this spec file (packaging/). Project root is
# one level up. Paths passed to Analysis must be absolute.
_ROOT = os.path.normpath(os.path.join(SPECPATH, ".."))

datas = [(os.path.join(_ROOT, "web"), "web")]

# Optional one-time setup import (only for the machine that builds the app).
# packaging/build.ps1 -Seed <workspace> writes packaging/seed_source.txt with the
# absolute path of a source workspace whose data/ and profiles/ should be adopted
# on first launch of the installed app. When the file is absent the install
# starts fresh (accounts added via the normal one-time login). See
# config.migrate_source_setup().
_seed_marker = os.path.join(SPECPATH, "seed_source.txt")
if os.path.isfile(_seed_marker):
    datas.append((_seed_marker, "."))

block_cipher = None

a = Analysis(
    [os.path.join(_ROOT, "main.py")],
    pathex=[_ROOT],
    binaries=[],
    # web/ assets go in alongside the app package so WEB_DIR = BASE_DIR/web works
    # whether frozen (_MEIPASS/web) or running from source (project_root/web).
    datas=datas,
    hookspath=[os.path.join(_ROOT, "packaging")],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "ttkbootstrap"],   # not used by the web UI
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="GroupPostAutomator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    icon=os.path.join(_ROOT, "packaging", "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GroupPostAutomator",
)
