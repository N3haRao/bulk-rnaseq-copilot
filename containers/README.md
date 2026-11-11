# Containers for Ada (Apptainer)

Step-by-step commands from logging in with ssh: [docs/ada_setup_commands.sh](../docs/ada_setup_commands.sh).

Two images:

| Image | Contains | Size |
|---|---|---|
| `rnaseq-copilot.sif` (`rnaseq-copilot.def`) | The software: Python for the website, Node to build its frontend, Chromium for PDFs, R 4.5 with every code-bank package (DESeq2, apeglm, fgsea + the MSigDB database, DEXSeq, stageR, tximport, annotation and example data), and fastp, STAR, samtools, Salmon and rMATS for the FASTQ steps. Also a copy of the site and code bank, used only when no site folder is mounted | about 6-8 GB |
| `ollama.sif` (`ollama.def`) | The Ollama model server for the open-weight assistants (`qwen3:30b` on a GPU with 20 GB or more, `qwen3:8b` otherwise) and the release agent (`qwen3-coder:30b`). Models are kept outside the image, in a folder bound to `/models` | about 2 GB |

**The code is not taken from the image on the server.** With `COPILOT_HOME` set, the site runs from `$COPILOT_HOME/copilot` and the code bank from `$COPILOT_HOME/codebank-live`, both mounted into the container at the same path (`site-env.sh`). A code bank release or a site update then needs only a restart (see [docs/RELEASES.md](../docs/RELEASES.md)). Rebuild the image only when the software changes: `env-*.yml`, `backend/requirements.txt` or `frontend/package-lock.json`.

Package versions are pinned in `env-r.yml`, `env-tools.yml` and `env-app.yml`, at the versions all test suites passed with: main pipeline, splicing (20), isoforms (15), FASTQ/BAM steps (22) and website (78). The exact environments are also exported into the image (`/opt/app/env-*.lock.yml`).

## 1. Build the images

You need Apptainer 1.2 or newer (or Docker, with `BUILD_WITH=docker`), git, about 25 GB of free disk, and internet access (Docker Hub, conda-forge, bioconda, Bioconductor, Zenodo for MSigDB, GEO for one example dataset). Build on Ada if Apptainer there allows building (`--fakeroot`), or on any Linux machine where you are root, then copy the `.sif` files.

```bash
git clone --recurse-submodules -b main https://github.com/N3haRao/bulk-rnaseq-copilot.git
cd bulk-rnaseq-copilot
containers/build.sh                          # both images -> containers/build/
BUILD_OPTS="--fakeroot" containers/build.sh  # where you are not root (if the cluster allows it)
BUILD_WITH=docker containers/build.sh        # inside Docker, with a current Apptainer (e.g. fuji: Singularity 3.7 cannot build)
```

`build.sh` builds from the last **commit** (uncommitted changes are left out) and copies the code bank as a git clone at the pinned commit. Every run then records that commit, and the code .zip contains it. At the end of the build Apptainer runs the image's own `%test` section: R packages, tools, Python packages, frontend and code-bank commit. To run the full test suites inside the finished image (about 10 minutes):

```bash
apptainer run --app tests containers/build/rnaseq-copilot.sif
```

## 2. Set up a folder on Ada

```bash
mkdir -p ~/rnaseq-copilot                    # or a project folder: COPILOT_HOME=/project/<lab>/rnaseq-copilot
scp containers/build/*.sif ada:~/rnaseq-copilot/
# on Ada, from a clone of this repository:
COPILOT_HOME=~/rnaseq-copilot containers/ada/pull_model.sh     # site model (qwen3:30b on a 20 GB+ GPU, else qwen3:8b) + qwen3-coder:30b; needs internet
COPILOT_HOME=~/rnaseq-copilot containers/ada/setup_home.sh     # copilot/ and codebank/ folders, first release live
```

The folder then holds:

```
rnaseq-copilot.sif   ollama.sif
copilot/             the live site (git); code bank releases are merged in by ops/release
codebank/            the code bank (git); bioinformaticians commit and run scripts/release.sh here
codebank-live        -> codebank-releases/vX.Y.Z, the release the site uses
models/              open-weight models (pull_model.sh)
data/                uploads, runs, chats, the saved API key: private (chmod 700)
secrets.env          optional: ANTHROPIC_API_KEY=sk-ant-...  (chmod 600), instead of the Settings page
```

## 3. Run the site as a job

```bash
cd ~/rnaseq-copilot
sbatch copilot/containers/ada/site.slurm                 # COPILOT_HOME=... sbatch ... for another folder
sbatch --gres=gpu:1 copilot/containers/ada/site.slurm    # on a server with GPUs
OLLAMA_MODEL=none sbatch copilot/containers/ada/site.slurm   # without the open-weight model
cat rnaseq-copilot-<jobid>.log                            # shows the node, the port and the ssh command
```

On your computer, open the tunnel the log prints and keep it open:

```bash
ssh -N -L 8000:127.0.0.1:<port> -J <you>@<ada-login> <you>@<node>
```

