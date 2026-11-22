#!/bin/zsh
# Live Translator — starts the local engine and web UI. Ctrl+C to stop.
#   ./run.sh          start
#   ./run.sh --open   start and open the UI in the browser
#   LT_LAN=0 ./run.sh only this Mac (no access from other Wi-Fi devices)
cd "${0:A:h}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 HF_HUB_DISABLE_TELEMETRY=1 DO_NOT_TRACK=1
mkdir -p logs
.venv/bin/python app/server.py "$@" 2>&1 | tee logs/server.log
