#!/usr/bin/env bash
# Stop the site (and its model server) started by start_site.sh / site.slurm.
set -euo pipefail
COPILOT_HOME=$(readlink -f "${COPILOT_HOME:-${COPILOT_DIR:-$HOME/rnaseq-copilot}}")
[ -f "$COPILOT_HOME/site.job" ] || { echo "No site recorded in $COPILOT_HOME/site.job."; exit 0; }
read -r ID _ < "$COPILOT_HOME/site.job" || true
case "${ID:-}" in
  pid:*)
    P=${ID#pid:}
    if kill -0 "$P" 2>/dev/null; then
      kill -TERM -- "-$P" 2>/dev/null || kill -TERM "$P"          # the whole process group (setsid)
      for _ in $(seq 1 20); do kill -0 "$P" 2>/dev/null || break; sleep 1; done
      echo "Stopped the site (process $P)."
    else
      echo "The site (process $P) is not running."
    fi
    for A in apptainer singularity; do command -v $A >/dev/null && $A instance stop "ollama_$P" >/dev/null 2>&1; done; true ;;
  "") echo "Nothing recorded." ;;
  *)  if squeue -h -j "$ID" 2>/dev/null | grep -q .; then scancel "$ID" && echo "Stopped site job $ID."; else echo "Site job $ID is not running."; fi ;;
esac
