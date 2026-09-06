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

block_cipher = None

a = Analysis(
    [os.path.join(_ROOT, "main.py")],
    pathex=[_ROOT],
    binaries=[],
    # web/ assets go in alongside the app package so WEB_DIR = BASE_DIR/web works
    # whether frozen (_MEIPASS/web) or running from source (project_root/web).
    datas=[(os.path.join(_ROOT, "web"), "web")],
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
