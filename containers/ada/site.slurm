#!/usr/bin/env bash
# =============================================================================
# site.slurm  --  run the RNA-seq analysis site (and, optionally, its model) as a SLURM job
# =============================================================================
#   sbatch containers/ada/site.slurm                       # then read the log for the tunnel command
#   sbatch --gres=gpu:1 containers/ada/site.slurm          # on a server with GPUs (the model uses it)
#   OLLAMA_MODEL=none sbatch containers/ada/site.slurm     # no open-weight model (Claude or built-in rules)
# The site's model: qwen3:30b on a GPU with 20 GB or more (fuji), qwen3:8b otherwise; OLLAMA_MODEL=... overrides.
#   containers/ada/start_site.sh                           # the same, with or without SLURM (e.g. fuji: one
#                                                          # GPU server, no scheduler: runs it in the background)
#
# $COPILOT_HOME (default ~/rnaseq-copilot; set up with containers/ada/setup_home.sh) holds:
#   rnaseq-copilot.sif ollama.sif   the images (software only)
#   copilot/                        the live site (git); code bank releases are merged in by ops/release
#   codebank/                       the bioinformaticians' code bank (git, tagged releases)
#   codebank-live -> codebank-releases/vX.Y.Z   the approved release the site uses
#   models/  data/  secrets.env     models, uploads/runs/API key (private), optional ANTHROPIC_API_KEY=...
# The site code and the code bank are read from these folders, not from the image, so after
# `ops/release approve` a restart (scancel + sbatch, or containers/ada/restart_site.sh) is all it takes.
#
# No #SBATCH partition or GPU line: the default partition is used (Ada: defq, one node, no GPU) and the
# model runs on the CPU, slowly. Add --partition / --gres on the sbatch command line on another server.
# =============================================================================
#SBATCH --job-name=rnaseq-copilot
#SBATCH --time=08:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --output=rnaseq-copilot-%j.log

set -euo pipefail
USER=${USER:-$(id -un)}
COPILOT_HOME=$(readlink -f "${COPILOT_HOME:-${COPILOT_DIR:-$HOME/rnaseq-copilot}}")
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
# Singularity 3.6+ works in place of Apptainer (e.g. fuji: singularity 3.7)
if ! command -v apptainer >/dev/null && command -v singularity >/dev/null; then apptainer() { singularity "$@"; }; fi
APP=$COPILOT_HOME/rnaseq-copilot.sif
OLL=$COPILOT_HOME/ollama.sif
[ -f "$APP" ] || { echo "Missing $APP (containers/build.sh, then copy it there)" >&2; exit 1; }
[ -d "$COPILOT_HOME/copilot/backend" ] || echo "NOTE: no $COPILOT_HOME/copilot: the image's built-in copy of the site is used (containers/ada/setup_home.sh)."
mkdir -p "$COPILOT_HOME/data" "$COPILOT_HOME/models"
chmod 700 "$COPILOT_HOME/data"

free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
PORT=$(free_port)
NV=""; command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 && NV="--nv"
# the site's model: Qwen3 30B on a GPU with 20 GB or more (fuji's A30: 24 GB), else qwen3:8b (CPU, small GPUs)
GPU_MB=0
[ -n "$NV" ] && GPU_MB=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | sort -n | tail -1 | tr -dc 0-9)
if [ "${GPU_MB:-0}" -ge 20000 ]; then MODEL=${OLLAMA_MODEL:-qwen3:30b}; else MODEL=${OLLAMA_MODEL:-qwen3:8b}; fi

