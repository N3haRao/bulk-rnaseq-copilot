#!/usr/bin/env bash
# Build the Apptainer images of the RNA-seq analysis site.
#
#   containers/build.sh                 # both images (rnaseq-copilot.sif, ollama.sif) into containers/build/
#   containers/build.sh app             # only the site image
#   containers/build.sh ollama          # only the model-server image
#   BUILD_OPTS="--fakeroot" containers/build.sh   # e.g. on a cluster node where you are not root
#   BUILD_WITH=docker containers/build.sh         # build inside Docker with a current Apptainer (needs `docker`
#                                                 # to work for you; e.g. fuji, whose Singularity 3.7 is too old)
# The images then run with Apptainer 1.x or Singularity 3.6+.
#
# Needs Apptainer 1.2+ (multi-stage builds), git, about 25 GB of free disk space
# for the build, and internet access (Docker Hub, conda-forge, bioconda,
# Bioconductor data packages, r-universe, GEO). The site image is about 6-8 GB.
set -euo pipefail
cd "$(dirname "$0")/.."
REPO=$(pwd)
OUT=$REPO/containers/build
WHAT=${1:-all}
mkdir -p "$OUT"
APPTAINER_DEB=https://github.com/apptainer/apptainer/releases/download/v1.3.6/apptainer_1.3.6_amd64.deb
if [ "${BUILD_WITH:-}" = docker ]; then
  docker info >/dev/null 2>&1 || { echo "docker does not work for you (are you in the docker group?)" >&2; exit 1; }
  mkdir -p "$OUT/tmp" "$OUT/cache"
  # build_sif <folder with the build files> <recipe, relative to it> <image name>: Apptainer 1.3 as root in a
  # throw-away Ubuntu container; its temporary files and cache go to $OUT (Docker's own disk may be small)
  build_sif() {
    docker run --rm --privileged -v "$1:/ctx:ro" -v "$OUT:/out" -w /ctx \
      -e APPTAINER_TMPDIR=/out/tmp -e APPTAINER_CACHEDIR=/out/cache ubuntu:22.04 bash -c "
        set -e
        apt-get update -qq && apt-get install -y -qq wget ca-certificates >/dev/null
        wget -q -O /tmp/apptainer.deb $APPTAINER_DEB && apt-get install -y -qq /tmp/apptainer.deb >/dev/null
        apptainer build --force /out/$3 $2
        chown -R $(id -u):$(id -g) /out/$3 /out/tmp /out/cache"
  }
else
  if ! command -v apptainer >/dev/null && command -v singularity >/dev/null; then apptainer() { singularity "$@"; }; fi
  command -v apptainer >/dev/null || { echo "apptainer not found (module load apptainer?), or use BUILD_WITH=docker" >&2; exit 1; }
  build_sif() { (cd "$1" && apptainer build ${BUILD_OPTS:-} --force "$OUT/$3" "$2"); }
fi

if [ "$WHAT" = all ] || [ "$WHAT" = app ]; then
  [ -f codebank/manifest/tools.yaml ] || { echo "The codebank submodule is empty: git submodule update --init" >&2; exit 1; }
  CTX=$(mktemp -d "$OUT/context.XXXX")
  trap 'rm -rf "$CTX"' EXIT
  echo "Preparing the build folder in $CTX ..."
  # the site's tracked files only (no data/, .venv, node_modules)
  git archive HEAD backend frontend containers ops | tar -x -C "$CTX"
  # the code bank as a real git clone at the pinned commit, so runs record their exact commit
  COMMIT=$(git -C codebank rev-parse HEAD)
  git -c advice.detachedHead=false clone --quiet --no-local codebank "$CTX/codebank"
  git -C "$CTX/codebank" -c advice.detachedHead=false checkout --quiet "$COMMIT"
  git -C "$CTX/codebank" remote set-url origin "$(git -C codebank remote get-url origin)"
  echo "$(git describe --always --dirty) ($(git rev-parse --abbrev-ref HEAD))" > "$CTX/VERSION"
  if ! git diff --quiet HEAD -- backend frontend containers ops; then
    echo "NOTE: you have uncommitted changes in backend/frontend/containers; the image uses the last commit." >&2
  fi
  echo "Building rnaseq-copilot.sif (code bank $COMMIT) ..."
  build_sif "$CTX" containers/rnaseq-copilot.def rnaseq-copilot.sif
fi

if [ "$WHAT" = all ] || [ "$WHAT" = ollama ]; then
  echo "Building ollama.sif ..."
  build_sif "$REPO" containers/ollama.def ollama.sif
fi
echo "Done: $(ls -1 "$OUT"/*.sif 2>/dev/null | tr '\n' ' ')"
echo "Copy them to Ada (e.g. scp $OUT/*.sif ada:~/rnaseq-copilot/) and see containers/ada/README.md."
