"""server.py — FastAPI application for the Metadata Scrubber.

This is the API + frontend server that replaces Streamlit.
Start with: uv run scrubber-web

Key features:
- SSE (Server-Sent Events) for real-time pipeline progress
- REST API for registry management
- Embedded frontend (HTML/CSS/JS inline from package resources)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from importlib import resources as importlib_resources
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse

from metadata_scrubber.ui.models import (
    AddToCleanedRequest,
    BulkDecision,
    CodelistFilter,
    DecisionUpdate,
    FilteredResult,
    PipelineRequest,
    PipelineResponse,
    PipelineStatus,
    RegistryStats,
)
from metadata_scrubber.ui.services.job_manager import JobStatus, job_manager
from metadata_scrubber.ui.services.pipeline_service import start_pipeline_job
from metadata_scrubber.cleaned_registry import (
    add_entry_from_parent,
    cleaned_registry_path,
    empty_cleaned_doc,
    read_cleaned_registry,
    validate_cleaned_doc,
    write_cleaned_registry,
)
from metadata_scrubber.ui.services.registry_service import (
    bulk_set_decisions,
    filter_codelists,
    get_duplicates_for_codelist,
    get_stats,
    read_registry,
    set_decision,
    write_registry,
)
from metadata_scrubber.ui.services.upload_service import upload_file_to_s3

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Global registry state (in-memory, synchronized by registry_path)
_registry_lock = threading.Lock()
_registry_state: dict[str, Any] | None = None
_registry_path: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle."""
    logger.info("Metadata Scrubber API starting...")
    # Clean up old jobs periodically
    cleanup_thread = threading.Thread(target=_cleanup_loop, daemon=True)
    cleanup_thread.start()
    yield
    logger.info("Metadata Scrubber API shutting down...")


def _cleanup_loop() -> None:
    """Periodically clean up old finished jobs."""
    while True:
        time.sleep(300)  # every 5 minutes
        removed = job_manager.cleanup(max_age=3600)
        if removed:
            logger.info("Cleaned up %d old jobs", removed)


app = FastAPI(
    title="Metadata Scrubber API",
    description="Pipeline de deloublonnage des CodeLists DDI",
    version="1.0.0",
    lifespan=lifespan,
    root_path=os.environ.get("X_SCRIPT_NAME", ""),
)

# ============================================================================
# Frontend — embedded from package resources (no StaticFiles needed)
# ============================================================================

_UI_PACKAGE = "metadata_scrubber.ui"


def _read_template() -> str:
    """Load index.html from the ui.templates package resource."""
    return (
        importlib_resources.files(f"{_UI_PACKAGE}.templates")
        .joinpath("index.html")
        .read_text(encoding="utf-8")
    )


def _read_static(filename: str) -> str:
    """Read a CSS or JS file from ui.static.<path>."""
    if filename.endswith(".css"):
        return (
            importlib_resources.files(f"{_UI_PACKAGE}.static.css")
            .joinpath(filename)
            .read_text(encoding="utf-8")
        )
    return (
        importlib_resources.files(f"{_UI_PACKAGE}.static.js")
        .joinpath(filename)
        .read_text(encoding="utf-8")
    )


def _inline_assets(html: str) -> str:
    """
    Inline local CSS and JS into the HTML so no /static endpoint is needed.

    - <link rel="stylesheet" href="static/css/style.css"> → <style>…</style>
    - <script src="static/js/…"> → <script>…</script>

    CDN links are left untouched.
    """
    css_file = "style.css"
    css = _read_static(css_file)
    html = html.replace(
        f'<link rel="stylesheet" href="static/css/{css_file}">',
        f"<style>\n{css}\n</style>",
    )

    js_files = ["config.js", "main.js", "pipeline.js", "registry.js", "cleaned.js"]
    for js_file in js_files:
        js = _read_static(js_file)
        # Replace <script src="static/js/xxx.js"></script> → <script>…</script>
        pattern = f'<script src="static/js/{js_file}"></script>'
        html = html.replace(pattern, f"<script>{js}</script>")

    return html


# ============================================================================
# Frontend Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main page with CSS/JS inlined from package resources."""
    html = _read_template()
    html = _inline_assets(html)
    return HTMLResponse(html)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "jobs_active": sum(
            1 for j in job_manager.jobs.values()
            if j.status in (JobStatus.RUNNING, JobStatus.PENDING)
        ),
        "version": "1.0.0",
    }

# ============================================================================
# Pipeline API
# ============================================================================

@app.post("/api/pipeline", response_model=PipelineResponse)
async def launch_pipeline(req: PipelineRequest):
    """Creer un job de pipeline et le lancer en arriere-plan.

    Repond instantanement avec un job_id.
    Le frontend se connecte ensuite au SSE pour la progression.
    """
    job_id = start_pipeline_job(
        xml_source=req.xml_source,
        audit_dir=req.output_base,
        run_llm=req.run_llm,
        verbose=req.verbose,
        registry_path=req.registry_path,
    )
    return PipelineResponse(job_id=job_id, status="pending")


