#!/usr/bin/env bash
# Start the site on http://localhost:8000 (or $PORT).
# Builds the frontend when its code changed and keeps the Python environment up to date.
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8000}"

# Settings and the Anthropic API key for the chat, from .env (not in git), e.g.
#   ANTHROPIC_API_KEY=sk-ant-...
if [ -f .env ]; then set -a; . ./.env; set +a; fi

# Python 3.11+ (macOS ships an older python3; prefer Homebrew's)
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null && "$cand" -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.11 or newer is needed. On a Mac: brew install python@3.12" >&2
  exit 1
fi

if [ ! -f codebank/manifest/tools.yaml ]; then
  echo "The codebank folder is empty. Run: git submodule update --init" >&2
  exit 1
fi

# Frontend: build on first start and whenever its source is newer than the build
if [ ! -f frontend/dist/index.html ] || [ -n "$(find frontend/src frontend/index.html frontend/package.json -newer frontend/dist/index.html 2>/dev/null | head -1)" ]; then
  echo "Building the website..."
  (cd frontend && npm install --no-audit --no-fund && npm run build)
fi

if [ ! -x backend/.venv/bin/python ] || ! backend/.venv/bin/python -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  echo "Creating the Python environment with $PY..."
  rm -rf backend/.venv
  "$PY" -m venv backend/.venv
fi
backend/.venv/bin/pip install -q --disable-pip-version-check -r backend/requirements.txt

# Chromium for PDF export of the report (once, about 150 MB)
if [ -z "${CHROMIUM_PATH:-}" ] && [ ! -f backend/.venv/.chromium-installed ]; then
  echo "Installing Chromium for PDF reports (first start only)..."
  if backend/.venv/bin/python -m playwright install chromium; then
    touch backend/.venv/.chromium-installed
  else
    echo "Could not install Chromium; PDF download will not work (HTML reports still do)." >&2
  fi
fi

if curl -s --max-time 2 "${OLLAMA_URL:-http://localhost:11434}/api/tags" >/dev/null 2>&1; then
  echo "Assistant: Ollama is running (model ${OLLAMA_MODEL:-qwen3:8b})."
else
  echo "Assistant: Ollama is not running; the site will use its built-in rules. (Start it with: brew services start ollama)"
fi
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  echo "Chat: Anthropic API key found (model ${CHAT_MODEL:-claude-opus-5-5})."
else
  echo "Chat: no ANTHROPIC_API_KEY in .env; the Ask (chat) tab will explain how to add one."
fi
echo "Open http://localhost:$PORT in your browser. Stop with Ctrl+C."
cd backend && exec .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
