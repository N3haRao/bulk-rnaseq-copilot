"""HTTP API for the RNA-seq analysis site (Phase 1: no LLM yet).

Flow: upload (or example) -> check (step 01) -> setup -> run (steps 1-7) -> results.
"""

import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from fastapi.staticfiles import StaticFiles

from . import code_bundle, codebank, comparisons, extras, geo, report, results, secrets_store, uploads
from .config import settings
from .jobs import jobs
from .chat import model as chat_model
from .agents.setup_chat import SetupChat
from .chat.agent import Conversation
from .llm import executor_llm
from .params import ADVANCED_PARAMS, AnalysisSettings, manifest_params, validate


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.uploads_dir.mkdir(parents=True, exist_ok=True)
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    jobs.recover()
    extras.extras.recover(settings.jobs_dir)
    comparisons.base_dir().mkdir(parents=True, exist_ok=True)
    comparisons.comparisons.recover()
    yield


app = FastAPI(title="RNA-seq analysis", version="0.1.0", lifespan=lifespan)


def _not_found(what: str):
    raise HTTPException(status_code=404, detail=f"{what} not found.")


# ---- System ---------------------------------------------------------------------------
@app.get("/api/health")
def health() -> dict:
    r_version = None
    try:
        out = subprocess.run([settings.rscript, "--version"], capture_output=True, text=True, timeout=20)
        r_version = (out.stdout or out.stderr).strip().splitlines()[0] if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass
    manifest_ok = codebank.path("manifest", "tools.yaml").exists()
    return {
        "ok": bool(r_version) and manifest_ok,
        "r": r_version, "rscript": settings.rscript,
        "pandoc": shutil.which("pandoc") is not None,
        "codebank": codebank.version_info() if manifest_ok else None,
        "codebank_dir": str(settings.codebank_dir),
    }


@app.get("/api/options")
def options() -> dict:
    """Allowed values for settings, from the code bank manifest."""
    specs = manifest_params()
    pick = ("organism", *ADVANCED_PARAMS)
    defaults = codebank.defaults()
    return {"parameters": {k: {**specs[k], "default": defaults.get(k, specs[k].get("default"))}
                           for k in pick if k in specs},
            "steps": codebank.steps()}


# ---- Examples and templates ---------------------------------------------------------------
@app.get("/api/examples")
def list_examples() -> list[dict]:
    return [{k: v for k, v in e.items() if k not in ("counts_file", "metadata_file")}
            for e in codebank.examples()]


@app.post("/api/examples/{name}")
def use_example(name: str) -> dict:
    try:
        rec = uploads.create_from_example(name)
    except KeyError:
        _not_found("Example")
    except uploads.UploadError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return uploads.describe(rec["id"])


@app.get("/api/templates/{kind}")
def template(kind: str):
    path = codebank.templates().get(kind)
    if not path or not path.exists():
        _not_found("Template")
    return FileResponse(path, filename=f"{kind}_template.csv", media_type="text/csv")


# ---- Import from GEO ------------------------------------------------------------------------
class GeoRequest(BaseModel):
    accession: str = Field(min_length=4, max_length=20)
    counts_option: str | None = None


@app.post("/api/geo/inspect")
def geo_inspect(body: GeoRequest) -> dict:
    """What GEO has for this series: title, summary, samples and the count tables on offer."""
    try:
        return geo.inspect(body.accession)
    except geo.GeoError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/geo/import")
def geo_import(body: GeoRequest) -> dict:
    """Download the chosen count table, build the sample table from GEO, and create an upload."""
    if not body.counts_option:
        raise HTTPException(status_code=422, detail="Choose a count table (counts_option).")
    try:
        out = geo.import_dataset(body.accession, body.counts_option)
    except geo.GeoError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except uploads.UploadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {**uploads.describe(out["upload_id"]), "geo": out["geo"], "description": out["description"]}


# ---- Uploads ------------------------------------------------------------------------------
@app.post("/api/uploads")
def create_upload(counts: UploadFile = File(...), metadata: UploadFile = File(...)) -> dict:
    try:
        rec = uploads.create((counts.filename or "counts.csv", counts.file),
                             (metadata.filename or "metadata.csv", metadata.file))
    except uploads.UploadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return uploads.describe(rec["id"])


@app.get("/api/uploads/{upload_id}")
def get_upload(upload_id: str, condition: str | None = None) -> dict:
    try:
        return uploads.describe(upload_id, condition=condition)
    except KeyError:
        _not_found("Upload")
    except uploads.UploadError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/uploads/{upload_id}/check")