@app.get("/api/pipeline/{job_id}/status", response_model=PipelineStatus)
async def get_pipeline_status(job_id: str):
    """Etat d'un job pipeline."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    return PipelineStatus(
        job_id=job.job_id,
        status=job.status.value if isinstance(job.status, JobStatus) else job.status,
        progress=job.progress,
        phase=job.phase,
        phase_label=job.phase_label,
        current_log=job.current_log,
        logs=job.logs[-50:],  # 50 derniers logs
        result=job.result,
        error_message=job.error_message,
        duration_seconds=job.finished_at - job.created_at if job.finished_at else None,
    )


@app.get("/api/pipeline/{job_id}/sse")
async def pipeline_sse(job_id: str):
    """SSE stream pour la progression du pipeline."""
    job = job_manager.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        def _sse(event: str, data: Any) -> str:
            """Format a dict as SSE protocol."""
            return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

        # Register the listener BEFORE reading history: broadcast() holds
        # job.lock too, so every event is either in `history` or delivered
        # to the queue — no gap, no duplicate.
        loop = asyncio.get_running_loop()
        client_queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        listener = {
            "queue": client_queue,
            "loop": loop,
            "connected_at": time.time(),
            "job_id": job_id,
        }
        with job.lock:
            history = list(job.events)
            job.listeners.append(listener)

        try:
            # Padding anti-buffering : certains proxys ne flushent qu'après ~2 Ko.
            # (ligne de commentaire SSE, ignorée par EventSource)
            yield ":" + (" " * 2048) + "\n" + "retry: 3000\n\n"

            # Send initial state (logs are replayed via history, not here)
            yield _sse("init", {
                "status": job.status.value if isinstance(job.status, JobStatus) else job.status,
                "progress": job.progress,
                "phase": job.phase,
                "phase_label": job.phase_label,
                "current_log": job.current_log,
            })

            # Replay des événements émis avant la connexion du client
            for msg in history:
                event_type = msg.get("event", "message")
                yield _sse(event_type, msg.get("data", ""))
                if event_type == "done":
                    return

            # Job déjà terminé sans événement "done" enregistré (sécurité)
            if job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
                final_data = {
                    "status": job.status.value if isinstance(job.status, JobStatus) else job.status,
                    "progress": job.progress,
                    "finished": True,
                }
                if job.result:
                    final_data["result"] = job.result
                if job.error_message:
                    final_data["error"] = job.error_message
                yield _sse("done", final_data)
                return

            # Flux temps réel
            try:
                while True:
                    try:
                        msg = await asyncio.wait_for(client_queue.get(), timeout=15.0)
                        event_type = msg.get("event", "message")
                        yield _sse(event_type, msg.get("data", ""))
                        if event_type == "done":
                            return
                    except asyncio.TimeoutError:
                        # Keepalive ping
                        yield _sse("ping", "keepalive")
            except asyncio.CancelledError:
                pass
        finally:
            with job.lock:
                if listener in job.listeners:
                    job.listeners.remove(listener)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )


# ============================================================================
# Registry API
# ============================================================================

@app.get("/api/registry/stats", response_model=RegistryStats)
async def registry_stats():
    """Statistiques globales du registre."""
    with _registry_lock:
        if _registry_state is None:
            raise HTTPException(status_code=404, detail="Registry not loaded. Load it first.")
        stats = get_stats(_registry_state)
    return RegistryStats(**stats)


@app.get("/api/registry", response_model=dict[str, Any])
async def load_registry(path: str | None = Query(None)):
    """Charger le registre des doublons.

    Args:
        path: chemin du fichier (optionnel — par defaut charge le dernier utilise).
    """
    global _registry_state, _registry_path

    path = path or _registry_path
    if not path:
        raise HTTPException(status_code=400, detail="aucun chemin fourni")

    try:
        registry = read_registry(path)
        with _registry_lock:
            _registry_state = registry
            _registry_path = path
        return registry
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Registry not found: {path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading registry: {e}")


@app.post("/api/registry/save")
async def save_registry(path: str | None = Query(None)):
    """Sauvegarder le registre."""
    global _registry_state, _registry_path

    with _registry_lock:
        if _registry_state is None:
            raise HTTPException(status_code=400, detail="No registry loaded")
        data = _registry_state
        path = path or _registry_path
        if not path:
            raise HTTPException(status_code=400, detail="No path provided")

    try:
        write_registry(data, path)
        cleaned_path, cleaned_count = write_cleaned_registry(data, path)
        with _registry_lock:
            _registry_path = path
        return {
            "message": "Registry saved",
            "path": path,
            "cleaned_path": cleaned_path,
            "cleaned_count": cleaned_count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving registry: {e}")


@app.get("/api/registry/cleaned", response_model=dict[str, Any])
async def get_cleaned_registry():
    """Registre des CodeLists nettoyées associé au registre chargé (lu sur disque)."""
    with _registry_lock:
        if _registry_path is None:
            raise HTTPException(status_code=400, detail="No registry loaded")
        path = cleaned_registry_path(_registry_path)
    try:
        return read_cleaned_registry(path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Registre nettoyé introuvable ({path}) — sauvegardez d'abord le registre.",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture registre nettoyé: {e}")


@app.post("/api/registry/add-to-cleaned")
async def add_to_cleaned(req: AddToCleanedRequest):
    """Ajoute manuellement une CodeList (sans doublon) au registre nettoyé."""
    with _registry_lock:
        if _registry_state is None or _registry_path is None:
            raise HTTPException(status_code=400, detail="No registry loaded")
        cl_data = _registry_state.get(req.cl_id)
        if cl_data is None:
            raise HTTPException(status_code=404, detail=f"CodeList {req.cl_id} not found")
        path = cleaned_registry_path(_registry_path)

    try:
        cleaned = read_cleaned_registry(path)
    except FileNotFoundError:
        cleaned = empty_cleaned_doc()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture registre nettoyé: {e}")

    if not add_entry_from_parent(cleaned, cl_data):
        raise HTTPException(status_code=409, detail="CodeList déjà présente dans le registre nettoyé")

    try:
        write_registry(cleaned, path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur écriture: {e}")
    return {
        "message": "CodeList ajoutée au registre nettoyé",
        "path": path,
        "count": len(cleaned["codelists"]),
    }


# ============================================================================
# Cleaned registry API (édition du registre nettoyé — stateless)
# ============================================================================

@app.get("/api/cleaned", response_model=dict[str, Any])
async def get_cleaned(path: str = Query(...)):
    """Lit un registre nettoyé (local ou S3), migré au schéma v2."""
    try:
        return read_cleaned_registry(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Fichier introuvable: {path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture: {e}")


@app.post("/api/cleaned/save")
async def save_cleaned(request: Request, path: str = Query(...)):
    """Sauvegarde un registre nettoyé édité (validation avant écriture)."""
    from datetime import datetime, timezone

    try:
        doc = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Corps JSON invalide")

    errors = validate_cleaned_doc(doc)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    doc["generated_at"] = datetime.now(timezone.utc).isoformat()
    doc["version"] = 2
    try:
        write_registry(doc, path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur écriture: {e}")
    return {
        "message": "Registre nettoyé sauvegardé",
        "path": path,
        "count": len(doc.get("codelists", {})),
    }


@app.patch("/api/registry/decision")
async def update_decision(update: DecisionUpdate):
    """Mettre a jour la decision d'un duplicate."""
    with _registry_lock:
        if _registry_state is None:
            raise HTTPException(status_code=400, detail="No registry loaded")
        success = set_decision(_registry_state, update.cl_id, update.dup_id, update.decision)
        if not success:
            raise HTTPException(status_code=404, detail="Duplicate not found")
    return {"success": True, "cl_id": update.cl_id, "dup_id": update.dup_id, "decision": update.decision}


