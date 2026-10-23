"""Tools the chat assistant can call, and the code that carries them out.

Reading tools answer from the run's files (the same files the results tabs
show). `run_python` / `run_r` run the assistant's own code in the sandbox for
anything else: custom plots, extra statistics, tables, slides. `propose_rerun`
never runs anything: it puts a suggested new run in front of the user, who
starts it with one click.
"""

import base64
import json
from pathlib import Path

from .. import results
from ..params import ADVANCED_PARAMS, AnalysisSettings, validate
from .sandbox import Workspace

TEXT_EXTENSIONS = {".csv", ".tsv", ".txt", ".json", ".md", ".yaml", ".yml", ".log", ".gmt", ".R", ".py", ".html"}
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_READ_CHARS = 30000


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description, "eager_input_streaming": True,
            "input_schema": {"type": "object", "properties": properties, "required": required,
                             "additionalProperties": False}}


TOOLS = [
    _tool("list_files",
          "List files in the workspace. run/ holds this analysis (results tables, figures, logs, provenance, "
          "dds.rds); outputs/ holds what you made in this conversation.",
          {"folder": {"type": "string", "description": "Folder to list, e.g. 'run', 'run/06_enrichment', 'outputs'. Default 'run'."}},
          []),
    _tool("read_file",
          "Read a text file from the workspace (CSV/TSV/JSON/Markdown/YAML/logs). Long files are cut; use "
          "start_line/max_lines to page, or run_python for whole-table work.",
          {"path": {"type": "string", "description": "e.g. run/run_summary.json"},
           "start_line": {"type": "integer", "description": "First line (1-based). Default 1."},
           "max_lines": {"type": "integer", "description": "Lines to return. Default 200, at most 2000."}},
          ["path"]),
    _tool("find_genes",
          "Look up genes in the differential expression results (all tested genes): fold change, adjusted "
          "p-value, mean normalised counts per group, significance, and why a value is missing. Use for any "
          "question about specific genes or top genes.",
          {"genes": {"type": "array", "items": {"type": "string"},
                     "description": "Gene symbols or IDs to look up exactly (case-insensitive)."},
           "search": {"type": "string", "description": "Text to search in symbols, IDs and gene descriptions."},
           "direction": {"type": "string", "enum": ["all", "significant", "up", "down"],
                         "description": "Filter by result. Default 'all' when genes are given, else 'significant'."},
           "sort": {"type": "string", "enum": ["padj", "log2FoldChange", "baseMean"], "description": "Default padj."},
           "order": {"type": "string", "enum": ["asc", "desc", "abs"],
                     "description": "abs = largest absolute fold change first. Default asc."},
           "limit": {"type": "integer", "description": "Rows to return, default 20, at most 200."}},
          []),
    _tool("get_pathways",
          "Pathway enrichment results from the analysis: GSEA (all genes ranked) or over-representation among "
          "significant up/down genes, per gene-set collection.",
          {"collection": {"type": "string", "description": "Collection name, e.g. H, GO:BP, REACTOME, KEGG. Default: all."},
           "analysis": {"type": "string", "enum": ["gsea", "ora_up", "ora_down"], "description": "Default gsea."},
           "search": {"type": "string", "description": "Text to look for in pathway names."},
           "limit": {"type": "integer", "description": "Rows per collection, default 15."}},
          []),
    _tool("view_image",
          "Look at an image file (a figure from the analysis, or a plot you made) to check or describe it.",
          {"path": {"type": "string", "description": "e.g. run/05_de_plots/volcano_plot.png or outputs/my_plot.png"}},
          ["path"]),
    _tool("run_python",
          "Run Python code in the workspace (working directory = the workspace). Available: pandas, numpy, scipy, "
          "matplotlib, python-pptx, python-docx, openpyxl. Read inputs from run/, save every file you make in "
          "outputs/ (it is shown to the user). Print what you need to see. Each call is a fresh process.",
          {"code": {"type": "string"}, "description": {"type": "string", "description": "One short line for the user: what this code does."}},
          ["code", "description"]),
    _tool("run_r",
          "Run R code (Rscript) in the workspace. DESeq2 and the code bank's packages are installed; the fitted "
          "model is run/04_differential_expression/dds.rds and the transformed counts run/03_exploratory/vsd.rds. "
          "Save files in outputs/. Each call is a fresh process.",
          {"code": {"type": "string"}, "description": {"type": "string", "description": "One short line for the user: what this code does."}},
          ["code", "description"]),
    _tool("share_file",
          "Show an existing file to the user in the chat (e.g. a figure from the analysis, or one of your outputs).",
          {"path": {"type": "string"}, "caption": {"type": "string"}},
          ["path"]),
    _tool("propose_rerun",
          "Suggest a NEW run of the tested pipeline with changed settings (the user starts it with one click; the "
          "current run stays as it is). Use this for changes to the tested analysis itself.",
          {"changes": {"type": "object", "description": (
              "Settings to change. Allowed keys: condition, reference_level, test_level, covariates (list), "
              "organism, padj_cutoff, log2fc_cutoff, lfc_shrinkage, min_count, run_enrichment, "
              "gene_set_collections (list), exclude_samples (list), n_label_volcano, n_heatmap_genes.")},
           "reason": {"type": "string", "description": "One sentence for the user on why."}},
          ["changes", "reason"]),
]