# ---- the model server (optional) ----
OLLAMA_ENV=""
if [ "$MODEL" != none ] && [ -f "$OLL" ]; then
  OPORT=$(free_port)
  NAME=ollama_${SLURM_JOB_ID:-$$}
  # flash attention + 8-bit KV cache: qwen3:30b (site) or qwen3-coder:30b (release agent, 32k context) each fit
  # a 24 GB GPU (A30) fully, one at a time; Ollama unloads the idle one to make room for the other
  apptainer instance start $NV --containall --bind "$COPILOT_HOME/models:/models" \
    --env OLLAMA_HOST=127.0.0.1:$OPORT,OLLAMA_NUM_PARALLEL=1,OLLAMA_FLASH_ATTENTION=1,OLLAMA_KV_CACHE_TYPE=q8_0,OLLAMA_MAX_LOADED_MODELS=1 \
    "$OLL" "$NAME" >/dev/null
  trap 'apptainer instance stop "$NAME" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 60); do curl -s "http://127.0.0.1:$OPORT/api/tags" >/dev/null && break; sleep 1; done
  if ! curl -s "http://127.0.0.1:$OPORT/api/tags" | grep -q "\"$MODEL\""; then
    echo "WARNING: model $MODEL is not in $COPILOT_HOME/models. Run containers/ada/pull_model.sh on a login node;"
    echo "         until then the site uses Claude (if a key is saved) or its built-in rules."
  fi
  if [ -n "$NV" ]; then WHERE="GPU ($GPU_MB MiB)"; else WHERE="CPU (no GPU found: answers are slow)"; fi
  echo "Model server: $MODEL on the $WHERE, port $OPORT"
  OLLAMA_ENV=",OLLAMA_URL=http://127.0.0.1:$OPORT,OLLAMA_MODEL=$MODEL,OLLAMA_TIMEOUT=${OLLAMA_TIMEOUT:-300}"
else
  if [ "$MODEL" = none ]; then WHY="OLLAMA_MODEL=none"; else WHY="no $OLL"; fi
  echo "Model server: not started ($WHY); the site uses Claude (if a key is saved) or its built-in rules."
  OLLAMA_ENV=",OLLAMA_URL=http://127.0.0.1:9"
fi

# ---- how to reach the site ----
NODE=$(hostname -f 2>/dev/null || hostname)
# who runs the site and where its model server listens (restart_site.sh and release.slurm read this)
echo "${SLURM_JOB_ID:-pid:$$} $NODE $PORT ${OPORT:-none}" > "$COPILOT_HOME/site.job"
if [ -n "${SLURM_JOB_ID:-}" ]; then
  TUNNEL="ssh -N -L 8000:127.0.0.1:$PORT -J $USER@${LOGIN_HOST:-${ADA_LOGIN:-ada.nri.bcm.edu}} $USER@$NODE"
  STOP="scancel $SLURM_JOB_ID"
  NOTE="If $NODE is the login node itself (a one-node cluster), the shorter
    ssh -N -L 8000:127.0.0.1:$PORT $USER@${LOGIN_HOST:-${ADA_LOGIN:-ada.nri.bcm.edu}}   works."
else
  TUNNEL="ssh -N -L 8000:127.0.0.1:$PORT $USER@$NODE"
  STOP="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)/stop_site.sh"
  NOTE="(No SLURM: the site runs as a background process on $NODE.)"
fi
cat <<MSG
================================================================================
The site starts on $NODE, port $PORT (only reachable from that machine).
On your own computer, open a tunnel and keep it open:

    $TUNNEL

then open http://localhost:8000 .
$NOTE
Stop the site with:  $STOP
================================================================================
MSG

# ---- the site: code from $COPILOT_HOME (bound at the same path), data in $COPILOT_HOME/data ----
ENVFILE=()
[ -f "$COPILOT_HOME/secrets.env" ] && ENVFILE=(--env-file "$COPILOT_HOME/secrets.env")
apptainer run --containall --cleanenv --bind "$COPILOT_HOME" ${ENVFILE[@]+"${ENVFILE[@]}"} \
  --env "COPILOT_HOME=$COPILOT_HOME,OMICS_DATA_DIR=$COPILOT_HOME/data,PORT=$PORT,HOST=127.0.0.1$OLLAMA_ENV" \
  ${HTTPS_PROXY:+--env HTTPS_PROXY=$HTTPS_PROXY} \
  "$APP"
