#!/usr/bin/env bash
# Build the Apptainer images of the RNA-seq analysis site.
#
#   containers/build.sh                 # both images (rnaseq-copilot.sif, ollama.sif) into containers/build/
#   containers/build.sh app             # only the site image
#   containers/build.sh ollama          # only the model-server image
#   BUILD_OPTS="--fakeroot" containers/build.sh   # e.g. on a cluster node where you are not root
#
# Needs Apptainer 1.2+ (multi-stage builds), git, about 25 GB of free disk space
# for the build, and internet access (Docker Hub, conda-forge, bioconda,
# Bioconductor data packages, r-universe, GEO). The site image is about 6-8 GB.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)
OUT=$REPO/containers/build
WHAT=${1:-all}
command -v apptainer >/dev/null || { echo "apptainer not found (module load apptainer?)" >&2; exit 1; }
mkdir -p "$OUT"

if [ "$WHAT" = all ] || [ "$WHAT" = app ]; then
  [ -f codebank/manifest/tools.yaml ] || { echo "The codebank submodule is empty: git submodule update --init" >&2; exit 1; }
  CTX=$(mktemp -d "$OUT/context.XXXX")
  trap 'rm -rf "$CTX"' EXIT
  echo "Preparing the build folder in $CTX ..."
  # the site's tracked files only (no data/, .venv, node_modules)
  git archive HEAD backend frontend containers | tar -x -C "$CTX"
  # the code bank as a real git clone at the pinned commit, so runs record their exact commit
  COMMIT=$(git -C codebank rev-parse HEAD)
  git -c advice.detachedHead=false clone --quiet --no-local codebank "$CTX/codebank"
  git -C "$CTX/codebank" -c advice.detachedHead=false checkout --quiet "$COMMIT"
  git -C "$CTX/codebank" remote set-url origin "$(git -C codebank remote get-url origin)"
  echo "$(git describe --always --dirty) ($(git rev-parse --abbrev-ref HEAD))" > "$CTX/VERSION"
  if ! git diff --quiet HEAD -- backend frontend containers; then
    echo "NOTE: you have uncommitted changes in backend/frontend/containers; the image uses the last commit." >&2
  fi
  echo "Building rnaseq-copilot.sif (code bank $COMMIT) ..."
  (cd "$CTX" && apptainer build ${BUILD_OPTS:-} --force "$OUT/rnaseq-copilot.sif" containers/rnaseq-copilot.def)
fi

if [ "$WHAT" = all ] || [ "$WHAT" = ollama ]; then
  echo "Building ollama.sif ..."
  apptainer build ${BUILD_OPTS:-} --force "$OUT/ollama.sif" containers/ollama.def
fi
echo "Done: $(ls -1 "$OUT"/*.sif 2>/dev/null | tr '\n' ' ')"
echo "Copy them to Ada (e.g. scp $OUT/*.sif ada:~/rnaseq-copilot/) and see containers/ada/README.md."
