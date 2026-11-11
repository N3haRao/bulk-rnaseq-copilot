#!/usr/bin/env bash
# Download open-weight models once into $COPILOT_HOME/models (needs internet: run it on a login node or any
# node that can reach ollama.com). Models are shared by the site and the release job.
#
#   containers/ada/pull_model.sh                     # the site's model and qwen3-coder:30b (release agent, ~19 GB);
#                                                    # site model: qwen3:30b (~19 GB) on a GPU with 20 GB+ (fuji), else qwen3:8b (~5 GB)
#   containers/ada/pull_model.sh qwen3:8b            # only one model
#   containers/ada/pull_model.sh qwen3:14b           # another model (then OLLAMA_MODEL=qwen3:14b for the site)
set -euo pipefail
COPILOT_HOME=$(readlink -f "${COPILOT_HOME:-${COPILOT_DIR:-$HOME/rnaseq-copilot}}")
MODELS=("$@")
if [ ${#MODELS[@]} -eq 0 ]; then
  GPU_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | sort -n | tail -1 | tr -dc 0-9)
  if [ "${GPU_MB:-0}" -ge 20000 ]; then MODELS=(qwen3:30b qwen3-coder:30b); else MODELS=(qwen3:8b qwen3-coder:30b); fi
  [ "${ALL_MODELS:-0}" = 1 ] && MODELS=(qwen3:30b qwen3:8b qwen3-coder:30b)
fi
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
OLL=$COPILOT_HOME/ollama.sif
[ -f "$OLL" ] || { echo "No $OLL. Copy the images there first (containers/build.sh)." >&2; exit 1; }
mkdir -p "$COPILOT_HOME/models"
echo "Free space in $COPILOT_HOME/models: $(df -h "$COPILOT_HOME/models" | awk 'NR==2 {print $4}') (qwen3:8b ~5 GB, qwen3:30b ~19 GB, qwen3-coder:30b ~19 GB)"
PORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
NAME=ollama_pull_$$
apptainer instance start --containall --bind "$COPILOT_HOME/models:/models" \
  --env OLLAMA_HOST=127.0.0.1:$PORT${HTTPS_PROXY:+,HTTPS_PROXY=$HTTPS_PROXY} "$OLL" "$NAME" >/dev/null
trap 'apptainer instance stop "$NAME" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 30); do curl -s "http://127.0.0.1:$PORT/api/tags" >/dev/null && break; sleep 1; done
for m in "${MODELS[@]}"; do
  echo "== $m"; [ -n "${GPU_MB:-}" ] && [ "${GPU_MB:-0}" -ge 20000 ] && [ "$m" = qwen3:30b ] && echo "   (GPU with $GPU_MB MiB: the site uses Qwen3 30B)"
  apptainer exec --env OLLAMA_HOST=127.0.0.1:$PORT "instance://$NAME" /bin/ollama pull "$m"
done
apptainer exec --env OLLAMA_HOST=127.0.0.1:$PORT "instance://$NAME" /bin/ollama list
echo "Models are in $COPILOT_HOME/models"