def check_upload(upload_id: str, body: AnalysisSettings) -> dict:
    s, problems = validate(body)
    if problems:
        raise HTTPException(status_code=422, detail=" ".join(problems))
    try:
        return uploads.check(upload_id, s)
    except KeyError:
        _not_found("Upload")
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---- Executor agent ---------------------------------------------------------------------------
@app.get("/api/agent/status")
def agent_status() -> dict:
    return executor_llm().status()


@app.post("/api/uploads/{upload_id}/agent/setup")
def agent_setup(upload_id: str, condition: str | None = None, refresh: bool = False) -> dict:
    try:
        return uploads.agent_setup(upload_id, condition, refresh=refresh)
    except KeyError:
        _not_found("Upload")
    except uploads.UploadError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/uploads/{upload_id}/agent/explain-check")
def agent_explain_check(upload_id: str) -> dict:
    try:
        return uploads.agent_explain_check(upload_id)
    except KeyError:
        _not_found("Input check")


# ---- Setup chat (open-weight model) -------------------------------------------------------------
def _setup_chat(upload_id: str) -> SetupChat:
    try:
        return SetupChat(uploads.upload_dir(upload_id), uploads.record(upload_id))
    except KeyError:
        _not_found("Upload")


class SetupMessage(BaseModel):
    text: str = Field(default="", max_length=8000)
    current: dict | None = None  # the setup card as the user left it


@app.get("/api/uploads/{upload_id}/setup-chat")
def get_setup_chat(upload_id: str) -> dict:
    return _setup_chat(upload_id).state()


@app.post("/api/uploads/{upload_id}/setup-chat")
def send_setup_chat(upload_id: str, body: SetupMessage) -> dict:
    """The user's message; returns the conversation with the assistant's reply and the new setup."""
    return _setup_chat(upload_id).send(body.text, body.current)


@app.delete("/api/uploads/{upload_id}/setup-chat")
def reset_setup_chat(upload_id: str) -> dict:
    chat = _setup_chat(upload_id)
    chat.path.unlink(missing_ok=True)
    return chat.state()


@app.post("/api/runs/{job_id}/agent/warnings")
def agent_run_warnings(job_id: str) -> dict:
    _job_or_404(job_id)
    try:
        return jobs.agent_warnings(job_id)
    except KeyError:
        raise HTTPException(status_code=409, detail="This run has no results yet.")


# ---- Runs ---------------------------------------------------------------------------------
def _job_or_404(job_id: str) -> dict:
    try:
        return jobs.load(job_id)
    except (KeyError, FileNotFoundError):
        _not_found("Run")


def _run_dir_or_404(job_id: str) -> Path:
    _job_or_404(job_id)
    d = jobs.run_dir(job_id)
    if not d or not d.exists():
        raise HTTPException(status_code=409, detail="This run has no results yet.")
    return d


class RunRequest(AnalysisSettings):
    upload_id: str
    setup_origin: dict | None = None


@app.post("/api/runs")
def start_run(body: RunRequest) -> dict:
    s, problems = validate(AnalysisSettings(**body.model_dump(exclude={"upload_id", "setup_origin"})))
    if problems:
        raise HTTPException(status_code=422, detail=" ".join(problems))
    try:
        uploads.record(body.upload_id)
    except KeyError:
        _not_found("Upload")
    try:
        return jobs.start(body.upload_id, s, setup_origin=body.setup_origin)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/runs")
def list_runs() -> list[dict]:
    return [{k: j.get(k) for k in ("id", "status", "created", "finished", "dataset", "settings", "error")}
            for j in jobs.all_jobs()]


@app.get("/api/runs/{job_id}")
def get_run(job_id: str) -> dict:
    job = _job_or_404(job_id)
    job = {k: v for k, v in job.items() if k != "command"}
    job["steps"] = jobs.progress(job_id)
    return job


@app.post("/api/runs/{job_id}/cancel")
def cancel_run(job_id: str) -> dict:
    _job_or_404(job_id)
    return jobs.cancel(job_id)


@app.get("/api/runs/{job_id}/results")
def run_results(job_id: str) -> dict:
    return results.overview(_run_dir_or_404(job_id))


@app.get("/api/runs/{job_id}/de")
def run_de(job_id: str, search: str = "", direction: str = "all", sort: str = "padj", order: str = "asc",
           page: int = 1, page_size: int = 50) -> dict:
    try:
        return results.de_table(_run_dir_or_404(job_id), search=search, direction=direction, sort=sort,
                                order=order, page=page, page_size=page_size)
    except KeyError:
        _not_found("Differential expression table")