Then open <http://localhost:8000>. The job starts the model server (on the GPU if the job has one, otherwise on the CPU) and then the site. If the frontend sources are newer than the built frontend (after an update), the job rebuilds it first. Both listen only on the compute node's loopback interface (127.0.0.1), so they can only be reached through your tunnel. Stop with `scancel <jobid>`; the data folder keeps everything for the next job.

The script asks for no partition or GPU, so it runs on Ada's default partition (`defq`, one node, no GPU). On another server add `--partition` / `--gres` on the sbatch command line. The GPU is only for the model. Without one, the model runs on the CPU (slowly), and with `OLLAMA_MODEL=none` the site uses Claude (if a key is saved) or its built-in rules.

## A server without SLURM (fuji: one NVIDIA A30, 24 GB)

The images run with Apptainer 1.x or Singularity 3.6+: the scripts call `singularity` when there is no `apptainer` command (fuji has Singularity 3.7.4).

The same scripts work without a scheduler. Step by step: [docs/fuji_setup_commands.sh](../docs/fuji_setup_commands.sh).

- `containers/ada/start_site.sh` starts the site as a background process that keeps running after you log out (with SLURM it submits the job instead). `stop_site.sh` and `restart_site.sh` stop and restart it. The log prints the tunnel command, `ssh -N -L 8000:127.0.0.1:<port> you@fuji.nri.bcm.edu`.
- Releases: `scripts/release.sh` runs `release.slurm` directly. It reuses the site's model server, so a single Ollama holds the GPU.
- GPU memory: on the A30 both models are Qwen3 30B, run fully on the GPU. The site's assistants use `qwen3:30b` (about 19–20 GB), chosen automatically on a GPU with 20 GB or more; `OLLAMA_MODEL=...` overrides it. The release agent uses `qwen3-coder:30b` (about 21 GB with a 32k context, using flash attention and an 8-bit KV cache). Only one fits at a time, so Ollama swaps them in 10–20 s (`OLLAMA_MAX_LOADED_MODELS=1`). During a release the site's assistant waits for the GPU (up to `OLLAMA_TIMEOUT`, 300 s), then falls back to its built-in rules.
- FASTQ jobs: `nohup bash containers/ada/upstream.slurm ... &`.

## Code bank releases

`scripts/release.sh` in `$COPILOT_HOME/codebank` tags a release and submits `containers/ada/release.slurm`. That job starts `qwen3-coder:30b`, checks the site against the release in a staging copy, lets the release agent fix what is needed, and writes a review for the bioinformatician who released it. They approve with `copilot/ops/release approve vX.Y.Z`, which restarts the site through `containers/ada/restart_site.sh`. Step by step: [docs/RELEASES.md](../docs/RELEASES.md).

## 4. FASTQ files: batch jobs

`codebank/later/scripts/run_upstream.R` runs trimming, STAR, Salmon, gene counts, rMATS, splicing, sashimi plots and isoforms from one config file (see `codebank/later/README.md`):

```bash
CONFIG=/project/<lab>/exp1/upstream.yaml sbatch copilot/containers/ada/upstream.slurm     # uses the live code bank release
```

STAR needs about 32 GB of memory for human or mouse (the script asks for 64 GB and 16 CPUs). The results include `counts.csv` and `samples.csv` for the site, plus Salmon and rMATS output for the **Splicing & isoforms** tab. Any code-bank script can also be run directly:

```bash
apptainer run --app rscript rnaseq-copilot.sif /opt/app/codebank/isoforms/scripts/run_isoforms.R --help
```

## Notes for the Ada admins / before wider use

- **One user per job.** The site has no logins yet. Each person runs their own job with their own data folder, reachable only through their own ssh tunnel. A shared multi-user service needs user accounts first; that is on the to-do list.
- **The chat assistant's code** runs inside the same container. `--containall --cleanenv` gives it no home folder and no environment secrets, and it only sees the bound data folder.
- **Outbound internet.** The Claude chat needs to reach `api.anthropic.com` from the compute node. If Ada requires a proxy, export `HTTPS_PROXY` before `sbatch`, and `site.slurm` passes it on. If there is no internet at all, everything else works: the analysis, the open-weight assistants and the reports.
- **Images are read-only.** Everything that changes is written to the bound data folder, under `data/`.
- **Versions in an image:** `apptainer exec rnaseq-copilot.sif cat /opt/app/IMAGE_VERSIONS.txt`.
- **SkyRiver later:** the same images run there. Only the SLURM settings and the port forwarding will differ.

## What was tested where

These recipes were written and partly checked in a cloud sandbox that cannot run Apptainer's build steps (it lacks the needed Linux capabilities):

- **Checked there:**
  - the three conda environments resolve with the pinned versions;
  - the base images and the micromamba release exist;
  - `build.sh` stages the build folder correctly;
  - Apptainer reads the recipe, pulls the first image and copies the files in.
- **Not checked there:** the first complete build. Its `%test` section and `apptainer run --app tests` are how to confirm it on Ada.
