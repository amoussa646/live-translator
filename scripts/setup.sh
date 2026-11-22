#!/bin/zsh
# One-time setup on an Apple Silicon Mac: Python env + the two models (~7.5 GB total).
# After this, the app runs fully offline: ./run.sh
set -euo pipefail
cd "${0:A:h}/.."
command -v uv >/dev/null || { echo "Install uv first: brew install uv"; exit 1; }
command -v ffmpeg >/dev/null || echo "Note: ffmpeg is only needed for scripts/test_stream.py (brew install ffmpeg)"
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/hf download mlx-community/Voxtral-Mini-4B-Realtime-6bit --local-dir models/voxtral-realtime-6bit
.venv/bin/hf download mlx-community/translategemma-4b-it-4bit --local-dir models/translategemma-4b-it-4bit
echo "Done. Start with: ./run.sh --open"
