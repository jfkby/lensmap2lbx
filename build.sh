#!/bin/sh
# Local build: produces dist/LensLabels.app (macOS) or dist/LensLabels/ (win/linux)
set -e
python3 -m pip install --quiet pillow pyinstaller
pyinstaller lenslabels.spec
echo "done -> dist/"