@app.get("/api/runs/{job_id}/enrichment/{collection}/{kind}")
def run_enrichment(job_id: str, collection: str, kind: str) -> dict:
    try:
        return results.enrichment_table(_run_dir_or_404(job_id), collection, kind)
    except KeyError:
        _not_found("Pathway table")


@app.get("/api/runs/{job_id}/files/{rel:path}")
def run_file(job_id: str, rel: str, download: bool = False):
    try:
        p = results.safe_path(_run_dir_or_404(job_id), rel)
    except KeyError:
        _not_found("File")
    return FileResponse(p, filename=p.name if download else None,
                        content_disposition_type="attachment" if download else "inline")


# ---- Report (report agent) ---------------------------------------------------------------
@app.get("/api/runs/{job_id}/report")
def report_status(job_id: str) -> dict:
    _run_dir_or_404(job_id)
    return report.status(jobs.job_dir(job_id))


@app.post("/api/runs/{job_id}/report")
def report_build(job_id: str, refresh: bool = False) -> dict:
    """Write (or rewrite, with refresh=true) the narrative and the HTML report. Takes up to a minute with the model."""
    run_dir = _run_dir_or_404(job_id)
    return report.build(jobs.load(job_id), jobs.job_dir(job_id), run_dir, refresh=refresh)


@app.get("/api/runs/{job_id}/report/html")
def report_html(job_id: str, download: bool = False):
    _run_dir_or_404(job_id)
    p = jobs.job_dir(job_id) / "report" / "report.html"
    if not p.exists():
        raise HTTPException(status_code=409, detail="Create the report first.")
    return FileResponse(p, media_type="text/html", filename=f"report_{job_id}.html" if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/runs/{job_id}/report/pdf")
def report_pdf(job_id: str):
    _run_dir_or_404(job_id)
    try:
        p = report.pdf(jobs.job_dir(job_id))
    except report.ReportError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return FileResponse(p, media_type="application/pdf", filename=f"report_{job_id}.pdf")


@app.get("/api/runs/{job_id}/report/pptx")
def report_pptx(job_id: str):
    run_dir = _run_dir_or_404(job_id)
    p = report.pptx(jobs.load(job_id), jobs.job_dir(job_id), run_dir)
    return FileResponse(p, filename=f"report_{job_id}.pptx",
                        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


# ---- Settings (API key) -----------------------------------------------------------------------
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def _require_local_same_origin(request: Request) -> None:
    """Only this computer, and only this site's own pages, may change the key.

    Blocks requests from other machines and from other websites open in the
    same browser (which could otherwise send requests to localhost).
    """
    host = request.client.host if request.client else ""
    if host not in LOCAL_HOSTS:
        raise HTTPException(status_code=403, detail="The API key can only be changed on the computer running the site.")
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="The API key can only be changed from this site's own Settings page.")
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        raise HTTPException(status_code=403, detail="The API key can only be changed from this site's own Settings page.")


def _key_status() -> dict:
    st = chat_model.configured()
    return {"configured": st["configured"], "source": st.get("source"), "masked": st.get("masked"),
            "model": st["model"], "can_change": st.get("source") != "environment",
            "storage": {"keychain": "your computer's keychain", "file": "a private file in the data folder",
                        "environment": "the ANTHROPIC_API_KEY setting (.env or environment)"}.get(st.get("source") or "")}


class ApiKey(BaseModel):
    key: str = Field(min_length=1, max_length=400)


@app.get("/api/settings")
def get_settings() -> dict:
    return {"anthropic": _key_status()}


@app.put("/api/settings/anthropic-key")
def set_anthropic_key(body: ApiKey, request: Request) -> dict:
    _require_local_same_origin(request)
    key = body.key.strip()
    if secrets_store.get_key()[1] == "environment":
        raise HTTPException(status_code=409, detail="A key is set in .env / the environment and takes precedence. "
                                                    "Remove ANTHROPIC_API_KEY there to manage the key here.")
    if not secrets_store.looks_like_key(key):
        raise HTTPException(status_code=422, detail="That does not look like an Anthropic API key (it starts with sk-ant-).")
    ok, message = chat_model.verify_key(key)
    if ok is False:
        raise HTTPException(status_code=422, detail=message)
    secrets_store.save_key(key)
    return {"anthropic": _key_status(), "message": message}


@app.delete("/api/settings/anthropic-key")
def delete_anthropic_key(request: Request) -> dict:
    _require_local_same_origin(request)
    if secrets_store.get_key()[1] == "environment":
        raise HTTPException(status_code=409, detail="This key comes from .env / the environment; remove it there.")
    secrets_store.delete_key()
    return {"anthropic": _key_status()}


# ---- Chat (interpreter agent, Claude API) -------------------------------------------------------
@app.get("/api/chat/status")
def chat_status() -> dict:
    return chat_model.configured()


def _conversation(job_id: str, conv_id: str) -> Conversation:
    run_dir = _run_dir_or_404(job_id)
    try:
        return Conversation(jobs.job_dir(job_id), run_dir, jobs.load(job_id), conv_id)
    except KeyError:
        _not_found("Conversation")


@app.get("/api/runs/{job_id}/chats")
def list_chats(job_id: str) -> list[dict]:
    _run_dir_or_404(job_id)
    return Conversation.all_for(jobs.job_dir(job_id))


@app.post("/api/runs/{job_id}/chats")
def new_chat(job_id: str) -> dict:
    run_dir = _run_dir_or_404(job_id)
    conv = Conversation.create(jobs.job_dir(job_id), run_dir, jobs.load(job_id))
    return {"id": conv.id, "transcript": []}


@app.get("/api/runs/{job_id}/chats/{conv_id}")
def get_chat(job_id: str, conv_id: str) -> dict:
    conv = _conversation(job_id, conv_id)
    return {"id": conv.id, "transcript": conv.transcript()}


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@app.post("/api/runs/{job_id}/chats/{conv_id}/messages")
def send_chat(job_id: str, conv_id: str, body: ChatMessage):
    """Claude's answer as a stream of server-sent events (text pieces, tool calls, files, proposals)."""
    conv = _conversation(job_id, conv_id)

    def events():
        try:
            model = chat_model.chat_model()
            for ev in conv.send(body.text.strip(), model):
                yield f"data: {json.dumps(ev, default=str)}\n\n"
        except Exception as e:  # never leave the browser waiting on a broken stream
            yield f"data: {json.dumps({'type': 'error', 'text': f'The assistant stopped: {e}'})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs/{job_id}/chats/{conv_id}/files/{rel:path}")
