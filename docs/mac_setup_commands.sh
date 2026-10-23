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
# TROUBLESHOOTING
# - If R asks "Update all/some/none? [a/s/n]" or to pick a mirror: type  n  (or 1) and press Enter.
# - A package fails to compile ("clang: error", "gfortran not found"):
#     1. make sure step 1 finished:  xcode-select --install
#     2. make sure you use the CRAN R from step 3:  which Rscript
#        should show /usr/local/bin/Rscript or /Library/Frameworks/...
#     3. run the setup script again; it only installs what is missing.
# - "MSigDB" / msigdbr download fails: check your internet and run the setup again.
#   Without it, everything works except pathway analysis.


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
# All lines should say [PASS]. If only "enrichment" fails, see the msigdbr note in step 6,
# or run without it:  Rscript tests/run_tests.R --skip_enrichment


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
# Try it: "Or try an example" -> gse60450 -> "Use this dataset" -> "Run analysis".
# On your own upload, the suggested setup should be tagged "Assistant · qwen3:8b".
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
# - The suggestion says "Built-in rules" and "Assistant not used: ...":
#       Ollama is not running ->  brew services restart ollama
#       model missing         ->  ollama pull qwen3:8b
#   then reload the page. (The site still works with the rules.)
# - The suggestion takes very long the first time: the model loads into memory
#   (up to a minute). Later answers are faster.
# - An example says "Not prepared yet": run step 7.
# - No "Report (HTML)" button after a run: pandoc is missing ->  brew install pandoc
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
# STEP 10. Get the latest version (after changes are pushed)
# -----------------------------------------------------------------------------
cd ~/projects/bulk-rnaseq-copilot
git pull
git submodule update --init --recursive
./start.sh                       # rebuilds the website automatically if it changed
# TROUBLESHOOTING
# - "Your local changes would be overwritten": you edited files. Keep a copy of
#   your changes, then:  git stash  &&  git pull  (and  git stash pop  to restore).


# -----------------------------------------------------------------------------
# STEP 11. Measure the assistant
# -----------------------------------------------------------------------------
# In a NEW Terminal tab (Cmd+T), with Ollama running:
cd ~/projects/bulk-rnaseq-copilot/backend
.venv/bin/python -m eval.run_setup_eval --rules     # built-in rules (should be 22/27)
.venv/bin/python -m eval.run_setup_eval             # qwen3:8b
# TROUBLESHOOTING
# - "Model not available: ...": Ollama is not running or the model is missing (step 4).
# - "No such file .venv/bin/python": run ./start.sh once first (step 8).