class ToolBox:
    """Carries out the assistant's tool calls for one conversation."""

    def __init__(self, workspace: Workspace, run_dir: Path, job: dict) -> None:
        self.ws, self.run_dir, self.job = workspace, run_dir, job

    def call(self, name: str, args: dict) -> tuple[list[dict] | str, dict]:
        """(tool_result content for the model, event for the user interface)."""
        fn = getattr(self, f"t_{name}", None)
        if fn is None:
            raise ValueError(f"Unknown tool {name}")
        return fn(**args)

    # ---- reading ---------------------------------------------------------------------------------
    def t_list_files(self, folder: str = "run"):
        base = self.ws.resolve(folder)
        if not base.is_dir():
            raise ValueError(f"'{folder}' is not a folder.")
        lines = []
        for p in sorted(base.rglob("*")):
            if p.is_file() and len(lines) < 400:
                lines.append(f"{self.ws.rel(p)}  ({_size(p.stat().st_size)})")
        return "\n".join(lines) or "(empty)", {"title": f"Listed {folder}"}

    def t_read_file(self, path: str, start_line: int = 1, max_lines: int = 200):
        p = self.ws.resolve(path)
        if not p.is_file():
            raise ValueError(f"No file '{path}'. Use list_files to see what exists.")
        if p.suffix not in TEXT_EXTENSIONS and p.suffix.lower() not in TEXT_EXTENSIONS:
            raise ValueError(f"'{path}' is not a text file; use view_image for images or run_python/run_r for binary files.")
        max_lines = max(1, min(int(max_lines), 2000))
        start = max(1, int(start_line))
        out, total = [], 0
        with open(p, errors="replace") as f:
            for i, line in enumerate(f, start=1):
                total = i
                if start <= i < start + max_lines:
                    out.append(line.rstrip("\n"))
        text = "\n".join(out)
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS] + "\n... [cut; read fewer lines]"
        head = f"{path}: lines {start}-{start + len(out) - 1} of {total}\n"
        return head + text, {"title": f"Read {path}"}

    def t_find_genes(self, genes: list[str] | None = None, search: str = "", direction: str | None = None,
                     sort: str = "padj", order: str = "asc", limit: int = 20):
        limit = max(1, min(int(limit), 200))
        q = " ".join(genes or []) or search
        direction = direction or ("all" if genes else "significant")
        table = results.de_table(self.run_dir, search=q, direction=direction, sort=sort, order=order, page_size=limit)
        p = _resolved(self.run_dir)
        ref, test = p.get("reference_level"), p.get("test_level")
        keep = ["gene_symbol", "gene_id", "gene_name", "log2FoldChange", "padj", "baseMean",
                f"mean_normalized_{ref}", f"mean_normalized_{test}", "direction", "note"]
        rows = [{k: r.get(k) for k in keep if k in r} for r in table["rows"]]
        found = {str(x).lower() for r in rows for x in (r.get("gene_symbol"), r.get("gene_id")) if x}
        missing = [g for g in (genes or []) if g.lower() not in found]
        payload = {"comparison": f"{test} vs {ref} (log2FoldChange > 0 = higher in {test}; shrunken estimates)",
                   "total_matching": table["total"], "rows": rows}
        if missing:
            payload["not_found"] = missing
            payload["not_found_note"] = ("Not among the tested genes: the name may differ in this dataset (gene ID type), "
                                         "or the gene had too few reads and was removed by the low-count filter.")
        return json.dumps(payload, default=str), {"title": "Looked up " + (", ".join(genes) if genes else (search or "top genes"))}

    def t_get_pathways(self, collection: str | None = None, analysis: str = "gsea", search: str = "", limit: int = 15):
        ov = results.overview(self.run_dir)
        en = ov["enrichment"]
        if not en or en.get("status") != "done":
            return f"Pathway analysis was not run: {(en or {}).get('reason', 'no enrichment results')}", {"title": "Pathways"}
        out = {}
        for name, c in (en.get("collections") or {}).items():
            if collection and name.lower() != collection.lower():
                continue
            try:
                rows = results.enrichment_table(self.run_dir, c["folder"], analysis, limit=5000)["rows"]
            except KeyError:
                out[name] = "not available (for over-representation: too few significant genes)"
                continue
            if search:
                rows = [r for r in rows if search.lower().replace(" ", "_") in r["pathway"].lower()]
            out[name] = rows[: max(1, min(int(limit), 100))]
        return json.dumps({"analysis": analysis, "padj_cutoff": en["methods"]["padj_cutoff"], "results": out},
                          default=str), {"title": f"Pathways ({analysis})"}

    def t_view_image(self, path: str):
        p = self.ws.resolve(path)
        mt = IMAGE_TYPES.get(p.suffix.lower())
        if not p.is_file() or not mt:
            raise ValueError(f"'{path}' is not an image file in the workspace.")
        data = base64.standard_b64encode(p.read_bytes()).decode()
        return ([{"type": "image", "source": {"type": "base64", "media_type": mt, "data": data}},
                 {"type": "text", "text": f"Image {path}"}], {"title": f"Looked at {Path(path).name}"})

    def t_share_file(self, path: str, caption: str = ""):
        p = self.ws.resolve(path)
        if not p.is_file():
            raise ValueError(f"No file '{path}'.")
        return f"Shown to the user: {path}", {"title": caption or f"Shared {p.name}", "files": [self.ws.rel(p)]}

    # ---- running code ------------------------------------------------------------------------------
    def _run(self, language: str, code: str, description: str):
        r = self.ws.run(language, code)
        parts = [f"exit: {'ok' if r.ok else 'error'} ({r.seconds} s)"]
        if r.stdout:
            parts.append("stdout:\n" + r.stdout)
        if r.stderr:
            parts.append("stderr:\n" + r.stderr)
        if r.new_files:
            parts.append("new files (shown to the user): " + ", ".join(r.new_files))
        content: list[dict] = [{"type": "text", "text": "\n".join(parts)}]
        # let the model see the plots it just made, so it can check them
        for f in r.new_files[:3]:
            mt = IMAGE_TYPES.get(Path(f).suffix.lower())
            fp = self.ws.resolve(f)
            if mt and fp.stat().st_size < 4_000_000:
                content.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                            "data": base64.standard_b64encode(fp.read_bytes()).decode()}})
        event = {"title": description, "language": language, "code": code, "ok": r.ok, "script": r.script,
                 "output": (r.stdout + ("\n" + r.stderr if r.stderr else "")).strip()[-4000:], "files": r.new_files}
        return content, event

    def t_run_python(self, code: str, description: str):
        return self._run("python", code, description)

    def t_run_r(self, code: str, description: str):
        return self._run("r", code, description)

    # ---- reruns ------------------------------------------------------------------------------------
    def t_propose_rerun(self, changes: dict, reason: str):
        base = dict(self.job["settings"])
        adv = dict(base.get("advanced") or {})
        top_keys = {"condition", "reference_level", "test_level", "covariates", "organism"}
        for k, v in changes.items():
            if k in top_keys:
                base[k] = v
            elif k in ADVANCED_PARAMS:
                adv[k] = v
            else:
                raise ValueError(f"'{k}' cannot be changed in a rerun. Allowed: {', '.join(sorted(top_keys | set(ADVANCED_PARAMS)))}.")
        base["advanced"] = adv
        base["run_name"] = (base.get("run_name") or "analysis")[:30] + "_rerun"
        s, problems = validate(AnalysisSettings(**base))
        if problems:
            raise ValueError("These settings are not valid: " + " ".join(problems))
        event = {"title": "Suggested a new run", "rerun": {"settings": s.model_dump(), "changes": changes,
                                                          "reason": reason, "upload_id": self.job["upload_id"]}}
        return ("The proposal is shown to the user with a button to start it. It has not started; do not "
                "describe its results until the user runs it."), event


def _resolved(run_dir: Path) -> dict:
    return results.overview(run_dir)["resolved_params"] or {}


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.0f} TB"
