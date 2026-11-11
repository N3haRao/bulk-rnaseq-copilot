#!/usr/bin/env bash
# =============================================================================
# setup_home.sh  --  set up $COPILOT_HOME on the server once (the folders the site and releases use)
# =============================================================================
#   COPILOT_HOME=/project/lab/rnaseq-copilot containers/ada/setup_home.sh
#
# Makes, in $COPILOT_HOME (default ~/rnaseq-copilot):
#   copilot/    the live site: a git clone of this repository (SITE_SRC, default: the repository this script is in)
#   codebank/   the bioinformaticians' code bank: a git clone (CODEBANK_SRC, default: this repository's codebank/
#               submodule's origin); bioinformaticians commit there and release with scripts/release.sh
#   data/ models/   (private data folder, chmod 700)
# and, if the code bank has a release tag, integrates and approves the newest one so codebank-live exists.
# The images (rnaseq-copilot.sif, ollama.sif) are copied in separately (containers/build.sh).
# Safe to run again: existing folders are left alone.
# =============================================================================
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
COPILOT_HOME=${COPILOT_HOME:-${COPILOT_DIR:-$HOME/rnaseq-copilot}}
mkdir -p "$COPILOT_HOME" && COPILOT_HOME=$(readlink -f "$COPILOT_HOME")
cd "$COPILOT_HOME"
mkdir -p data models && chmod 700 data

if [ ! -d copilot/.git ]; then
  SRC=${SITE_SRC:-$HERE}
  BR=${SITE_BRANCH:-$(git -C "$HERE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)}
  echo "== copilot/ <- $SRC ($BR)"
  git clone --quiet -b "$BR" "$SRC" copilot
  # the server's copy is its own repository from now on: releases are merged into it locally
  git -C copilot remote rename origin upstream 2>/dev/null || true
else
  echo "== copilot/ exists ($(git -C copilot log --oneline -1))"
fi
git -C copilot config user.name >/dev/null || git -C copilot config user.name "${USER:-copilot}"
git -C copilot config user.email >/dev/null || git -C copilot config user.email "${USER:-copilot}@localhost"

if [ ! -d codebank/.git ]; then
  SRC=${CODEBANK_SRC:-$(git -C "$HERE/codebank" remote get-url origin 2>/dev/null || echo "")}
  [ -n "$SRC" ] || { echo "Set CODEBANK_SRC to the code bank's git URL or folder." >&2; exit 1; }
  echo "== codebank/ <- $SRC"
  git clone --quiet "$SRC" codebank
else
  echo "== codebank/ exists ($(git -C codebank describe --tags --always))"
fi
chmod -R g+rX codebank copilot 2>/dev/null || true

TAG=$(git -C codebank describe --tags --abbrev=0 2>/dev/null || true)
if [ -L codebank-live ]; then
  echo "== live code bank release: $(basename "$(readlink codebank-live)")"
elif [ -n "$TAG" ]; then
  if ! git -C codebank tag -l --format='%(contents)' "$TAG" | grep -q "released-by:"; then
    echo "NOTE: $TAG has no 'released-by:' line (made before scripts/release.sh); whoever runs this approves it."
  fi
  echo "== first release: $TAG"
  if COPILOT_HOME="$COPILOT_HOME" copilot/ops/release integrate "$TAG" --agent never; then
    COPILOT_HOME="$COPILOT_HOME" copilot/ops/release approve "$TAG" --override "first setup" || true
  else
    echo "The site does not pass its checks with $TAG yet: read $COPILOT_HOME/staging/$TAG/RELEASE_REVIEW.md"
  fi
else
  echo "NOTE: the code bank has no release tag yet. A bioinformatician runs  scripts/release.sh <version>  in"
  echo "      $COPILOT_HOME/codebank; until then the site uses its own codebank/ folder."
fi
cat <<MSG

Done. Add to ~/.bashrc:
    export COPILOT_HOME=$COPILOT_HOME
    export COPILOT_RESTART_CMD="\$COPILOT_HOME/copilot/containers/ada/restart_site.sh"
Then: copy the images here, pull the models (copilot/containers/ada/pull_model.sh) and start the site
(copilot/containers/ada/start_site.sh: a SLURM job, or a background process without SLURM). Releases: copilot/docs/RELEASES.md
MSG
