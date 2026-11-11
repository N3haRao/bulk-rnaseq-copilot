# =============================================================================
# site-env.sh  --  sourced inside rnaseq-copilot.sif: which site code and code bank to use
# =============================================================================
# The image holds the software (R, tools, Python, Node, Chromium). The code can come from two places:
#
#   1. Folders on the server (the normal case on Ada): set COPILOT_HOME and bind it at the same path.
#        $COPILOT_HOME/copilot         the live site (a git repository; releases are merged into it)
#        $COPILOT_HOME/codebank-live   symlink to the live code bank release (ops/release approve sets it)
#      A code bank release then needs only a site restart, never a new image.
#   2. The copies built into the image (/opt/app), when COPILOT_HOME is not set: a self-contained image
#      for a quick test or a machine without the folder layout.
#
# Sets: SITE_DIR, CODEBANK_DIR, FRONTEND_DIST, COPILOT_PYTHON, CODE_SOURCE (a sentence for the log).
# Defines: ensure_frontend (builds frontend/dist when it is missing or older than frontend/src).
# =============================================================================

SITE_DIR=/opt/app
CODEBANK_DIR=/opt/app/codebank
CODE_SOURCE="the copies built into the image"
if [ -n "${COPILOT_HOME:-}" ]; then
  if [ -d "$COPILOT_HOME/copilot/backend/app" ]; then
    SITE_DIR=$COPILOT_HOME/copilot
    CODE_SOURCE="site from $SITE_DIR"
    if [ -f "$COPILOT_HOME/codebank-live/manifest/tools.yaml" ]; then
      # resolved once: a release approved while the site runs takes effect at the next restart, never mid-run
      CODEBANK_DIR=$(readlink -f "$COPILOT_HOME/codebank-live")
    elif [ -f "$SITE_DIR/codebank/manifest/tools.yaml" ]; then
      CODEBANK_DIR=$SITE_DIR/codebank
      echo "NOTE: no $COPILOT_HOME/codebank-live yet (no release approved); using the site's codebank/ folder." >&2
    else
      echo "NOTE: no code bank in $COPILOT_HOME/codebank-live or $SITE_DIR/codebank; using the image's copy." >&2
    fi
    CODE_SOURCE="$CODE_SOURCE, code bank from $CODEBANK_DIR"
  else
    echo "NOTE: COPILOT_HOME=$COPILOT_HOME has no copilot/ folder (or it is not bound into the container);" >&2
    echo "      using the copies built into the image." >&2
  fi
fi
export SITE_DIR CODEBANK_DIR CODE_SOURCE
export FRONTEND_DIST=$SITE_DIR/frontend/dist
export COPILOT_PYTHON=/opt/conda/envs/app/bin/python

if [ "$SITE_DIR" != /opt/app ] && ! cmp -s "$SITE_DIR/backend/requirements.txt" /opt/app/backend/requirements.txt; then
  echo "WARNING: $SITE_DIR/backend/requirements.txt differs from the one this image was built with." >&2
  echo "         Rebuild the image (containers/build.sh app) if the site needs new Python packages." >&2
fi

ensure_frontend() {
  [ "$SITE_DIR" = /opt/app ] && return 0
  local fe=$SITE_DIR/frontend
  if [ ! -e "$fe/node_modules" ]; then
    if cmp -s "$fe/package-lock.json" /opt/app/frontend/package-lock.json; then
      ln -s /opt/app/frontend/node_modules "$fe/node_modules"     # the image's packages (same lock file)
    else
      echo "frontend/package-lock.json differs from the image's; installing packages (needs internet) ..." >&2
      (cd "$fe" && npm ci --no-audit --no-fund >&2) || { echo "npm ci failed; rebuild the image." >&2; return 1; }
    fi
  fi
  if [ ! -f "$fe/dist/index.html" ] || [ -n "$(find "$fe/src" "$fe/index.html" "$fe/package.json" -newer "$fe/dist/index.html" -print -quit 2>/dev/null)" ]; then
    echo "Building the frontend in $fe ..." >&2
    (cd "$fe" && npm run build >/dev/null) || { echo "Frontend build FAILED (cd $fe && npm run build)." >&2; return 1; }
  fi
}
