#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "Erro: instale FFmpeg e confirme que ffmpeg/ffprobe estão no PATH." >&2
  exit 1
fi
if [ ! -d .venv ]; then python3 -m venv .venv; fi
. .venv/bin/activate
python -m pip install -r requirements.txt
exec python app.py
