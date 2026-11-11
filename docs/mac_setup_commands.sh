# =============================================================================
# RNA-seq analysis site: full setup on a Mac 
# =============================================================================
# Paste the blocks into Terminal ONE AT A TIME, in order, and wait for each to
# finish. Lines starting with # are comments (Terminal ignores them).
# The first setup takes about 30-60 minutes, mostly R packages and the model download.
# After that, starting the site takes one command (see STEP 9).
# =============================================================================


# -----------------------------------------------------------------------------
# STEP 1. Apple's command-line developer tools (compilers, git)
# -----------------------------------------------------------------------------
xcode-select --install
# A window opens: click "Install" and wait until it finishes.
# TROUBLESHOOTING
# - "command line tools are already installed": fine, go to step 2.
# - Later errors like "xcrun: error: invalid active developer path" (common after
#   a macOS update): run  xcode-select --install  again.


# -----------------------------------------------------------------------------
# STEP 2. Homebrew (the Mac package manager)
# -----------------------------------------------------------------------------
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# It asks for your Mac password (nothing appears as you type; that is normal).

# Make the "brew" command available now and in every new Terminal window:
if [ -x /opt/homebrew/bin/brew ]; then BREW=/opt/homebrew/bin/brew; else BREW=/usr/local/bin/brew; fi
grep -q "brew shellenv" ~/.zprofile 2>/dev/null || echo "eval \"\$($BREW shellenv)\"" >> ~/.zprofile
eval "$($BREW shellenv)"
brew --version
# TROUBLESHOOTING
# - "zsh: command not found: brew": close Terminal, open a new window, try again.
# - Already have Homebrew? Run  brew update  and continue.


# -----------------------------------------------------------------------------
# STEP 3. Python, Node.js, pandoc, R and Ollama
# -----------------------------------------------------------------------------
brew install git python@3.12 node pandoc
brew install --cask r          # the official CRAN build of R (fast binary packages)
brew install ollama

# Check versions (Python must be 3.11+, Node 20+, R 4.3+):
python3.12 --version
node --version
pandoc --version | head -1
R --version | head -1
# TROUBLESHOOTING
# - "R: command not found" after installing: open a NEW Terminal window. If it
#   still fails, R lives at /Library/Frameworks/R.framework/Resources/bin/R
# - Prefer to install R by hand? Download the .pkg for your chip from
#   https://cran.r-project.org/bin/macosx/  (arm64 = Apple Silicon, x86_64 = Intel).
#   Check your chip with:  uname -m   (arm64 = Apple Silicon)
# - Do NOT use "brew install r" (without --cask): that version builds every
#   Bioconductor package from source, which is slow and often fails.


# -----------------------------------------------------------------------------
# STEP 4. Start Ollama and download the assistant's model (about 5 GB)
# -----------------------------------------------------------------------------
brew services start ollama       # runs Ollama in the background, also after restarts
sleep 5
ollama pull qwen3:8b
ollama list                      # should list qwen3:8b

# Quick test (should print a short answer; press Ctrl+D if it waits for more):
ollama run qwen3:8b "Reply with one word: hello" --think=false
# TROUBLESHOOTING
# - "could not connect to ollama app" / "connection refused":
#       brew services restart ollama
#   or start it in the foreground in a separate Terminal window:  ollama serve
# - "unknown flag: --think": your Ollama is old. Run  brew upgrade ollama
# - Download interrupted: run  ollama pull qwen3:8b  again (it resumes).
# - The Mac gets very slow while the model answers: 16 GB is enough for qwen3:8b,
#   but close big apps (Chrome tabs, Docker). A smaller model also works (see step 9).


# -----------------------------------------------------------------------------
# STEP 5. Download the website and the code bank from GitHub
# -----------------------------------------------------------------------------
mkdir -p ~/projects && cd ~/projects
git clone --recurse-submodules -b main https://github.com/N3haRao/bulk-rnaseq-copilot.git
cd bulk-rnaseq-copilot
ls codebank                      # should list scripts, R, config, ... (the code bank)
# TROUBLESHOOTING
# - "destination path 'bulk-rnaseq-copilot' already exists": you cloned before.
#   Update it instead:
#       cd ~/projects/bulk-rnaseq-copilot
#       git pull
#       git submodule update --init --recursive
# - "codebank" is empty:  git submodule update --init --recursive
# - GitHub asks for a username/password: the repositories must be reachable from
#   your account. Sign in with  brew install gh && gh auth login  and clone again.


