#!/usr/bin/env bash
# =============================================================================
# upstream.slurm  --  FASTQ -> counts, splicing and isoforms as a batch job
# =============================================================================
#   CONFIG=/project/lab/myexp/upstream.yaml sbatch containers/ada/upstream.slurm
#
# Uses the live code bank release ($COPILOT_HOME/codebank-live) when there is one, else the image's copy.
# The config is codebank/later/scripts/run_upstream.R's (see codebank/later/README.md).
# Paths in it must be visible inside the container: they are bound at the same
# location (BIND, default: the config's folder plus /project and /scratch when they exist).
# STAR needs ~32 GB of memory for human or mouse; Salmon / rMATS use the CPUs.
# =============================================================================
#SBATCH --job-name=rnaseq-upstream
#SBATCH --time=24:00:00
#SBATCH --cpus-per-task=16
#SBATCH --mem=64G
#SBATCH --output=rnaseq-upstream-%j.log

set -euo pipefail
: "${CONFIG:?Set CONFIG=/path/to/upstream.yaml}"
COPILOT_DIR=$(readlink -f "${COPILOT_HOME:-${COPILOT_DIR:-$HOME/rnaseq-copilot}}")
module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
# Singularity 3.6+ works in place of Apptainer (e.g. fuji: singularity 3.7)
if ! command -v apptainer >/dev/null && command -v singularity >/dev/null; then apptainer() { singularity "$@"; }; fi
APP=$COPILOT_DIR/rnaseq-copilot.sif
[ -f "$APP" ] || { echo "Missing $APP" >&2; exit 1; }
CONFIG=$(readlink -f "$CONFIG")
BIND=${BIND:-$(dirname "$CONFIG")}
for d in /project /scratch /work; do [ -d "$d" ] && BIND="$BIND,$d"; done
BIND="$BIND,$COPILOT_DIR"     # the live code bank release ($COPILOT_HOME/codebank-live), when there is one
# a real /tmp for STAR, Salmon and rMATS (with --containall the container's own /tmp is a few MB in memory)
mkdir -p "$COPILOT_DIR/tmp" && BIND="$BIND,$COPILOT_DIR/tmp:/tmp"
echo "Config: $CONFIG   threads in the config should match --cpus-per-task (${SLURM_CPUS_PER_TASK:-?})"
apptainer run --containall --cleanenv --bind "$BIND" --env COPILOT_HOME=$COPILOT_DIR --pwd "$(dirname "$CONFIG")" --app upstream "$APP" --config "$CONFIG"
