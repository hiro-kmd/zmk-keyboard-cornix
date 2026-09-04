#!/usr/bin/env sh
here="$(cd "$(dirname "$0")" && pwd)"
if [ ! -x "$here/.venv/bin/python" ]; then
  echo "先に setup.sh を実行してください。" >&2
  exit 1
fi
exec "$here/.venv/bin/python" "$here/zmkbak.py" "$@"
