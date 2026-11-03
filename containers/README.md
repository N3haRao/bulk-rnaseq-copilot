# Containers for Ada (Apptainer)

Step-by-step commands from logging in with ssh: [docs/ada_setup_commands.sh](../docs/ada_setup_commands.sh).

Two images:

| Image | Contains | Size |
|---|---|---|
| `rnaseq-copilot.sif` (`rnaseq-copilot.def`) | The website (backend + built frontend, Chromium for PDFs), R 4.5 with every code-bank package (DESeq2, apeglm, fgsea + the MSigDB database, DEXSeq, stageR, tximport, annotation and example data), the code bank as a git clone at the commit the site pins, and fastp, STAR, samtools, Salmon and rMATS for the FASTQ steps | about 6-8 GB |
| `ollama.sif` (`ollama.def`) | The Ollama model server for the open-weight assistants. Models are kept outside the image, in a folder bound to `/models` | about 2 GB |

Package versions are pinned in `env-r.yml`, `env-tools.yml` and `env-app.yml`, at the versions all test suites passed with: main pipeline, splicing (20), isoforms (15), FASTQ/BAM steps (22) and website (78). The exact environments are also exported into the image (`/opt/app/env-*.lock.yml`).

## 1. Build the images

You need Apptainer 1.2 or newer, git, about 25 GB of free disk, and internet access (Docker Hub, conda-forge, bioconda, Bioconductor, Zenodo for MSigDB, GEO for one example dataset). Build on Ada if Apptainer there allows building (`--fakeroot`), or on any Linux machine where you are root, then copy the `.sif` files.

```bash
git clone --recurse-submodules -b main https://github.com/N3haRao/bulk-rnaseq-copilot.git
cd agentic-omics-testbed
containers/build.sh                          # both images -> containers/build/
BUILD_OPTS="--fakeroot" containers/build.sh  # where you are not root (if the cluster allows it)
```

`build.sh` builds from the last **commit** (uncommitted changes are left out) and copies the code bank as a git clone at the pinned commit. Every run then records that commit, and the code .zip contains it. At the end of the build Apptainer runs the image's own `%test` section: R packages, tools, Python packages, frontend and code-bank commit. To run the full test suites inside the finished image (about 10 minutes):

```bash
apptainer run --app tests containers/build/rnaseq-copilot.sif
```

## 2. Set up a folder on Ada

```bash
mkdir -p ~/rnaseq-copilot                    # or a project folder: COPILOT_DIR=/project/<lab>/rnaseq-copilot
scp containers/build/*.sif ada:~/rnaseq-copilot/
# on an Ada login node (needs internet, once): download the model into ~/rnaseq-copilot/models
COPILOT_DIR=~/rnaseq-copilot containers/ada/pull_model.sh            # qwen3:8b, about 5 GB
```

The folder then holds:

```
rnaseq-copilot.sif   ollama.sif
models/              open-weight models (pull_model.sh)
data/                uploads, runs, chats, the saved API key: private (chmod 700, set by site.slurm)
secrets.env          optional: ANTHROPIC_API_KEY=sk-ant-...  (chmod 600), instead of the Settings page
```

## 3. Run the site as a job

```bash
sbatch containers/ada/site.slurm             # COPILOT_DIR=... sbatch ... for another folder
cat rnaseq-copilot-<jobid>.log               # shows the node, the port and the ssh command
```

On your computer, open the tunnel the log prints and keep it open:

```bash
ssh -N -L 8000:127.0.0.1:<port> -J <you>@<ada-login> <you>@<node>
```

Then open <http://localhost:8000>. The job starts the model server (on the GPU if the node has one) and then the site. Both listen only on the compute node's loopback interface (127.0.0.1), so they can only be reached through your tunnel. Stop with `scancel <jobid>`; the data folder keeps everything for the next job.

Adjust the `#SBATCH` lines (partition, GPU, memory, time) to Ada's policies. The GPU is only for the model: without one, remove `--gres` and the model runs on the CPU (slowly), or the site falls back to its built-in rules.

## 4. FASTQ files: batch jobs

`codebank/later/scripts/run_upstream.R` runs trimming, STAR, Salmon, gene counts, rMATS, splicing, sashimi plots and isoforms from one config file (see `codebank/later/README.md`):

```bash
CONFIG=/project/<lab>/exp1/upstream.yaml sbatch containers/ada/upstream.slurm
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
