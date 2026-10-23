# RNA-seq analysis site 

A no-code website where a wet-lab biologist uploads a raw counts table and a sample table and gets a standard bulk RNA-seq analysis: input checks, QC, PCA, DESeq2 differential expression, volcano/MA plots, heatmap, pathway enrichment and a report with a methods paragraph.

All analysis is done by the tested R scripts in the [rnaseq-codebank](https://github.com/N3haRao/rnaseq-codebank), included here as the `codebank/` git submodule. The website never writes analysis code; it fills in the code bank's documented parameters and runs it. See [PLAN.md](PLAN.md) for the full plan (agents come in Phases 2-4).

**Status: Phase 4** (chat assistant). Phase 4 adds the **Ask (chat)** tab: Claude, through the Anthropic API, answers questions about a run, writes and runs its own Python or R code for custom plots, tables, statistics and slides, and proposes reruns of the tested pipeline. Phase 3 added a downloadable report (HTML and PDF) whose narrative is written by the same local model and checked sentence by sentence against the results. Before that, Phase 2 added the executor agent. Phase 1 gave the website: upload or example, automatic file check, guided setup, background runs with live progress, results tabs and downloads. Phase 2 adds an open-weight assistant (Qwen3-8B through Ollama, on the same laptop) that reads the sample table and proposes the setup, explains file problems with how to fix them, and explains run warnings. Without the assistant everything still works with built-in rules. Trial use with public data only.

## Setting up on a laptop (macOS or Linux)

You need R 4.3 or newer, Python 3.11 or newer, Node.js 20 or newer and git.

```bash
git clone --recurse-submodules https://github.com/N3haRao/bulk-rnaseq-copilot.git
cd bulk-rnaseq-copilot

# 1. R packages for the code bank (once, takes a while) and the example datasets
cd codebank
Rscript scripts/00_setup_environment.R
Rscript scripts/make_example_data.R          # airway, pasilla, gse60450 (downloaded from GEO)
cd ..

# 2. Start the site (builds the frontend and Python environment the first time)
./start.sh
```

Then open <http://localhost:8000>. Step-by-step Mac commands with troubleshooting: [docs/mac_setup_commands.sh](docs/mac_setup_commands.sh).

### The assistant (optional, recommended)

The assistant runs locally with [Ollama](https://ollama.com); nothing is sent to the internet.

```bash
brew install ollama          # or download the app from ollama.com
ollama serve                 # skip if the Ollama app is already running
ollama pull qwen3:8b         # about 5 GB, once
```

Restart `./start.sh`. Suggestions marked **Assistant · qwen3:8b** come from the model, and suggestions marked **Built-in rules** come from the rules (when Ollama is not running, the model is missing, or its answer failed the checks). The site works the same without it.

Settings: `EXECUTOR_LLM` (`ollama` or `none`), `OLLAMA_MODEL` (default `qwen3:8b`), `OLLAMA_URL` (default `http://localhost:11434`), `OLLAMA_TIMEOUT` seconds (default 180).

### How the assistant is kept safe and traceable

- It only answers questions with a fixed JSON shape. The shape is built from your data, so it can only name columns and groups that exist. It never runs code or commands.
- Code checks every proposal (columns exist, groups are different, covariates are not confounded with the comparison, the design can be estimated). A failed proposal is retried once with the problems listed, then replaced by the rule-based one.
- Explanations that contain numbers not present in the data it was given are not shown.
- The user confirms the setup before anything runs; each run records whether the setup came from the assistant or the rules and whether the user changed it (`setup_origin` in `job.json`).
- Every assistant call (prompt, answer, checks, outcome) is saved under `data/uploads/<id>/agent/log/` or `data/jobs/<id>/agent/log/`.
- The prompts are plain text in `backend/app/agents/prompts/` and can be edited without changing code.

### The report (Phase 3)

When a run finishes, the site writes a report automatically: open it, or download it as PDF or HTML from the run page. It contains a plain-language summary, key findings, things to check, sample structure and QC, differential expression (volcano, MA, heatmap, top-20 tables), pathways, suggested next steps, methods, settings and software versions.

- Only the narrative text comes from the model. Figures, tables, numbers, methods and settings are read from the analysis files.
- Every sentence the model writes is checked. It is removed if it contains a number not in the results, names a gene or pathway the model was not shown, or looks like a gene symbol that is not in the data. A section that loses most of its sentences is replaced by a template written from the same facts. Every section in the report says whether the assistant or the template wrote it, and the end of the report says how many sentences were removed.
- Style examples are in `backend/app/agents/report_examples/` (two illustrative examples). Add your lab's own examples there: same keys, one JSON file each.
- PDF export uses headless Chromium (installed by `start.sh` on first start). Without it, open the HTML report and use Print → Save as PDF.

### The chat (Phase 4)

On a run's page, open **Ask (chat)**. Examples: "Is Krt5 lower in luminal cells?", "Make a volcano plot with only these 10 genes labelled", "Which pathways come up in the downregulated genes?", "What settings did you use?", "Make 5 slides for lab meeting", "Rerun with a stricter cutoff".

Setup: create an API key at https://console.anthropic.com, then in this folder:

```bash
cp .env.example .env        # then put your key in .env: ANTHROPIC_API_KEY=sk-ant-...
./start.sh
```

How it works:
- **Model:** Claude Opus 5.5 (`CHAT_MODEL` in `.env` to change), streamed, with prompt caching and the API's automatic fallback if a request is declined.
- **Grounded answers:** it answers from the run's files through tools (`find_genes`, `get_pathways`, `read_file`, `list_files`, `view_image`). The system prompt tells it to look numbers up before stating them, to mark anything it computes itself as a custom analysis, and to keep literature knowledge separate from the data.
- **Its own code** (`run_python`, `run_r`) runs in a per-conversation workspace: a copy of the run folder (the original is never touched), an `outputs/` folder shown in the chat (images inline, other files as downloads), and every script kept in `cells/`. The code runs with no API keys or tokens in its environment, a time limit (`CHAT_CODE_TIMEOUT`, default 300 s) and file-size limits. This is a boundary for a single-user laptop; on the BCM server it should run inside a container without network access.
- **Reruns:** `propose_rerun` shows a card with the changed settings, checked against the code bank's manifest. The user starts the new run with one click, and the current run stays.
- **Privacy:** questions, results and tool outputs are sent to the Anthropic API. Use public data during the trial.
- Each conversation is saved under `data/jobs/<run>/chat/<conversation>/`: the full API messages, the transcript the user sees, and the workspace.

### Measuring the assistant

`backend/eval/setup_cases.yaml` holds 27 sample tables with the setup a bioinformatician would choose (paired designs, confounded batches, controls named shNT, DMSO, sham, pre, normoxia...). Score a model against it:

```bash
cd backend
.venv/bin/python -m eval.run_setup_eval --rules            # built-in rules: 22/27 (81%)
.venv/bin/python -m eval.run_setup_eval                    # qwen3:8b through Ollama
.venv/bin/python -m eval.run_setup_eval --model qwen3:14b  # any other Ollama model
```

The plan's target is 95% fully correct. Add a case whenever the site gets a setup wrong during the trial.

If `Rscript` is not on your PATH (e.g. R installed from the CRAN .pkg on macOS is usually fine, but a custom install may not be), start with `RSCRIPT=/path/to/Rscript ./start.sh`.

## Using it

1. **Upload** a counts table and a sample table, or pick an example dataset. Templates are linked on the start page.
2. **Check & set up.** The files are checked straight away and problems are explained in plain language (sample names that do not match, TPM instead of raw counts, ...). The comparison is pre-filled with a suggestion (which column holds the groups, which group is the control, what to account for), with the reasons shown. Change anything with the dropdowns.
3. **Run.** The analysis runs in the background with per-step progress. It takes about a minute for a typical dataset.
4. **Results.** Tabs for the summary and warnings, quality control, sample PCA and clustering, the searchable gene table with volcano and MA plots, the heatmap, pathways, and methods with every setting and software version. Download the HTML report (print to PDF from the browser), the gene tables, the methods text, or everything as a zip.

Each analysis is kept under `data/jobs/` with the code bank's complete run folder (inputs, parameters, logs, provenance). Uploads are under `data/uploads/`. Both are ignored by git.

## Development

```bash
# backend with auto-reload (port 8000)
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/uvicorn app.main:app --reload

# frontend with hot reload (port 5173, forwards /api to the backend)
cd frontend && npm install && npm run dev

# tests (the API tests run the real pipeline and are skipped when R/DESeq2 are missing)
cd backend && .venv/bin/pytest
cd frontend && npm run typecheck
```

Settings (environment variables): `CODEBANK_DIR` (default `./codebank`), `OMICS_DATA_DIR` (default `./data`), `RSCRIPT`, `MAX_UPLOAD_MB` (default 300).

## Layout

```
backend/app/
  main.py             HTTP API (FastAPI); also serves the built frontend
  codebank.py         reads the code bank's manifest, examples and defaults; builds run_pipeline.R commands
  params.py           checks every setting against the manifest's types and allowed values before anything runs
  setup_proposal.py   rule-based suggestion of comparison, control group and covariates (the assistant's fallback)
  agents/executor.py  executor agent: setup proposal, check explanations, run-warning explanations
  agents/reporter.py  report agent: narrative sections of the report, with a template fallback
  agents/grounding.py checks that model-written text only states numbers, genes and pathways from the results
  agents/report_examples/  style examples for the report agent
  report.py           the report: self-contained HTML (figures embedded) and PDF
  chat/               the chat assistant: agent loop, tools, sandbox, system prompt, Claude client
  agents/prompts/     the prompts, as editable text
  llm/                model clients: Ollama (open weights), scripted stand-in for tests
  uploads.py          stores uploads; runs step 01 (input check) on demand
  jobs.py             runs the full pipeline in the background, one at a time; progress from the run log
  results.py          reads a finished run folder for the results tabs
backend/eval/         benchmark of sample tables with expected setups, and the scoring script
frontend/src/
  pages/              Home (upload / examples), Dataset (check & set up), Run (progress & results), Runs
  components/         shared UI, gene table, pathways
codebank/             git submodule: rnaseq-codebank
```

## Updating the code bank

The submodule is pinned to one code-bank commit, which every run records in its provenance. After a new code-bank release:

```bash
cd codebank && git fetch --tags && git checkout v0.1.1 && cd ..
git add codebank && git commit -m "Use code bank v0.1.1"
```
