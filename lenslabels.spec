# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: builds the LensLabels GUI as a self-contained app.
#   macOS   -> dist/LensLabels.app       (windowed bundle)
#   Windows -> dist/LensLabels/          (folder with LensLabels.exe)
#   Linux   -> dist/LensLabels/          (folder with LensLabels binary)
# Build with:  pyinstaller lenslabels.spec

import os
import sys
from pathlib import Path

here = Path(SPECPATH)

if sys.platform == "darwin":
    icon = str(here / "assets" / "icon.icns")
elif sys.platform.startswith("win"):
    icon = str(here / "assets" / "icon.ico")
else:
    icon = None
if icon and not Path(icon).exists():
    icon = None

# CI injects CODESIGN_IDENTITY (a Developer ID Application identity) to sign
# every collected binary with the hardened runtime; unset -> unsigned/ad-hoc.
codesign_identity = os.environ.get("CODESIGN_IDENTITY") or None
entitlements = str(here / "assets" / "entitlements.plist") if codesign_identity else None

a = Analysis(
    ["lensmap2lbx_gui.py"],
    pathex=[str(here)],
    binaries=[],
    datas=[("fonts", "fonts")],          # bundled DejaVu -> identical rendering
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LensLabels",
    debug=False,
    strip=False,
    upx=False,
    console=False,                       # windowed app, no terminal
    icon=icon,
    codesign_identity=codesign_identity,
    entitlements_file=entitlements,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="LensLabels",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="LensLabels.app",
        icon=icon,
        bundle_identifier="org.lensmap2lbx.LensLabels",
        info_plist={
            "CFBundleShortVersionString": "1.3.0",
            "NSHighResolutionCapable": True,
        },
    )
