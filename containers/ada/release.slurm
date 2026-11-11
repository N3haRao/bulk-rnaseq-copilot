#!/usr/bin/env bash
# =============================================================================
# release.slurm  --  bring a code bank release into the site, with the release agent, as a SLURM job
# =============================================================================
# Normally submitted by the code bank's scripts/release.sh (TAG and COPILOT_CODEBANK_REPO are set):
#   cd $COPILOT_HOME/codebank && scripts/release.sh 0.6.1
# By hand:
#   TAG=v0.6.1 sbatch containers/ada/release.slurm
#   TAG=v0.6.1 sbatch --gres=gpu:1 containers/ada/release.slurm     # on a SLURM cluster with GPUs
#   TAG=v0.6.1 bash containers/ada/release.slurm                    # without SLURM (fuji), in the foreground
#
# If the site is running on this machine, its model server is reused (one Ollama on the GPU, which swaps
# the idle site model out for the coding model), so the two never compete for GPU memory.
#
# Starts the coding model (RELEASE_AGENT_MODEL, default qwen3-coder:30b; pull it once with
# containers/ada/pull_model.sh qwen3-coder:30b) and runs  ops/release integrate $TAG  inside the image.
# The agent only edits a staging copy; nothing goes live until the releasing bioinformatician runs
#   $COPILOT_HOME/copilot/ops/release approve $TAG
# after reading the review this job names at the end of its log.
#
# Without a GPU the model runs on the CPU: qwen3-coder:30b is a mixture-of-experts model (about 3B active
# parameters per token), so it works on CPUs, but a release that needs changes can take hours. Releases the
# site already handles need no model at all and take minutes (only the tests run).
# =============================================================================
#SBATCH --job-name=rnaseq-release
#SBATCH --time=12:00:00
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --output=rnaseq-release-%j.log

set -euo pipefail
USER=${USER:-$(id -un)}
: "${TAG:?Set TAG=vX.Y.Z (the code bank release to integrate)}"
if [ -z "${COPILOT_HOME:-}" ] && [ -n "${COPILOT_CODEBANK_REPO:-}" ]; then COPILOT_HOME=$(dirname "$(readlink -f "$COPILOT_CODEBANK_REPO")"); fi
COPILOT_HOME=$(readlink -f "${COPILOT_HOME:-$HOME/rnaseq-copilot}")
export COPILOT_HOME
export RELEASE_AGENT_MODEL=${RELEASE_AGENT_MODEL:-qwen3-coder:30b}
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
OLL=$COPILOT_HOME/ollama.sif
SITE=$COPILOT_HOME/copilot
[ -x "$SITE/ops/release" ] || { echo "No site at $SITE (containers/ada/setup_home.sh)" >&2; exit 1; }
[ -f "$COPILOT_HOME/rnaseq-copilot.sif" ] || { echo "Missing $COPILOT_HOME/rnaseq-copilot.sif" >&2; exit 1; }

NV=""; command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 && NV="--nv"
SITE_OPORT=""
if [ -f "$COPILOT_HOME/site.job" ]; then
  read -r _ SNODE _ SITE_OPORT < "$COPILOT_HOME/site.job" || true
  [ "${SNODE:-}" = "$(hostname -f 2>/dev/null || hostname)" ] || SITE_OPORT=""
fi
if [ -n "$SITE_OPORT" ] && [ "$SITE_OPORT" != none ] && curl -s "http://127.0.0.1:$SITE_OPORT/api/tags" >/dev/null; then
  OPORT=$SITE_OPORT
  echo "Using the running site's model server (port $OPORT)."
elif [ -f "$OLL" ]; then
  OPORT=$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')
  NAME=ollama_release_${SLURM_JOB_ID:-$$}
  apptainer instance start $NV --containall --bind "$COPILOT_HOME/models:/models" \
    --env OLLAMA_HOST=127.0.0.1:$OPORT,OLLAMA_NUM_PARALLEL=1,OLLAMA_KEEP_ALIVE=2h,OLLAMA_FLASH_ATTENTION=1,OLLAMA_KV_CACHE_TYPE=q8_0 \
    "$OLL" "$NAME" >/dev/null
  trap 'apptainer instance stop "$NAME" >/dev/null 2>&1 || true' EXIT
  for _ in $(seq 1 60); do curl -s "http://127.0.0.1:$OPORT/api/tags" >/dev/null && break; sleep 1; done
else
  OPORT=""
  echo "WARNING: no $OLL; only releases that need no site change can be integrated."
fi
if [ -n "$OPORT" ]; then
  curl -s "http://127.0.0.1:$OPORT/api/tags" | grep -q "\"$RELEASE_AGENT_MODEL\"" || \
    echo "WARNING: $RELEASE_AGENT_MODEL is not in $COPILOT_HOME/models (containers/ada/pull_model.sh $RELEASE_AGENT_MODEL). If the site needs changes, the review will say the model stopped answering."
  export OLLAMA_URL=http://127.0.0.1:$OPORT
  # on CPUs one answer can take many minutes
  [ -n "$NV" ] || export RELEASE_AGENT_TIMEOUT=${RELEASE_AGENT_TIMEOUT:-3600}
  if [ -n "$NV" ]; then WHERE="GPU ($(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | head -1))"; else WHERE="CPU"; fi
  echo "Model: $RELEASE_AGENT_MODEL on the $WHERE"
fi

echo "Integrating code bank $TAG into $SITE ..."
set +e
"$SITE/ops/release" integrate "$TAG" ${RELEASE_AGENT_MODE:+--agent $RELEASE_AGENT_MODE}
rc=$?
set -e
echo
case $rc in
  0) echo "READY. The releasing bioinformatician reads $COPILOT_HOME/staging/$TAG/RELEASE_REVIEW.md, then runs:"
     echo "     $SITE/ops/release approve $TAG" ;;
  2) echo "NEEDS ATTENTION: a check failed. Read $COPILOT_HOME/staging/$TAG/RELEASE_REVIEW.md (the live site is unchanged)." ;;
  *) echo "The release command failed (exit $rc); see above. The live site is unchanged." ;;
esac
exit $rc
