# Agentic RNA-seq analysis: build plan

A no-code website where a wet-lab biologist uploads a counts table and a sample table, and agents run a standard bulk RNA-seq analysis from the lab code bank ([N3haRao/rnaseq-codebank](https://github.com/N3haRao/rnaseq-codebank)), write a report, and answer questions about the results.

Trial scope: public data only, runs locally on a laptop (16 GB unified memory, 512 GB SSD). BCM hosting, logins and private data come later.

## Decisions

| Topic | Decision |
| --- | --- |
| Analysis code | R code bank, pinned to a git tag. The executor agent only calls its entry points. |
| Executor and report agents | Open-weight model through Ollama. Default: Qwen3-8B (4-bit, about 5 GB in memory); one model loaded and shared by both agents. |
| Interpreter (chat) agent | Claude through the Anthropic API. May write its own R/Python code, run in a local sandbox (see below). |
| Backend | Python, FastAPI. One analysis at a time on the laptop; server-sent events for progress and chat streaming. |
| Frontend | React + TypeScript (Vite), so it can be hosted on BCM later without changes. |
| Report | HTML first, PDF printed from the same HTML. |
| Upload | Browser upload for now; Globus later (separate work). |

Why Qwen3-8B: with 16 GB shared between the model, R (2-4 GB during DESeq2 and GSEA), the browser and the OS, a ~5 GB model is the largest that fits comfortably. It handles tool calling and JSON output well. Larger models (14B, gpt-oss-20b) would push the laptop into swap during runs. The model name is a setting, so it can be swapped on BCM hardware.

## Architecture

```
React frontend
  Upload | Check | Set up | Results tabs | Chat | Download report
        |  REST + server-sent events
FastAPI backend
  run_manager      creates runs, calls Rscript run_pipeline.R, tracks status
  codebank         reads manifest/tools.yaml, turns it into tool schemas and validators
  agents/
    executor       Ollama (open weights); fixed workflow, LLM only for judgment steps
    reporter       Ollama (open weights); writes narrative from the run's JSON summaries
    interpreter    Anthropic API; tools over run files + sandboxed code
  sandbox          runs interpreter-written scripts with limits, saves them with outputs
codebank/          git submodule of rnaseq-codebank at a fixed tag
runs/              one folder per run (written by the code bank)
```

Rule shared by all agents: numbers shown to the user come from files in the run folder, never from the model's memory. The executor and reporter never get a shell; they can only call Python functions whose arguments are checked against the manifest's types and allowed values before anything runs.

## Agent 1: Executor (open weights)

A fixed workflow written in Python. The LLM fills in only the steps that need judgment, and every step has a non-LLM fallback.

| Step | Done by |
| --- | --- |
| Save uploads, run `run_pipeline.R --steps 1` | Code |
| Explain `validation_report.json` problems in plain language, with how to fix them | LLM (fallback: the code bank's own messages) |
| Propose condition column, control group, test group, covariates (pairing, batch, sex) from the metadata | LLM, JSON output constrained by a schema |
| Check the proposal: columns exist, levels exist, design estimable | Code (step 01 also checks) |
| Show the proposal as pre-filled dropdowns; user confirms or changes | UI |
| Run steps 2-7 with the confirmed settings, stream progress | Code |
| Turn `run_summary.json` warnings into a short, plain-language list | LLM (fallback: raw warnings) |

If the model's JSON fails validation twice, the dropdowns open with defaults chosen by code. The user can always finish an analysis even if the model misbehaves.

## Agent 2: Report writer (open weights)

- Input: `run_summary.json`, `de_summary.json`, `qc_summary.json`, `exploratory_summary.json`, `enrichment_summary.json`, `07_report/methods.md`.
- Output: a plain-language summary, a "things to check" list, and figure captions, inserted into the code bank's HTML report.
- Guided by 2-3 hand-written example reports (few-shot).
- Number check before accepting the text: every number in it must appear in a source file, otherwise it is regenerated (then falls back to the template-only report).
- PDF: the same HTML printed with headless Chromium.

## Agent 3: Interpreter (Claude API)

Tools, in the order the agent should prefer them:

1. Read tools built from the manifest's `answers:` map: `get_run_summary`, `lookup_gene`, `query_de_table` (filter, sort, top N), `get_pathways`, `get_settings`, `explain_method` (from `docs/METHODS.md`).
2. `run_code(language, code)`: for anything the read tools cannot do, for example "a volcano plot with only these 10 genes labelled".
3. `propose_rerun(changes)`: shows a confirmation card; on confirm the executor starts a new run with `--based_on`.

Sandbox for `run_code`:
- Runs locally (the data never leaves the laptop; this matters once private data is allowed).
- Working folder `runs/<run>/chat/<n>/`; the run's results are read-only.
- No network, time limit (2 min), memory limit; on BCM this becomes a container.
- Every script is saved next to its output and listed in the run's provenance.
- Results made this way are labelled in the UI and report as "custom analysis, not from the tested code bank".

## Trial datasets

| Dataset | Organism | Design | Why |
| --- | --- | --- | --- |
| airway (Himes et al. 2014) | Human | Dexamethasone vs control, 4 paired donors | Built-in example. Known glucocorticoid-response genes (FKBP5, PER1, ZBTB16, DUSP1) to check against. |
| GSE60450 (Fu et al. 2015) | Mouse | Mammary basal vs luminal cells (virgin mice) | Downloaded as-is from GEO (Entrez IDs, like most public data a biologist will find). Used in the Bioconductor RNAseq123 workflow, so expected results are published. |
| Broken copies of both | - | - | Planted problems: mismatched sample names, TPM instead of counts, one group with 2 replicates, group confounded with batch, swapped labels. Tests that the site catches and explains each one. |

## Phases

| Phase | Deliverable | Done when |
| --- | --- | --- |
| 0. Code bank | `07_report.R` (pushed), Entrez ID support, annotation-column handling, GSE60450 example (patch delivered), then tag `v0.1.1` | `tests/run_tests.R` passes on airway, pasilla and GSE60450 |
| 1. Backend + React UI, no LLM (built) | Upload, checks, dropdown setup with rule-based suggestions, background runs with progress, results tabs, downloads | GSE60450 runs end to end in the browser; backend tests pass |
| 2. Executor agent (built) | LLM-proposed setup with code checks, retry and rule fallback; plain-language check and warning explanations; 27-case benchmark (rules alone: 81%) | 95% correct setups on the benchmark with qwen3:8b (to be measured on the laptop) |
| 3. Report writer (built) | Narrative HTML/PDF report: model-written sections checked sentence by sentence (numbers, genes, pathways), template fallback per section, style examples | Number and name check passes on every report (enforced in code); biologist rates it clear (trial) |
| 4. Interpreter (built) | Chat with Claude (API): grounded answers through tools, its own Python/R code in a sandboxed workspace (plots, tables, statistics, slides), rerun proposals, saved conversations | 90% correct on ~50 questions with known answers (to be measured with the API key) |
| 5. Trial | 3-5 wet-lab biologists, public data, no help | Completion rate, errors, feedback |

Phase 1 comes before any LLM work so there is a working tool early and the evaluation harness exists before the agents do.

## Repository layout (this repo)

```
backend/            FastAPI app, agents, sandbox, run manager
frontend/           React + TypeScript (Vite)
codebank/           submodule: rnaseq-codebank at a tag
eval/               benchmark datasets, question set with answers, scoring scripts
docs/               this plan, setup notes
```
