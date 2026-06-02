#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

# Linux 下通常使用 python3
python3 ./app_v2.py
