#!/usr/bin/env bash
# Download the open-weight model once into $COPILOT_DIR/models (needs internet,
# so run it on a login node or any node that can reach ollama.com).
#
#   COPILOT_DIR=~/rnaseq-copilot containers/ada/pull_model.sh            # qwen3:8b
#   COPILOT_DIR=~/rnaseq-copilot containers/ada/pull_model.sh qwen3:14b  # another model
set -euo pipefail
COPILOT_DIR=${COPILOT_DIR:-$HOME/rnaseq-copilot}
MODEL=${1:-qwen3:8b}
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
OLL=$COPILOT_DIR/ollama.sif
[ -f "$OLL" ] || { echo "No $OLL. Copy the images there first (containers/build.sh)." >&2; exit 1; }
mkdir -p "$COPILOT_DIR/models"
PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
NAME=ollama_pull_$$
apptainer instance start --containall --bind "$COPILOT_DIR/models:/models" \
  --env OLLAMA_HOST=127.0.0.1:$PORT "$OLL" "$NAME" >/dev/null
trap 'apptainer instance stop "$NAME" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do curl -s "http://127.0.0.1:$PORT/api/tags" >/dev/null && break; sleep 1; done
apptainer exec --env OLLAMA_HOST=127.0.0.1:$PORT "instance://$NAME" /bin/ollama pull "$MODEL"
apptainer exec --env OLLAMA_HOST=127.0.0.1:$PORT "instance://$NAME" /bin/ollama list
echo "Model $MODEL is in $COPILOT_DIR/models"
