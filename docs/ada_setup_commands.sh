# =============================================================================
# RNA-seq analysis site on Ada (BCM): full setup, from logging in with ssh
# =============================================================================
# Paste the blocks ONE AT A TIME, in order. Each block says where to run it:
#   [Mac]  in Terminal on your Mac
#   [Ada]  in the ssh session on Ada (after step 1)
# Lines starting with # are comments.
#
# What you end up with:
#   ~/rnaseq-copilot (or a project folder) on Ada with the two container images,
#   the model, and a data folder; the site runs as a SLURM job and you open it in
#   your Mac's browser through an ssh tunnel; FASTQ files are processed as batch jobs.
#
# Things only Ada's admins can tell you (ask them once, see step 2): the SLURM
# partition / GPU names, whether you may build Apptainer images on Ada, whether
# compute nodes can reach the internet (for the Claude chat), and the project
# storage path. The commands below work with the usual answers and say what to
# change otherwise.
# =============================================================================


# -----------------------------------------------------------------------------
# STEP 1. [Mac] Log in to Ada with ssh (and set up a short name + key, once)
# -----------------------------------------------------------------------------
# Off campus: connect to the BCM VPN first.
ssh nehar@ada.nri.bcm.edu
# Type your BCM password (nothing appears as you type). You are on an Ada login node
# when the prompt changes. Type  exit  to come back to the Mac.

# Once, so you do not type the password every time and can write  ssh ada :
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -C "nehar@mac"       # press Enter at the questions
ssh-copy-id nehar@ada.nri.bcm.edu                                       # asks for the password one last time
cat >> ~/.ssh/config <<'EOF'

Host ada
    HostName ada.nri.bcm.edu
    User nehar
    ServerAliveInterval 60
EOF
ssh ada                       # should log in without a password now
# TROUBLESHOOTING
# - "Connection timed out" / "could not resolve hostname": not on the BCM network -> VPN.
# - "Permission denied (publickey,...)" after ssh-copy-id: some clusters only accept
#   passwords or need keys registered by the admins; keep using the password.
# - ssh-copy-id: command not found ->  brew install ssh-copy-id


# -----------------------------------------------------------------------------
# STEP 2. [Ada] Look around: Apptainer, SLURM partitions, GPUs, storage
# -----------------------------------------------------------------------------
module avail apptainer singularity 2>&1 | head -20      # is there an Apptainer module?
module load apptainer 2>/dev/null || module load singularity 2>/dev/null
apptainer --version || singularity --version            # need 1.2 or newer
sinfo -s                                                # partitions
sinfo -o "%P %G %l %c %m" | column -t                   # partition, GPUs, time limit, CPUs, memory
df -h ~                                                  # your home quota
ls -d /project* /scratch* /work* /storage* 2>/dev/null   # larger storage areas, if any
# Write down: a partition with GPUs (for the model) and one for big CPU jobs (STAR),
# and a folder with ~100 GB free for images, models, references and runs.
# TROUBLESHOOTING
# - No apptainer / singularity module: ask the admins to install Apptainer (1.2+).
#   Without it nothing below works.
# - Only "singularity" exists: every command below works with  singularity  in place
#   of  apptainer  (the job scripts already try both modules).


# -----------------------------------------------------------------------------
# STEP 3. [Ada] Choose the working folder and get the code
# -----------------------------------------------------------------------------
# Home is fine to start; a project folder is better (images ~10 GB, model 5 GB,
# references and STAR indexes 30-60 GB per species). Change the path if you have one:
export COPILOT_DIR=$HOME/rnaseq-copilot
echo "export COPILOT_DIR=$COPILOT_DIR" >> ~/.bashrc
mkdir -p $COPILOT_DIR && cd $COPILOT_DIR

git clone --recurse-submodules -b claude/brave-hawking-jy7mje https://github.com/N3haRao/agentic-omics-testbed.git
cd agentic-omics-testbed && git log --oneline -1 && git -C codebank log --oneline -1
# TROUBLESHOOTING
# - GitHub asks for a password (private repository): use a personal access token as the
#   password (github.com -> Settings -> Developer settings -> Personal access tokens), or
#   make an ssh key on Ada (ssh-keygen -t ed25519), add ~/.ssh/id_ed25519.pub to GitHub
#   (Settings -> SSH keys) and clone git@github.com:N3haRao/agentic-omics-testbed.git
# - "Could not resolve host: github.com": the login node has no internet; ask the admins
#   which node can reach the internet (often a "data transfer" node), or clone on the Mac and
#   copy:  [Mac] scp -r ~/projects/agentic-omics-testbed ada:rnaseq-copilot/


