#!/usr/bin/env bash
# Start the site: as a SLURM job where there is SLURM (Ada), else as a background process (fuji).
#   containers/ada/start_site.sh                 # OLLAMA_MODEL=..., COPILOT_HOME=... are passed on
# The log (rnaseq-copilot-<id>.log in $COPILOT_HOME) prints the ssh tunnel command.
set -euo pipefail
COPILOT_HOME=$(readlink -f "${COPILOT_HOME:-${COPILOT_DIR:-$HOME/rnaseq-copilot}}")
export COPILOT_HOME
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$COPILOT_HOME"
if command -v sbatch >/dev/null; then
  sbatch --export=ALL "$HERE/site.slurm"
  echo "Submitted; the log rnaseq-copilot-<job>.log in $COPILOT_HOME prints the tunnel command."
  exit 0
fi
if [ -f site.job ]; then
  read -r ID _ < site.job || true
  if [[ "${ID:-}" == pid:* ]] && kill -0 "${ID#pid:}" 2>/dev/null; then
    echo "The site is already running (process ${ID#pid:}). Stop it first: $HERE/stop_site.sh"; exit 1
  fi
fi
LOG="$COPILOT_HOME/rnaseq-copilot-$(date +%Y%m%d-%H%M%S).log"
# its own session (setsid), so stop_site.sh can stop the site and its model server together;
# nohup: it keeps running after you log out
setsid nohup bash "$HERE/site.slurm" > "$LOG" 2>&1 < /dev/null &
BG=$!
for _ in $(seq 1 90); do
  grep -q "ssh -N" "$LOG" 2>/dev/null && break
  kill -0 "$BG" 2>/dev/null || { echo "The site stopped while starting:"; tail -20 "$LOG"; exit 1; }
  sleep 1
done
sed -n '/^=====/,/^=====/p' "$LOG"
echo "Log: $LOG   (the site is up when it says 'Uvicorn running'; tail -f it to watch)"