def chat_file(job_id: str, conv_id: str, rel: str, download: bool = False):
    conv = _conversation(job_id, conv_id)
    try:
        p = conv.workspace.resolve(rel)
    except ValueError:
        _not_found("File")
    if not p.is_file() or not (rel.startswith("outputs/") or rel.startswith("run/")):
        _not_found("File")
    return FileResponse(p, filename=p.name if download else None,
                        content_disposition_type="attachment" if download else "inline")


# ---- Optional analyses: splicing and isoforms ---------------------------------------------------
@app.get("/api/runs/{job_id}/extras")
def list_extras(job_id: str) -> dict:
    """The optional analyses the code bank offers on this run (from its manifest), with their forms and latest results."""
    _run_dir_or_404(job_id)
    job = jobs.load(job_id)
    return {"available": extras.available(), "forms": {m: extras.form_spec(m, job) for m in extras.modules()},
            "analyses": extras.all_for(jobs.job_dir(job_id))}


@app.post("/api/runs/{job_id}/extras/{module}")
async def start_extra(job_id: str, module: str, request: Request) -> dict:
    """Start an optional analysis. Multipart form: one file field per manifest input (by its name), plus the form settings."""
    run_dir = _run_dir_or_404(job_id)
    if module not in extras.modules():
        raise HTTPException(status_code=404, detail=f"This code bank offers no '{module}' analysis on a run.")
    form = await request.form()
    files: dict[str, list] = {}
    fields: dict[str, str] = {}
    for key, value in form.multi_items():
        if hasattr(value, "filename") and hasattr(value, "file"):
            if value.filename:
                files.setdefault(key, []).append((value.filename, value.file))
        else:
            fields[key] = str(value)
    try:
        return extras.extras.start(module, jobs.load(job_id), jobs.job_dir(job_id), run_dir, files, fields)
    except extras.ExtraError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _extra_dir(job_id: str, module: str) -> Path:
    _run_dir_or_404(job_id)
    try:
        d = extras.latest(jobs.job_dir(job_id), module)
    except KeyError:
        d = None
    if d is None:
        _not_found("Analysis")
    return d


@app.get("/api/runs/{job_id}/extras/{module}")
def get_extra(job_id: str, module: str) -> dict:
    return extras.overview(_extra_dir(job_id, module))


