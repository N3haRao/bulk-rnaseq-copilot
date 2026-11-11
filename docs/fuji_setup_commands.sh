# =============================================================================
# RNA-seq analysis site on fuji (BCM): one GPU server (NVIDIA A30, 24 GB), no SLURM
# =============================================================================
# Paste the blocks ONE AT A TIME, in order. [Mac] = Terminal on your Mac, [fuji] = the ssh session.
#
# What you end up with: ~/rnaseq-copilot on fuji with the images, the models, the site folders and
# a data folder. The site runs as a background process (no SLURM on fuji) and you open it in your
# Mac's browser through an ssh tunnel. Both open-weight models are Qwen3 30B, fully on the A30:
#   qwen3:30b        the site's assistants (setup chat, report writer), ~19-20 GB of GPU memory
#   qwen3-coder:30b  the release agent, ~21 GB with its 32k context (flash attention + 8-bit KV cache)
# The scripts pick qwen3:30b for the site by themselves on a GPU with 20 GB or more (qwen3:8b elsewhere).
# Only one 30B model fits in 24 GB at a time: Ollama swaps them automatically (~10-20 s). During a
# release (10-30 minutes) the site's assistant waits for the GPU, or falls back to its built-in rules.
# Ada's guide (docs/ada_setup_commands.sh) has more troubleshooting; most of it applies here too.
# =============================================================================


# -----------------------------------------------------------------------------
# STEP 1. [Mac] Log in (and set up a key + short name, once)
# -----------------------------------------------------------------------------
ssh user@fuji.nri.bcm.edu                     # off campus: BCM VPN first
# once, from the Mac:
ssh-copy-id user@fuji.nri.bcm.edu
cat >> ~/.ssh/config <<'EOF'

Host fuji
    HostName fuji.nri.bcm.edu
    User user
    ServerAliveInterval 60
EOF
ssh fuji


# -----------------------------------------------------------------------------
# STEP 2. [fuji] Check the machine (send me the output if anything looks off)
# -----------------------------------------------------------------------------
nvidia-smi                                     # the A30; note the driver version (needs 450+; 535+ ideal)
apptainer --version || singularity --version || (module load apptainer && apptainer --version)
docker --version 2>/dev/null                   # only if there is no Apptainer
nproc; free -g; df -h ~ /data* /scratch* 2>/dev/null   # CPUs, memory, disk (~100 GB needed; more for FASTQ)
curl -sI https://ollama.com | head -1          # internet (for the models and the Claude chat)
curl -sI https://api.anthropic.com | head -1
# Needed: Apptainer 1.2+ (or Singularity 3.8+). Without it, ask Dr. Hari / IT to install Apptainer
# (it needs no root to run images). Docker alone would need a different set of scripts: tell me.


# -----------------------------------------------------------------------------
# STEP 3. [fuji] The code and the images
# -----------------------------------------------------------------------------
export COPILOT_HOME=$HOME/rnaseq-copilot        # or a bigger disk, e.g. /data/user/rnaseq-copilot
echo "export COPILOT_HOME=$COPILOT_HOME" >> ~/.bashrc
echo 'export COPILOT_RESTART_CMD="$COPILOT_HOME/copilot/containers/ada/restart_site.sh"' >> ~/.bashrc
mkdir -p $COPILOT_HOME && cd $COPILOT_HOME
git clone --recurse-submodules -b main https://github.com/N3haRao/bulk-rnaseq-copilot.git
cd bulk-rnaseq-copilot
# Build the images here if Apptainer allows it (1-2 hours; needs internet and ~25 GB free):
mkdir -p $COPILOT_HOME/apptainer-cache $COPILOT_HOME/apptainer-tmp
export APPTAINER_CACHEDIR=$COPILOT_HOME/apptainer-cache APPTAINER_TMPDIR=$COPILOT_HOME/apptainer-tmp
BUILD_OPTS="--fakeroot" containers/build.sh && mv containers/build/*.sif $COPILOT_HOME/
# ...or copy images built on Ada:  scp ada:rnaseq-copilot/*.sif $COPILOT_HOME/   (they are the same)
apptainer exec --nv $COPILOT_HOME/ollama.sif nvidia-smi -L    # the container sees the A30


# -----------------------------------------------------------------------------
# STEP 4. [fuji] Models and the site folders (once)
# -----------------------------------------------------------------------------
containers/ada/pull_model.sh                   # qwen3:30b + qwen3-coder:30b into $COPILOT_HOME/models (~38 GB)
containers/ada/setup_home.sh                   # copilot/ (the live site), codebank/ (where releases are made)


# -----------------------------------------------------------------------------
# STEP 5. [fuji + Mac] Start the site and open it
# -----------------------------------------------------------------------------
# Optional: the Anthropic API key in a private file instead of the Settings page
#   ( umask 077; echo "ANTHROPIC_API_KEY=sk-ant-..." > $COPILOT_HOME/secrets.env )
$COPILOT_HOME/copilot/containers/ada/start_site.sh
# It prints a line like   ssh -N -L 8000:127.0.0.1:41234 user@fuji.nri.bcm.edu
# [Mac] paste that line in a new Terminal window and keep it open; open http://localhost:8000
# The site keeps running after you log out. Stop / restart:
#   $COPILOT_HOME/copilot/containers/ada/stop_site.sh
#   $COPILOT_HOME/copilot/containers/ada/restart_site.sh      (new port: use the new tunnel line)
# Log:  ls -t $COPILOT_HOME/rnaseq-copilot-*.log | head -1 | xargs tail -f
# GPU use while it runs:  nvidia-smi    (the log's "Model server:" line should say qwen3:30b on the GPU)


# -----------------------------------------------------------------------------
# STEP 6. [fuji] Code bank releases (a bioinformatician)
# -----------------------------------------------------------------------------
cd $COPILOT_HOME/codebank                      # commit the change, bump the version in 3 places, then:
scripts/release.sh 0.6.1
# Without SLURM this runs the release check right away, on the A30 (the site's model server is
# reused). It ends with READY or NEEDS ATTENTION and the review to read:
less $COPILOT_HOME/staging/v0.6.1/RELEASE_REVIEW.md
$COPILOT_HOME/copilot/ops/release approve v0.6.1      # restarts the site with the new version
# Step by step: $COPILOT_HOME/copilot/docs/RELEASES.md


# -----------------------------------------------------------------------------
# STEP 7. [fuji] FASTQ files -> counts, splicing, isoforms
# -----------------------------------------------------------------------------
# References, config and sample sheet as in docs/ada_setup_commands.sh step 8, then (no sbatch here;
# nohup keeps it running after you log out; set threads in the config to what nproc showed):
CONFIG=$COPILOT_HOME/experiments/exp1/upstream.yaml BIND=$COPILOT_HOME \
  nohup bash $COPILOT_HOME/copilot/containers/ada/upstream.slurm > $COPILOT_HOME/upstream-exp1.log 2>&1 &
tail -f $COPILOT_HOME/upstream-exp1.log
