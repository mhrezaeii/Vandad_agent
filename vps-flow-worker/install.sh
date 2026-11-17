#!/usr/bin/env bash
# 8Scale-free, Flow-on-VPS worker setup for a fresh Ubuntu 22.04/24.04 VPS.
# Run as root:  bash install.sh
set -e

echo "== [1/4] system packages =="
apt-get update -qq
apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
    chromium fonts-dejavu-core fonts-farsiweb curl ca-certificates

echo "== [2/4] python venv + deps =="
cd "$(dirname "$0")"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install playwright httpx

echo "== [3/4] playwright chromium =="
./.venv/bin/python -m playwright install chromium || \
    echo "playwright CDN blocked — using the system chromium (/usr/bin/chromium) instead"

echo "== [4/4] done =="
cat <<'NEXT'

Next steps:
  1. Export cookies: in YOUR browser (logged into labs.google/flow with the
     company Google account), use the Cookie-Editor extension -> Export
     (JSON to clipboard) -> save as cookies.json -> upload to this VPS.
  2. ./venv/bin/python flow_worker.py import-cookies cookies.json
  3. ./venv/bin/python flow_worker.py status
  4. Upload prompts.txt (scene prompts, one block per clip) and run:
     ./venv/bin/python flow_worker.py generate --prompt-file prompts.txt --out clips/
  5. Download the clips from ./clips/ back to your machine:
     E:\projects\vandad_agent\assets\clips\<project_id>\
     (names: 01.mp4, 02.mp4, ...)

Notes:
- Daily free quota: FLOW_DAILY_LIMIT env (default 6). Reset: delete state/quota.json
- Failures save full-page screenshots under state/shots/ for debugging.
NEXT