# -----------------------------------------------------------------------------
# STEP 6. Install the R packages for the analysis (once; 10-30 minutes)
# -----------------------------------------------------------------------------
cd ~/projects/bulk-rnaseq-copilot/codebank
Rscript scripts/00_setup_environment.R
Rscript scripts/00_setup_environment.R --check     # every "required" package should have a version
# Packages for the optional isoform analysis (DEI / DUI) and for comparing human with mouse studies (babelgene), about 5 minutes:
Rscript -e 'if (!requireNamespace("BiocManager", quietly = TRUE)) install.packages("BiocManager"); BiocManager::install(c("DEXSeq", "stageR", "tximport", "babelgene"), update = FALSE, ask = FALSE)'
# TROUBLESHOOTING
# - If R asks "Update all/some/none? [a/s/n]" or to pick a mirror: type  n  (or 1) and press Enter.
# - A package fails to compile ("clang: error", "gfortran not found"):
#     1. make sure step 1 finished:  xcode-select --install
#     2. make sure you use the CRAN R from step 3:  which Rscript
#        should show /usr/local/bin/Rscript or /Library/Frameworks/...
#     3. run the setup script again; it only installs what is missing.
# - "MSigDB" / msigdbr download fails: check your internet and run the setup again.
#   Without it, everything works except pathway analysis.
# - Still stuck: copy the last 20 lines of the error and send them to me.


# -----------------------------------------------------------------------------
# STEP 7. Create the example datasets (airway, pasilla, GSE60450)
# -----------------------------------------------------------------------------
Rscript scripts/make_example_data.R
ls data/example                  # should list airway, gse60450, pasilla
# TROUBLESHOOTING
# - GSE60450 cannot be downloaded from GEO: download it in your browser from
#   https://ftp.ncbi.nlm.nih.gov/geo/series/GSE60nnn/GSE60450/suppl/GSE60450_Lactation-GenewiseCounts.txt.gz
#   then:
#       mkdir -p data/raw
#       mv ~/Downloads/GSE60450_Lactation-GenewiseCounts.txt.gz data/raw/
#       Rscript scripts/make_example_data.R gse60450

# Optional: check that the analysis code works (about 5 minutes):
Rscript tests/run_tests.R
# All lines should say [PASS] (including "H. plot options"). If only "enrichment" fails, see the msigdbr note in step 6,
# or run without it:  Rscript tests/run_tests.R --skip_enrichment
#
# Optional: the splicing and isoform modules (1-3 minutes each; should end with "0 failed"):
Rscript splicing/tests/run_splicing_tests.R | tail -3
Rscript isoforms/tests/run_isoform_tests.R | tail -3
# (later/tests/run_later_tests.R needs fastp, STAR, Salmon, rMATS: run those on Ada, see docs/ada_setup_commands.sh)


# -----------------------------------------------------------------------------
# STEP 7b. The Anthropic API key for the chat (Ask tab): nothing to type here
# -----------------------------------------------------------------------------
# After starting the site (step 8), click "Settings" in the top bar, paste your
# key from https://console.anthropic.com (Settings -> API keys) and click
# "Check and save". It is stored in the macOS Keychain.
# TROUBLESHOOTING
# - "Anthropic did not accept this key": copy it again (no spaces).
# - macOS asks whether "python" may use the keychain: click "Always Allow".
# - The Settings page says the key comes from .env: an older ANTHROPIC_API_KEY line
#   in ~/projects/bulk-rnaseq-copilot/.env takes precedence; delete that line and
#   restart ./start.sh to manage the key on the page instead.


