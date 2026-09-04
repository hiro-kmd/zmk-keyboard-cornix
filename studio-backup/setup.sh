#!/usr/bin/env sh
# 初回セットアップ: このフォルダに .venv を作り、依存パッケージを入れる (macOS / Linux)
set -e
cd "$(dirname "$0")"
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip >/dev/null
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python zmkproto_decode.py
echo
echo "セットアップ完了。 ./zmkbak.sh doctor で接続確認できます。"
