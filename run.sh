#!/usr/bin/env bash
# Start the Code Intelligence backend.
#   ./run.sh              → http://localhost:8765
#   PORT=9000 ./run.sh    → custom port
#   ANTHROPIC_API_KEY=... ./run.sh   → enables the Claude AI mapping stage
set -e
cd "$(dirname "$0")"
PORT="${PORT:-8765}"
# Boot/Revive is required by the Pixel-Clone flow. Enabled by default; it only
# ever runs container-isolated docker-compose on apps you explicitly revive.
export CODEINTEL_ALLOW_BOOT="${CODEINTEL_ALLOW_BOOT:-1}"
VENV="../.cienv/bin/python"
[ -x "$VENV" ] || VENV="python3"
echo "Code Intelligence → http://localhost:${PORT}"
exec "$VENV" manage.py runserver "0.0.0.0:${PORT}" --noreload
