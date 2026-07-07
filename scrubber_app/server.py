"""server.py — FastAPI application for the Metadata Scrubber.

This is the API + frontend server that replaces Streamlit.
Start with: uv run -- scrubber_app/server.py

Key features:
- SSE (Server-Sent Events) for real-time pipeline progress
- REST API for registry management
- Jinja2 templating for the frontend (HTML/JS/CSS separation)
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
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from models import (
    BulkDecision,
    CodelistFilter,
    DecisionUpdate,
    FilteredResult,
    PipelineRequest,
    PipelineResponse,
    PipelineStatus,
    RegistryStats,
)
from services.job_manager import JobStatus, job_manager
from services.pipeline_service import start_pipeline_job
from services.registry_service import (
    bulk_set_decisions,
    filter_codelists,
    get_duplicates_for_codelist,
    get_stats,
    read_registry,
    set_decision,
    write_registry,
)
from services.upload_service import upload_file_to_s3

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

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

# No Jinja2 — the HTML template is pure static, no templating logic.
# Passing module globals() to Jinja2 fails because job_manager.jobs is a unhashable dict.
# We serve index.html directly instead.
_INDEX_HTML = (TEMPLATES_DIR / "index.html").read_text(encoding="utf-8")


# ============================================================================
# Frontend Routes
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Serve the main page."""
    # Inject API_BASE from the proxy prefix (X-Script-Name) so JS works
    # whether deployed at root "/" or under a sub-path like "/my-svc/".
    html = _INDEX_HTML
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

@app.get("/static/{file_path:path}")
async def serve_static(file_path: str):
    """Serve static files."""
    file_path = STATIC_DIR / file_path
    if file_path.exists() and file_path.is_file():
        return StreamingResponse(
            open(file_path, "rb"),
            media_type="application/octet-stream"
        )
    raise HTTPException(status_code=404, detail="Static file not found")


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

        # Send initial state
        yield _sse("init", {
            "status": job.status.value if isinstance(job.status, JobStatus) else job.status,
            "progress": job.progress,
            "phase": job.phase,
            "phase_label": job.phase_label,
            "current_log": job.current_log,
            "logs": job.logs[-10:],
        })

        # Create a new asyncio.Queue for this client
        client_queue = asyncio.Queue(maxsize=100)
        job.listeners.append({
            "queue": client_queue,
            "connected_at": time.time(),
            "job_id": job_id,
        })

        # Track whether we already forwarded a "done" event
        done_sent = False

        # Send events from this connection's own queue
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(client_queue.get(), timeout=15.0)
                    event_type = msg.get("event", "message")
                    # Mark when a "done" event is forwarded
                    if event_type == "done":
                        done_sent = True
                    yield _sse(event_type, msg.get("data", ""))
                except asyncio.TimeoutError:
                    # Keepalive ping
                    yield _sse("ping", "keepalive")
        except asyncio.CancelledError:
            pass

        # Fallback: if the job reached a final state and we haven't
        # forwarded a "done" event yet, emit it now (e.g. connection
        # dropped mid-stream).
        if not done_sent and job.status not in (JobStatus.PENDING, JobStatus.RUNNING):
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

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
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
        with _registry_lock:
            _registry_path = path
        return {"message": "Registry saved", "path": path}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving registry: {e}")


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
    output_base: str = Query("audit", description="Repertoire ou URL S3 de destination"),
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
# Static Files
# ============================================================================

# Only mount static files if the directory exists
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


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