# -----------------------------------------------------------------------------
# STEP 4. [Ada] Build the two images (once per new version; 1-2 hours)
# -----------------------------------------------------------------------------
# Building is heavy: do it in an interactive job, not on the login node.
srun --pty --cpus-per-task=8 --mem=32G --time=04:00:00 bash
# (add  -p <partition>  if sinfo showed you need one)
module load apptainer 2>/dev/null || module load singularity 2>/dev/null
cd $COPILOT_DIR/agentic-omics-testbed
# Apptainer's cache and temporary files are big: keep them out of home and /tmp
mkdir -p $COPILOT_DIR/apptainer-cache $COPILOT_DIR/apptainer-tmp
export APPTAINER_CACHEDIR=$COPILOT_DIR/apptainer-cache APPTAINER_TMPDIR=$COPILOT_DIR/apptainer-tmp
BUILD_OPTS="--fakeroot" containers/build.sh
mv containers/build/*.sif $COPILOT_DIR/
exit                                                     # leave the interactive job
ls -lh $COPILOT_DIR/*.sif                                 # rnaseq-copilot.sif (~6-8 GB), ollama.sif (~2 GB)
# The build ends with "image test: ok" (the image's own checks).
# TROUBLESHOOTING
# - "fakeroot ... not allowed" / "newuidmap" / "no mapping entry": building is not allowed on
#   Ada for normal users. Options, in order:
#     1. ask the admins to enable --fakeroot for you (they add you to /etc/subuid), or
#     2. ask the admins to run  containers/build.sh  for you, or
#     3. build on any Linux x86_64 machine where you are root (a lab workstation or a cloud
#        VM: sudo containers/build.sh), then  scp *.sif ada:rnaseq-copilot/
#   (A Mac cannot build these images: Apptainer needs Linux, and Ada is x86_64.)
# - Errors downloading from Docker Hub / conda / Bioconductor / Zenodo: the build node needs
#   internet. Ask which node has it, or use option 3.
# - "no space left on device": point APPTAINER_CACHEDIR / APPTAINER_TMPDIR at a bigger folder.
# - Anything else: copy the last 40 lines of the build output and send them to me.


# -----------------------------------------------------------------------------
# STEP 5. [Ada] Check the image (10-15 minutes)
# -----------------------------------------------------------------------------
srun --pty --cpus-per-task=4 --mem=16G --time=01:00:00 bash
module load apptainer 2>/dev/null || module load singularity 2>/dev/null
apptainer exec $COPILOT_DIR/rnaseq-copilot.sif cat /opt/app/IMAGE_VERSIONS.txt
apptainer run --app tests $COPILOT_DIR/rnaseq-copilot.sif 2>&1 | tail -20
exit
# Expected: the splicing (20), isoform (15) and FASTQ/BAM (22) tests end with "0 failed",
# and the website tests end with "passed". Send me the output if not.


# -----------------------------------------------------------------------------
# STEP 6. [Ada] Download the open-weight model (once; ~5 GB)
# -----------------------------------------------------------------------------
# Needs internet: run on the login node (or the node your admins point you to).
cd $COPILOT_DIR/agentic-omics-testbed
containers/ada/pull_model.sh                    # qwen3:8b into $COPILOT_DIR/models
# A bigger model if Ada's GPUs allow it (better setup suggestions):
#   containers/ada/pull_model.sh qwen3:14b      and later  OLLAMA_MODEL=qwen3:14b sbatch ...
# TROUBLESHOOTING
# - "pull model manifest: ... dial tcp": no internet here; try another node, or pull on the Mac
#   (ollama pull qwen3:8b) and copy ~/.ollama/models to Ada:
#       [Mac] rsync -a ~/.ollama/models/ ada:rnaseq-copilot/models/


# -----------------------------------------------------------------------------
# STEP 7. [Ada + Mac] Start the site and open it on the Mac
# -----------------------------------------------------------------------------
# [Ada] Optional: the Anthropic API key in a private file instead of the Settings page
#   ( umask 077; echo "ANTHROPIC_API_KEY=sk-ant-..." > $COPILOT_DIR/secrets.env )
# [Ada] Start the job (add -p <gpu partition> if needed; without a GPU remove "--gres" in the script):
cd $COPILOT_DIR/agentic-omics-testbed
sbatch containers/ada/site.slurm
squeue -u nehar                                 # wait until the state is R (running)
cat rnaseq-copilot-*.log                        # shows the node, the port and the ssh command
# The log contains a line like:
#   ssh -N -L 8000:127.0.0.1:41234 -J nehar@ada.nri.bcm.edu nehar@<node>
#
# [Mac] Paste THAT line into a new Terminal window and leave it open (it prints nothing):
#   ssh -N -L 8000:127.0.0.1:<port> -J nehar@ada.nri.bcm.edu nehar@<node>
# [Mac] Open http://localhost:8000 in your browser. Use the site as on the Mac.
#
# [Ada] Stop the site when you are done (your data stays in $COPILOT_DIR/data):
#   scancel <job id>                             # the number squeue shows
# TROUBLESHOOTING
# - The job stays PD (pending): the partition is busy or the request is too big; check with
#   squeue -u nehar  (the REASON column). Try a shorter --time or another partition.
# - The log says the model is missing: step 6. The site still works with built-in rules.
# - "channel 0: open failed: connect failed" in the tunnel window: the site is not up yet
#   (look at the end of the log) or the port in the command is not the one in the log.
# - The jump (-J) to the compute node is refused: tunnel in two hops instead:
#       [Mac] ssh -L 8000:127.0.0.1:9000 ada        then, in that Ada session:
#       [Ada] ssh -N -L 9000:127.0.0.1:<port> <node>
# - "address already in use" on the Mac: something else uses port 8000; use 8001 in the
#   ssh command and open http://localhost:8001
# - The chat says it cannot reach Anthropic: compute nodes have no internet. Ask the admins for
#   a proxy (then: export HTTPS_PROXY=http://<proxy>:<port> before sbatch). Everything else works.
# - "Settings" says the key can only be changed on this computer: open the site through the
#   tunnel (localhost), not the node's name.
# - The job ends after 8 hours (--time in site.slurm): start it again; nothing is lost.


# -----------------------------------------------------------------------------
# STEP 8. [Ada] FASTQ files -> counts, splicing, isoforms (batch jobs)
# -----------------------------------------------------------------------------
# 8a. Reference files, once per species (GENCODE; ~5 GB download, ~35 GB with the STAR index).
#     Human (GRCh38, GENCODE 44):
mkdir -p $COPILOT_DIR/refs/human && cd $COPILOT_DIR/refs/human
G=https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_44
wget -c $G/GRCh38.primary_assembly.genome.fa.gz $G/gencode.v44.primary_assembly.annotation.gtf.gz $G/gencode.v44.transcripts.fa.gz
gunzip -k *.gz                                   # STAR needs them uncompressed
#     Mouse (GRCm39, GENCODE M33):
mkdir -p $COPILOT_DIR/refs/mouse && cd $COPILOT_DIR/refs/mouse
G=https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_mouse/release_M33
wget -c $G/GRCm39.primary_assembly.genome.fa.gz $G/gencode.vM33.primary_assembly.annotation.gtf.gz $G/gencode.vM33.transcripts.fa.gz
gunzip -k *.gz
# (If a link does not work, the files are listed on https://www.gencodegenes.org .)

# 8b. One folder per experiment with the FASTQ files (Globus, rsync or scp) and a sample sheet:
mkdir -p $COPILOT_DIR/experiments/exp1/fastq && cd $COPILOT_DIR/experiments/exp1
#   [Mac] rsync -av --progress /path/to/fastq/ ada:rnaseq-copilot/experiments/exp1/fastq/
cat > samples.tsv <<'EOF'
file_name	sample	genotype	read_orientation
WT1_R1.fastq.gz	WT1	WT	R1
WT1_R2.fastq.gz	WT1	WT	R2
KO1_R1.fastq.gz	KO1	KO	R1
KO1_R2.fastq.gz	KO1	KO	R2
EOF
# (edit it: one row per FASTQ file; tabs between the columns; single-end: only R1 rows)

# 8c. The config (paths are inside $COPILOT_DIR; the first run also builds the STAR index):
cat > upstream.yaml <<EOF
sample_sheet: $COPILOT_DIR/experiments/exp1/samples.tsv
fastq_dir: $COPILOT_DIR/experiments/exp1/fastq
out_dir: $COPILOT_DIR/experiments/exp1/results
condition_column: genotype
control: WT
genome_fasta: $COPILOT_DIR/refs/mouse/GRCm39.primary_assembly.genome.fa
gtf: $COPILOT_DIR/refs/mouse/gencode.vM33.primary_assembly.annotation.gtf
star_index: $COPILOT_DIR/refs/mouse/star_index
salmon_index: $COPILOT_DIR/refs/mouse/salmon_index
transcripts_fasta: $COPILOT_DIR/refs/mouse/gencode.vM33.transcripts.fa
threads: 16
steps: [qc, read_length, star, salmon, gene_counts, rmats, splicing, sashimi, isoforms]
EOF

# 8d. Run it (hours, depending on the number of samples; the first run builds the indexes):
CONFIG=$COPILOT_DIR/experiments/exp1/upstream.yaml BIND=$COPILOT_DIR sbatch $COPILOT_DIR/agentic-omics-testbed/containers/ada/upstream.slurm
squeue -u nehar
tail -f rnaseq-upstream-*.log                    # Ctrl+C stops watching (not the job)

# 8e. Use the results on the site (through the tunnel of step 7, in the Mac's browser):
#   - New analysis: results/counts/counts.csv + results/counts/samples.csv
#   - On that run's "Splicing & isoforms" tab:
#       splicing: a .zip of results/rmats/WT_vs_KO/tables      (rMATS sample 1 = the test group)
#       isoforms: a .zip of results/salmon (folders named like the samples; GENCODE names need no GTF)
#   The browser uploads from the Mac, so copy them there first, e.g.:
#   [Mac] rsync -av ada:rnaseq-copilot/experiments/exp1/results/counts ~/Downloads/exp1/
#   [Ada] cd $COPILOT_DIR/experiments/exp1/results && zip -r salmon.zip salmon -i '*/quant.sf' && zip -r rmats_WT_vs_KO.zip rmats/WT_vs_KO/tables
# TROUBLESHOOTING
# - The job fails at "star" with "not enough memory": raise --mem in upstream.slurm (64G -> 96G)
#   or build the index once in a bigger interactive job.
# - "Sample sheet problems: ... file not found": file_name must match the FASTQ names exactly.
# - "Unknown group(s)" / "Control 'WT' is not in column": condition_column / control must match the
#   sample sheet's column name and values.
# - Low mapping rates flagged by STAR or Salmon: wrong species reference, or adapter trouble
#   (results/trimmed/reports/*.html).
# - See results/upstream_summary.json and results/logs/<step>.log for every step.


# -----------------------------------------------------------------------------
# STEP 9. [Ada] Update to a new version
# -----------------------------------------------------------------------------
cd $COPILOT_DIR/agentic-omics-testbed
git pull && git submodule update --init --recursive
# then rebuild the site image (step 4, "app" only) and replace the old one:
#   BUILD_OPTS="--fakeroot" containers/build.sh app && mv containers/build/rnaseq-copilot.sif $COPILOT_DIR/
# The model image (ollama.sif) only needs rebuilding when containers/ollama.def changes.


# -----------------------------------------------------------------------------
# STEP 10. [Ada] Housekeeping
# -----------------------------------------------------------------------------
squeue -u nehar                                  # your running / waiting jobs
# scancel <job id>                               # stop one (the number squeue shows)
du -sh $COPILOT_DIR/*                            # what uses space
rm -rf $COPILOT_DIR/apptainer-cache $COPILOT_DIR/apptainer-tmp     # after a successful build
# Back up $COPILOT_DIR/data (all analyses) and experiments/*/results; images, models,
# references and indexes can be rebuilt or downloaded again.
