#!/usr/bin/env bash
# Restart the site so it picks up a newly approved release (used as COPILOT_RESTART_CMD).
# Works with SLURM (Ada: cancels and resubmits the job) and without (fuji: restarts the background process).
# The port changes: the new log prints the new tunnel command.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export COPILOT_HOME=$(readlink -f "${COPILOT_HOME:-${COPILOT_DIR:-$HOME/rnaseq-copilot}}")
"$HERE/stop_site.sh"
# a SLURM job takes a moment to end
if [ -f "$COPILOT_HOME/site.job" ]; then
  read -r ID _ < "$COPILOT_HOME/site.job" || true
  if [ -n "${ID:-}" ] && [[ "$ID" != pid:* ]]; then
    for _ in $(seq 1 30); do squeue -h -j "$ID" 2>/dev/null | grep -q . || break; sleep 2; done
  fi
fi
"$HERE/start_site.sh"