# -----------------------------------------------------------------------------
# STEP 8. Start the website
# -----------------------------------------------------------------------------
cd ~/projects/bulk-rnaseq-copilot
./start.sh
# The first start builds the website and the Python environment (2-3 minutes).
# When it says "Open http://localhost:8000", open that address in your browser
# (or, in a NEW Terminal tab with Cmd+T:  open http://localhost:8000 ).
# It should also say "Assistant: Ollama is running".
# Keep this Terminal window open while you use the site. Stop it with Ctrl+C.
#
# THE START PAGE IS A CHAT
#   1. In "Describe your experiment", write in plain words what you have and what you
#      want, e.g. "Mouse mammary gland, luminal vs basal cells, 3 mice per group,
#      two batches. Account for batch and colour the PCA by mouse."
#   2. Click "Attach counts table" and "Attach sample table", then "Send".
#   3. On the next page the setup assistant replies (left) and fills in the
#      "Proposed setup" card (right). The card is checked automatically.
#   4. Ask for changes in the chat ("also account for sex", "use padj below 0.01",
#      "label Krt5 and Elf5 on the volcano plot"), or open "Edit the setup directly".
#   5. Click "Approve and run". Nothing runs before you approve.
#   The assistant can change the code bank's settings, never its methods: if you ask
#   for edgeR, a t-test, etc., it says so and keeps DESeq2 / fgsea.
#
# Try it with an example: "Or try an example" -> gse60450 -> "Use this dataset"
# (your description, if you typed one, is used) -> "Approve and run".
# The setup assistant's tag should say "Assistant · qwen3:8b".
#
# Or import a public dataset from GEO: "Or import a public dataset from GEO" -> type a
# GSE accession (e.g. GSE60450) -> "Look up" -> check the samples and the counts option
# (NCBI's uniform counts are preselected when GEO has them) -> "Import and set up".
# The study's description goes to the setup chat, which proposes the groups.
#
# Compare studies (top bar, or "Compare with another study" on a finished run): tick two
# or more finished analyses (and/or add a paper's DE table), set the cutoffs, click
# "Compare". You get fold-change agreement, genes significant in both (by direction),
# RRHO maps and shared pathways, plus a report and the code. Needs code bank v0.5.0.
#
# On a finished run you also get: "Download PowerPoint" (the report as slides) and
# "All code used (.zip)" (code bank version, settings, rerun script and any code the
# chat ran: for a paper's supplementary material).
#
# OPTIONAL: SPLICING & ISOFORMS (tab on a finished run)
#   - Alternative splicing: upload the rMATS output tables (SE.MATS.JCEC.txt, ...) or a .zip
#     of rMATS's output folder; choose which group was rMATS sample 1 (--b1).
#   - Transcript isoforms: upload a .zip of Salmon output folders (one folder per sample,
#     named exactly like the samples, each with quant.sf) and, unless the transcript names
#     contain the gene (GENCODE), the GTF the Salmon index was built from.
#   Both run in the background (a few minutes); results appear in the tab and are added to
#   the report, the PowerPoint, the code .zip and new chats. rMATS and Salmon output come
#   from the FASTQ files: make them on Ada (docs/ada_setup_commands.sh, step 8).
#
# TROUBLESHOOTING
# - "permission denied: ./start.sh":  chmod +x start.sh  then  ./start.sh
# - "Python 3.11 or newer is needed":  brew install python@3.12  and try again.
# - "address already in use" (port 8000 busy): stop the other program with
#       lsof -ti :8000 | xargs kill
#   or use another port:  PORT=8001 ./start.sh   (then open http://localhost:8001)
# - "Rscript was not found" on the site:
#       RSCRIPT=/Library/Frameworks/R.framework/Resources/bin/Rscript ./start.sh
# - npm errors while building: check  node --version  (20 or newer), then
#       rm -rf frontend/node_modules frontend/dist && ./start.sh
# - The setup assistant is tagged "Built-in rules" and says it is not available
#   (the start page also says "The setup assistant is not running"):
#       Ollama is not running ->  brew services restart ollama
#       model missing         ->  ollama pull qwen3:8b
#   then click "Start over" in the setup chat, or send your message again.
#   (Without it, the rules still propose a setup you can edit and approve.)
# - The first answer takes very long: the model loads into memory (up to a minute).
#   Later answers are faster.
# - The assistant replies "I could not make a valid setup from that request": the
#   change would make the design impossible to estimate (e.g. a covariate that lines
#   up with the groups, or too few samples left). Rephrase, or edit the card directly.
# - "Send" stays grey on the start page: both tables must be attached.
# - The setup card has no colour/shape options under "Edit the setup directly": the
#   code bank is older than v0.2.0 ->  git submodule update --init --recursive
# - "Download PowerPoint" fails: run  ./start.sh  again (it installs python-pptx).
# - Splicing & isoforms tab says the code bank has no modules: update (step 10).
# - Isoform analysis fails with "Package 'DEXSeq' is not installed": the extra packages in step 6.
# - "No quant.sf for sample(s) ...": the folders inside the .zip must be named exactly like the
#   samples in your sample table (e.g. WT1/quant.sf, WT2/quant.sf, ...).
# - "The transcript names do not include their gene": also upload the GTF (or a CSV with
#   transcript_id,gene_id) that your Salmon index was built from.
# - GEO import says "Could not reach GEO": check the internet connection (the site
#   downloads from ftp.ncbi.nlm.nih.gov and www.ncbi.nlm.nih.gov), then try again.
# - GEO import says no count table was found: the study only has raw FASTQ files or
#   normalised values (FPKM/TPM). Counts must be made from the FASTQ files (Ada, step 8).
# - GEO import says columns "could not be matched": the authors' table uses column names
#   that are not in GEO's sample descriptions. Download it, rename the columns to the GSM
#   IDs, and upload it with a sample table instead.
# - Compare studies says the code bank has no compare module: update the code bank (step 10).
# - A comparison warns that fold changes are negatively correlated: one study compared the
#   groups the other way round; tick "Flip direction" for it and compare again.
# - "Matching human and mouse genes needs the 'babelgene' package":
#       Rscript -e 'install.packages("babelgene")'
# - An example says "Not prepared yet": run step 7.
# - "Download PDF" says Chromium is not installed (or the first start could not install it):
#       cd ~/projects/bulk-rnaseq-copilot
#       backend/.venv/bin/python -m playwright install chromium
#   Meanwhile: "Open report", then File -> Print -> Save as PDF.
# - The report text says "Written from the results by fixed rules": the assistant was not
#   running when the report was written. Start Ollama, then click "Rewrite" on the run page.
# - No "Technical report (code bank)" link after a run: pandoc is missing ->  brew install pandoc
# - Gene names show as numbers (Entrez IDs) and pathways are skipped: the
#   annotation packages are missing -> run step 6 again and check org.Mm.eg.db / org.Hs.eg.db.
# - A run fails: the red message on the run page says why. The full logs are in
#   ~/projects/bulk-rnaseq-copilot/data/jobs/<run>/console.log


