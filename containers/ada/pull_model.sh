#!/usr/bin/env bash
# Download open-weight models once into $COPILOT_HOME/models (needs internet: run it on a login node or any
# node that can reach ollama.com). Models are shared by the site and the release job.
#
#   containers/ada/pull_model.sh                     # qwen3:8b (the site's assistants, ~5 GB) and qwen3-coder:30b (release agent, ~19 GB)
#   containers/ada/pull_model.sh qwen3:8b            # only one model
#   containers/ada/pull_model.sh qwen3:14b           # another model (then OLLAMA_MODEL=qwen3:14b for the site)
set -euo pipefail
COPILOT_HOME=$(readlink -f "${COPILOT_HOME:-${COPILOT_DIR:-$HOME/rnaseq-copilot}}")
MODELS=("$@")
[ ${#MODELS[@]} -gt 0 ] || MODELS=(qwen3:8b qwen3-coder:30b)
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
OLL=$COPILOT_HOME/ollama.sif
[ -f "$OLL" ] || { echo "No $OLL. Copy the images there first (containers/build.sh)." >&2; exit 1; }
mkdir -p "$COPILOT_HOME/models"
echo "Free space in $COPILOT_HOME/models: $(df -h "$COPILOT_HOME/models" | awk 'NR==2 {print $4}') (qwen3:8b ~5 GB, qwen3-coder:30b ~19 GB)"
PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
NAME=ollama_pull_$$
apptainer instance start --containall --bind "$COPILOT_HOME/models:/models" \
  --env OLLAMA_HOST=127.0.0.1:$PORT${HTTPS_PROXY:+,HTTPS_PROXY=$HTTPS_PROXY} "$OLL" "$NAME" >/dev/null
trap 'apptainer instance stop "$NAME" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do curl -s "http://127.0.0.1:$PORT/api/tags" >/dev/null && break; sleep 1; done
for m in "${MODELS[@]}"; do
  echo "== $m"
  apptainer exec --env OLLAMA_HOST=127.0.0.1:$PORT "instance://$NAME" /bin/ollama pull "$m"
done
apptainer exec --env OLLAMA_HOST=127.0.0.1:$PORT "instance://$NAME" /bin/ollama list
echo "Models are in $COPILOT_HOME/models"