@app.post("/api/registry/bulk")
async def bulk_decisions(bulk: BulkDecision):
    """Appliquer une decision en masse sur plusieurs duplicates."""
    with _registry_lock:
        if _registry_state is None:
            raise HTTPException(status_code=400, detail="No registry loaded")
        count = bulk_set_decisions(_registry_state, bulk.criteria, bulk.action)
    return {"message": f"{count} duplicates updated", "count": count}


@app.get("/api/registry/codelists", response_model=FilteredResult)
async def list_codelists(
    request: CodelistFilter = Query(CodelistFilter()),
):
    """Liste pageable des CodeLists avec filtres."""
    with _registry_lock:
        if _registry_state is None:
            raise HTTPException(status_code=404, detail="Registry not loaded")
        result = filter_codelists(
            registry=_registry_state,
            decision_filter=request.decision_filter,
            search=request.search,
            sort_by=request.sort_by,
            page=request.page,
            page_size=request.page_size,
        )
    return FilteredResult(**result)


@app.get("/api/registry/codelist/{cl_id}")
async def get_codelist(cl_id: str):
    """Donnees detaillees d'une CodeList."""
    with _registry_lock:
        if _registry_state is None:
            raise HTTPException(status_code=404, detail="Registry not loaded")
        result = get_duplicates_for_codelist(_registry_state, cl_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"CodeList {cl_id} not found")
    return result


# ============================================================================
# Upload API
# ============================================================================

@app.post("/api/upload")
async def upload_file(
    file: UploadFile = File(...),
    output_base: str = Query(
        "s3://projet-metadonnees-rmes/",
        description="Repertoire ou URL S3 de destination",
    ),
):
    """Upload un fichier vers S3."""
    try:
        # Save the uploaded file temporarily
        with tempfile.NamedTemporaryFile(delete=False, suffix=file.filename) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name

        # Upload to S3
        s3_url = upload_file_to_s3(
            local_path=tmp_path,
            s3_base=output_base,
            filename=file.filename,
            capture=None,
        )

        # Cleanup
        os.unlink(tmp_path)

        if not s3_url:
            raise HTTPException(status_code=500, detail="Upload failed")

        return {"url": s3_url, "filename": file.filename}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upload error: {str(e)}")


# ============================================================================
# Entrypoint
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )


def main() -> None:
    """Entry-point console pour ``uv run scrubber-web``."""
    import uvicorn

    uvicorn.run(
        "metadata_scrubber.ui.server:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info",
    )