@app.get("/api/runs/{job_id}/extras/{module}/files/{rel:path}")
def extra_file(job_id: str, module: str, rel: str, download: bool = False):
    try:
        p = extras.safe_file(_extra_dir(job_id, module), rel)
    except KeyError:
        _not_found("File")
    return FileResponse(p, filename=p.name if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/runs/{job_id}/extras/{module}/download")
def extra_download(job_id: str, module: str):
    try:
        z = extras.bundle(_extra_dir(job_id, module))
    except KeyError:
        raise HTTPException(status_code=409, detail="No results yet.")
    return FileResponse(z, filename=z.name, media_type="application/zip")


@app.get("/api/runs/{job_id}/code")
def run_code(job_id: str, include_data: bool = False):
    """All code that produced this run (code bank at its commit, settings, custom chat code) as a .zip."""
    run_dir = _run_dir_or_404(job_id)
    try:
        z = code_bundle.build(jobs.load(job_id), jobs.job_dir(job_id), run_dir, include_data=include_data)
    except code_bundle.BundleError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return FileResponse(z, filename=z.name, media_type="application/zip")


@app.get("/api/runs/{job_id}/bundle")
def run_bundle(job_id: str):
    run_dir = _run_dir_or_404(job_id)
    z = results.bundle(run_dir, jobs.job_dir(job_id))
    return FileResponse(z, filename=z.name, media_type="application/zip")


# ---- Cross-study comparison (results level, never merged counts) -------------------------------
@app.get("/api/comparisons")
def list_comparisons() -> dict:
    runs = [{"id": j["id"], "label": comparisons.default_label(j), "dataset": j.get("dataset"),
             "comparison": f"{j['settings']['test_level']} vs {j['settings']['reference_level']}", "created": j["created"]}
            for j in jobs.all_jobs() if j["status"] in ("success", "success_with_errors") and j.get("run_dir")]
    return {"available": comparisons.available(), "comparisons": comparisons.list_all(), "runs": runs}


@app.post("/api/comparisons")
def start_comparison(spec: str = Form(...), files: list[UploadFile] | None = File(None)) -> dict:
    """spec: JSON {title?, studies: [{kind: "run", job_id, label?, flip?} | {kind: "table", file: <index in files>,
    label, organism?, comparison?, flip?}], padj_cutoff?, log2fc_cutoff?, compare_pathways?}"""
    try:
        body = json.loads(spec)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="spec must be JSON.")
    try:
        return comparisons.comparisons.start(body, [(f.filename or "table.csv", f.file) for f in files or [] if f.filename])
    except comparisons.CompareError as e:
        raise HTTPException(status_code=422, detail=str(e))


def _comparison_dir(cid: str) -> Path:
    try:
        return comparisons.comparison_dir(cid)
    except KeyError:
        _not_found("Comparison")


@app.get("/api/comparisons/{cid}")
def get_comparison(cid: str) -> dict:
    return comparisons.overview(_comparison_dir(cid))


@app.get("/api/comparisons/{cid}/files/{rel:path}")
def comparison_file(cid: str, rel: str, download: bool = False):
    try:
        p = comparisons.safe_file(_comparison_dir(cid), rel)
    except KeyError:
        _not_found("File")
    return FileResponse(p, filename=p.name if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/comparisons/{cid}/download")
def comparison_download(cid: str):
    try:
        z = comparisons.results_zip(_comparison_dir(cid))
    except KeyError:
        raise HTTPException(status_code=409, detail="No results yet.")
    return FileResponse(z, filename=z.name, media_type="application/zip")


@app.get("/api/comparisons/{cid}/code")
def comparison_code(cid: str):
    try:
        z = comparisons.code_zip(_comparison_dir(cid))
    except KeyError:
        raise HTTPException(status_code=409, detail="No results yet.")
    return FileResponse(z, filename=z.name, media_type="application/zip")


@app.get("/api/comparisons/{cid}/report/html")
def comparison_report_html(cid: str, download: bool = False):
    try:
        p = comparisons.report_html(_comparison_dir(cid))
    except KeyError:
        raise HTTPException(status_code=409, detail="No results yet.")
    return FileResponse(p, filename=f"comparison_{cid}.html" if download else None, media_type="text/html",
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/comparisons/{cid}/report/pdf")
def comparison_report_pdf(cid: str):
    try:
        p = comparisons.report_pdf(_comparison_dir(cid))
    except KeyError:
        raise HTTPException(status_code=409, detail="No results yet.")
    except report.ReportError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return FileResponse(p, filename=f"comparison_{cid}.pdf", media_type="application/pdf")


# ---- Frontend (built React app) ---------------------------------------------------------------
if settings.frontend_dist.exists():
    app.mount("/assets", StaticFiles(directory=settings.frontend_dist / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path.startswith("api/"):
            _not_found("Endpoint")
        candidate = (settings.frontend_dist / full_path).resolve()
        if full_path and settings.frontend_dist in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(settings.frontend_dist / "index.html")
