#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# 依赖：pip3 install pyinstaller
# Linux 下 Tk 依赖通常需要：sudo apt-get install -y python3-tk

python3 -m PyInstaller --noconsole --onefile --name lan-file-transfer ./app_v2.py

echo "Build done. See dist/lan-file-transfer"
