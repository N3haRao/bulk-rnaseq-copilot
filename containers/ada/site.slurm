#!/usr/bin/env bash
# =============================================================================
# site.slurm  --  run the RNA-seq analysis site (and its model) as a SLURM job
# =============================================================================
#   sbatch containers/ada/site.slurm                    # then read the log for the tunnel command
#   COPILOT_DIR=/project/lab/rnaseq-copilot sbatch containers/ada/site.slurm
#
# $COPILOT_DIR holds rnaseq-copilot.sif, ollama.sif, models/ (pull_model.sh),
# data/ (uploads, runs, the saved API key: keep it private) and, optionally,
# secrets.env with ANTHROPIC_API_KEY=... (chmod 600) instead of the Settings page.
# Adjust the #SBATCH lines to Ada's partitions; the GPU is for the model only.
# =============================================================================
#SBATCH --job-name=rnaseq-copilot
#SBATCH --time=08:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --gres=gpu:1
#SBATCH --output=rnaseq-copilot-%j.log

set -euo pipefail
COPILOT_DIR=${COPILOT_DIR:-$HOME/rnaseq-copilot}
MODEL=${OLLAMA_MODEL:-qwen3:8b}
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
APP=$COPILOT_DIR/rnaseq-copilot.sif
OLL=$COPILOT_DIR/ollama.sif
for f in "$APP" "$OLL"; do [ -f "$f" ] || { echo "Missing $f (containers/build.sh, then copy here)" >&2; exit 1; }; done
mkdir -p "$COPILOT_DIR/data" "$COPILOT_DIR/models"
chmod 700 "$COPILOT_DIR/data"

free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
PORT=$(free_port)
OPORT=$(free_port)
NV=""; command -v nvidia-smi >/dev/null && nvidia-smi >/dev/null 2>&1 && NV="--nv"
NAME=ollama_${SLURM_JOB_ID:-$$}

# ---- the model server ----
apptainer instance start $NV --containall --bind "$COPILOT_DIR/models:/models" \
  --env OLLAMA_HOST=127.0.0.1:$OPORT "$OLL" "$NAME" >/dev/null
trap 'apptainer instance stop "$NAME" >/dev/null 2>&1 || true' EXIT
for _ in $(seq 1 60); do curl -s "http://127.0.0.1:$OPORT/api/tags" >/dev/null && break; sleep 1; done
if ! curl -s "http://127.0.0.1:$OPORT/api/tags" | grep -q "\"${MODEL%%:*}"; then
  echo "WARNING: model $MODEL is not in $COPILOT_DIR/models. Run containers/ada/pull_model.sh on a login node;"
  echo "         until then the site uses its built-in rules instead of the assistant."
fi
echo "Model server: ${NV:+GPU, }port $OPORT"

# ---- how to reach the site ----
NODE=$(hostname)
cat <<MSG
================================================================================
The site starts on $NODE, port $PORT (only reachable from that node).
On your own computer, open a tunnel and keep it open:

    ssh -N -L 8000:127.0.0.1:$PORT -J $USER@<ada-login-host> $USER@$NODE

then open http://localhost:8000 . (If jumping to compute nodes is not allowed, ask
the Ada admins for the recommended way to forward a port to a running job.)
Stop the site with:  scancel ${SLURM_JOB_ID:-<job id>}
================================================================================
MSG

# ---- the site ----
ENVFILE=()
[ -f "$COPILOT_DIR/secrets.env" ] && ENVFILE=(--env-file "$COPILOT_DIR/secrets.env")
apptainer run --containall --cleanenv --bind "$COPILOT_DIR/data:/data" ${ENVFILE[@]+"${ENVFILE[@]}"} \
  --env PORT=$PORT,HOST=127.0.0.1,OLLAMA_URL=http://127.0.0.1:$OPORT,OLLAMA_MODEL=$MODEL \
  ${HTTPS_PROXY:+--env HTTPS_PROXY=$HTTPS_PROXY} \
  "$APP"
  