# -----------------------------------------------------------------------------
# STEP 9. Every time after that
# -----------------------------------------------------------------------------
cd ~/projects/bulk-rnaseq-copilot && ./start.sh
# Ollama keeps running in the background (brew services). If it was stopped:
#   brew services start ollama
#
# Use a different or smaller model (e.g. if the Mac is slow):
#   ollama pull qwen3:4b
#   OLLAMA_MODEL=qwen3:4b ./start.sh
#
# Run without the assistant (rules only):
#   EXECUTOR_LLM=none ./start.sh


# -----------------------------------------------------------------------------
# STEP 10. Get the latest version (after I push changes)
# -----------------------------------------------------------------------------
cd ~/projects/bulk-rnaseq-copilot
git pull
git submodule update --init --recursive
./start.sh                       # rebuilds the website automatically if it changed
# The code bank comes along automatically (it is pinned in the site; the submodule command
# above switches it to the right version). New R packages may be needed: run step 6 again.
# TROUBLESHOOTING
# - "Your local changes would be overwritten": you edited files. Keep a copy of
#   your changes, then:  git stash  &&  git pull  (and  git stash pop  to restore).


# -----------------------------------------------------------------------------
# STEP 11. Measure the assistant (send me the result)
# -----------------------------------------------------------------------------
# In a NEW Terminal tab (Cmd+T), with Ollama running:
cd ~/projects/bulk-rnaseq-copilot/backend
.venv/bin/python -m eval.run_setup_eval --rules     # built-in rules (should be 22/27)
.venv/bin/python -m eval.run_setup_eval             # qwen3:8b
# Takes a few minutes. Copy the summary lines it prints (and, if you like, the
# file it names under eval/results/) and send them to me.
# TROUBLESHOOTING
# - "Model not available: ...": Ollama is not running or the model is missing (step 4).
# - "No such file .venv/bin/python": run ./start.sh once first (step 8).


# -----------------------------------------------------------------------------
# STEP 12. Ada (the BCM cluster)
# -----------------------------------------------------------------------------
# Running the site and the FASTQ steps on Ada (containers, SLURM jobs, ssh tunnel):
#   docs/ada_setup_commands.sh   (step by step, from logging in with ssh)
#   containers/README.md         (what is inside the images)
