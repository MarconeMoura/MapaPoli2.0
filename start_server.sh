#!/usr/bin/env bash
set -euo pipefail

# Start FastAPI app directly for Render.
uvicorn avatar:app --host 0.0.0.0 --port "${PORT:-9000}"
