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
    AddToCleanedBatchRequest,
    AddToCleanedRequest,
    RemoveFromCleanedRequest,
    BulkDecision,
    CodelistFilter,
    DismissRequest,
    SwapParentRequest,
    DecisionBatch,
    DecisionUpdate,
    FilteredResult,
    PipelineRequest,
    PipelineResponse,
    PipelineStatus,
    RegistryStats,
)
from metadata_scrubber.ui.services.job_manager import JobStatus, job_manager
from metadata_scrubber.ui.services.pipeline_service import start_pipeline_job
from metadata_scrubber.cleaned_registry import SCHEMA_VERSION as CLEANED_SCHEMA_VERSION
from metadata_scrubber.cleaned_registry import (
    add_entries_from_parents,
    add_entry_from_parent,
    remove_entries,
    cleaned_registry_path,
    empty_cleaned_doc,
    read_cleaned_registry,
    validate_cleaned_doc,
    write_cleaned_registry,
)
from metadata_scrubber.decisions import (
    decisions_path,
    load_decisions,
    read_decisions,
    reapply_decisions,
    update_decisions,
    write_decisions,
)
from metadata_scrubber.run_output import list_run_dirs
from metadata_scrubber.ui.services.registry_service import (
    bulk_set_decisions,
    filter_codelists,
    get_decision,
    get_duplicates_for_codelist,
    get_stats,
    read_registry,
    set_decision,
    swap_parent,
    SwapParentError,
    write_registry,
)
from metadata_scrubber.ui.services.upload_service import upload_file_to_s3
from metadata_scrubber.ui.auth import AuthMiddleware

# Logging configuration
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Registres des doublons chargés, indexés par chemin : deux utilisateurs qui
# travaillent sur des runs différents ne s'écrasent pas. Les routes prennent un
# paramètre `path` ; à défaut, le dernier registre chargé.
_registry_lock = threading.Lock()
_registries: dict[str, dict[str, Any]] = {}
_registry_path: str | None = None
_MAX_LOADED_REGISTRIES = 10


def _loaded(path: str | None, status_code: int = 400) -> tuple[str, dict[str, Any]]:
    """(chemin, registre) chargé pour `path` (défaut : le dernier chargé).

    À appeler sous `_registry_lock`.
    """
    path = path or _registry_path
    registry = _registries.get(path) if path else None
    if registry is None:
        raise HTTPException(status_code=status_code, detail="No registry loaded")
    return path, registry


def _persist_decisions(path: str, registry: dict[str, Any]) -> dict[str, Any]:
    """Enregistre decisions.json (sauvegarde automatique des décisions).

    À appeler sous `_registry_lock`. Un échec n'annule pas la modification en
    mémoire : le client signale l'erreur et peut relancer l'enregistrement
    (POST /api/registry/persist).
    """
    try:
        dpath = decisions_path(path)
        doc = read_decisions(dpath)
        update_decisions(doc, registry)
        write_registry(doc, dpath)
        return {"saved": True, "saved_at": doc["updated_at"]}
    except Exception as e:
        logger.exception("decisions.json non enregistré pour %s", path)
        return {"saved": False, "save_error": str(e)}


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

# Protège tous les_ENDPOINTS sauf /health, /docs, /openapi.json
app.add_middleware(AuthMiddleware)

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

    js_files = ["config.js", "main.js", "pipeline.js", "registry.js", "cleaned.js", "precheck.js"]
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
    api_scope = req.api_scope()
    if api_scope is None and not req.xml_source:
        raise HTTPException(status_code=422, detail="Indiquez un fichier XML ou une source API")
    job_id = start_pipeline_job(
        xml_source=req.xml_source or "",
        audit_dir=req.output_base,
        run_llm=req.run_llm,
        verbose=req.verbose,
        registry_path=req.registry_path,
        api_scope=api_scope,
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
def registry_stats(path: str | None = Query(None)):
    """Statistiques globales du registre."""
    with _registry_lock:
        _, registry = _loaded(path, status_code=404)
        stats = get_stats(registry)
    return RegistryStats(**stats)


@app.get("/api/registry", response_model=dict[str, Any])
def load_registry(path: str | None = Query(None)):
    """Charger le registre des doublons.

    Les décisions enregistrées dans decisions.json (sauvegarde automatique) sont
    réappliquées : un registre rechargé retrouve son dernier état.

    Args:
        path: chemin du fichier (optionnel — par defaut charge le dernier utilise).
    """
    global _registry_path

    path = path or _registry_path
    if not path:
        raise HTTPException(status_code=400, detail="aucun chemin fourni")

    try:
        registry = read_registry(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Registry not found: {path}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error loading registry: {e}")
    reapply_decisions(registry, load_decisions(decisions_path(path)))

    with _registry_lock:
        _registries.pop(path, None)
        _registries[path] = registry
        while len(_registries) > _MAX_LOADED_REGISTRIES:
            _registries.pop(next(iter(_registries)))
        _registry_path = path
    return registry


@app.post("/api/registry/save")
def save_registry(path: str | None = Query(None)):
    """Écrit le registre des doublons et synchronise le registre nettoyé et decisions.json."""
    with _registry_lock:
        path, data = _loaded(path)

    try:
        write_registry(data, path)
        cleaned_path, cleaned_count = write_cleaned_registry(data, path)
        decisions_file, decisions_count = write_decisions(data, path)
        return {
            "message": "Registry saved",
            "path": path,
            "cleaned_path": cleaned_path,
            "cleaned_count": cleaned_count,
            "decisions_path": decisions_file,
            "decisions_count": decisions_count,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error saving registry: {e}")


@app.post("/api/registry/persist")
def persist_decisions(path: str | None = Query(None)):
    """Relance l'enregistrement de decisions.json après un échec."""
    with _registry_lock:
        path, registry = _loaded(path)
        return _persist_decisions(path, registry)


@app.get("/api/registry/cleaned", response_model=dict[str, Any])
def get_cleaned_registry(path: str | None = Query(None)):
    """Registre des CodeLists nettoyées associé au registre chargé (lu sur disque)."""
    with _registry_lock:
        registry_path, _ = _loaded(path)
    cleaned_path = cleaned_registry_path(registry_path)
    try:
        return read_cleaned_registry(cleaned_path)
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"Registre nettoyé introuvable ({cleaned_path}) — sauvegardez d'abord le registre.",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture registre nettoyé: {e}")


@app.post("/api/registry/add-to-cleaned")
def add_to_cleaned(req: AddToCleanedRequest, path: str | None = Query(None)):
    """Ajoute manuellement une CodeList (sans doublon) au registre nettoyé."""
    with _registry_lock:
        registry_path, registry = _loaded(path)
        cl_data = registry.get(req.cl_id)
        if cl_data is None:
            raise HTTPException(status_code=404, detail=f"CodeList {req.cl_id} not found")
    cleaned_path = cleaned_registry_path(registry_path)

    try:
        cleaned = read_cleaned_registry(cleaned_path)
    except FileNotFoundError:
        cleaned = empty_cleaned_doc()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture registre nettoyé: {e}")

    if not add_entry_from_parent(cleaned, cl_data):
        raise HTTPException(status_code=409, detail="CodeList déjà présente dans le registre nettoyé")

    try:
        write_registry(cleaned, cleaned_path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur écriture: {e}")
    return {
        "message": "CodeList ajoutée au registre nettoyé",
        "path": cleaned_path,
        "count": len(cleaned["codelists"]),
    }


@app.post("/api/registry/add-to-cleaned/batch")
def add_to_cleaned_batch(req: AddToCleanedBatchRequest, path: str | None = Query(None)):
    """Ajoute en lot des CodeLists sans doublon au registre nettoyé (une seule écriture)."""
    if not req.all_without_duplicates and not req.cl_ids:
        raise HTTPException(status_code=422, detail="cl_ids vide et all_without_duplicates faux")

    with _registry_lock:
        registry_path, loaded = _loaded(path)
        registry = dict(loaded)
    cleaned_path = cleaned_registry_path(registry_path)

    try:
        cleaned = read_cleaned_registry(cleaned_path)
    except FileNotFoundError:
        cleaned = empty_cleaned_doc()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture registre nettoyé: {e}")

    result = add_entries_from_parents(
        cleaned, registry, None if req.all_without_duplicates else req.cl_ids
    )

    if result["added"]:
        try:
            write_registry(cleaned, cleaned_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erreur écriture: {e}")
    return {**result, "path": cleaned_path, "count": len(cleaned["codelists"])}


@app.post("/api/registry/remove-from-cleaned")
def remove_from_cleaned(req: RemoveFromCleanedRequest, path: str | None = Query(None)):
    """Retire du registre nettoyé des CodeLists ajoutées à la main (sans `replaces`)."""
    with _registry_lock:
        registry_path, _ = _loaded(path)
    cleaned_path = cleaned_registry_path(registry_path)

    try:
        cleaned = read_cleaned_registry(cleaned_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Registre nettoyé introuvable")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur lecture registre nettoyé: {e}")

    result = remove_entries(cleaned, req.cl_ids)
    if result["removed"]:
        try:
            write_registry(cleaned, cleaned_path)
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Erreur écriture: {e}")
    return {**result, "path": cleaned_path, "count": len(cleaned["codelists"])}


@app.get("/api/registry/run-summary")
def get_run_summary(path: str | None = Query(None)):
    """Résumé du run (run_summary.json) situé à côté du registre chargé, s'il existe."""
    with _registry_lock:
        registry_path, _ = _loaded(path)
    base = registry_path.rsplit("/", 1)[0] if "/" in registry_path else "."
    try:
        return read_registry(f"{base}/run_summary.json")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Pas de run_summary.json pour ce registre")
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"run_summary.json illisible: {e}")


# ============================================================================
# API Métadonnées de l'Insee (aide à la saisie de la source)
# ============================================================================

_series_cache: dict[str, Any] = {}


@app.get("/api/insee/series")
def insee_series():
    """Séries de l'API Métadonnées (id, libellé), gardées en mémoire une heure."""
    from metadata_scrubber.insee_api import InseeMetadataClient

    if _series_cache.get("at", 0) > time.time() - 3600:
        return _series_cache["series"]
    try:
        series = InseeMetadataClient().list_series()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"API Insee indisponible : {e}")
    _series_cache.update(at=time.time(), series=series)
    return series


# ============================================================================
# Runs (dossiers datés d'une racine de sortie)
# ============================================================================

_RUN_SUMMARY_FIELDS = ("status", "xml_source", "started_at", "duration_seconds", "counts", "warnings")


@app.get("/api/runs")
def list_runs(root: str = Query(...), limit: int = Query(30, ge=1, le=200)):
    """Runs d'une racine de sortie, du plus récent au plus ancien, avec leur résumé."""
    try:
        run_dirs = list_run_dirs(root)[:limit]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Lecture de {root} impossible : {e}")

    runs = []
    for run_dir in run_dirs:
        run: dict[str, Any] = {
            "run_id": run_dir.rsplit("/", 1)[-1],
            "run_dir": run_dir,
            "registry_path": f"{run_dir}/codelist_duplicates.json",
        }
        try:
            summary = read_registry(f"{run_dir}/run_summary.json")
            run.update({k: summary[k] for k in _RUN_SUMMARY_FIELDS if k in summary})
        except Exception:
            run["status"] = "unknown"   # run antérieur à run_summary.json, ou illisible
        runs.append(run)
    return {"root": root.rstrip("/"), "runs": runs}


# ============================================================================
# Cleaned registry API (édition du registre nettoyé — stateless)
# ============================================================================

@app.get("/api/cleaned", response_model=dict[str, Any])
def get_cleaned(path: str = Query(...)):
    """Lit un registre nettoyé (local ou S3), migré au schéma courant."""
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
    doc["version"] = CLEANED_SCHEMA_VERSION
    try:
        write_registry(doc, path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erreur écriture: {e}")
    return {
        "message": "Registre nettoyé sauvegardé",
        "path": path,
        "count": len(doc.get("codelists", {})),
    }


# ============================================================================
# Décisions (enregistrées automatiquement dans decisions.json)
# ============================================================================

@app.patch("/api/registry/decision")
def update_decision(update: DecisionUpdate, path: str | None = Query(None)):
    """Mettre a jour la decision d'un duplicate."""
    with _registry_lock:
        path, registry = _loaded(path)
        success = set_decision(registry, update.cl_id, update.dup_id, update.decision)
        if not success:
            raise HTTPException(status_code=404, detail="Duplicate not found")
        saved = _persist_decisions(path, registry)
    return {
        "success": True,
        "cl_id": update.cl_id,
        "dup_id": update.dup_id,
        "decision": update.decision,
        **saved,
    }


@app.post("/api/registry/decisions")
def set_decisions_batch(batch: DecisionBatch, path: str | None = Query(None)):
    """Applique plusieurs décisions en une fois.

    Sert aux actions de masse sur la sélection affichée et à leur annulation :
    la réponse contient les décisions précédentes (`previous`), à renvoyer
    telles quelles pour annuler.
    """
    with _registry_lock:
        path, registry = _loaded(path)
        previous: list[dict[str, str]] = []
        not_found: list[dict[str, str]] = []
        for item in batch.items:
            old = get_decision(registry, item.cl_id, item.dup_id)
            if old is None:
                not_found.append({"cl_id": item.cl_id, "dup_id": item.dup_id})
                continue
            set_decision(registry, item.cl_id, item.dup_id, item.decision)
            previous.append({"cl_id": item.cl_id, "dup_id": item.dup_id, "decision": old})
        saved = _persist_decisions(path, registry) if previous else {"saved": True}
    return {"count": len(previous), "previous": previous, "not_found": not_found, **saved}


@app.post("/api/registry/bulk")
def bulk_decisions(bulk: BulkDecision, path: str | None = Query(None)):
    """Appliquer une decision en masse sur tout le registre (préférer /api/registry/decisions)."""
    with _registry_lock:
        path, registry = _loaded(path)
        count = bulk_set_decisions(registry, bulk.criteria, bulk.action)
        saved = _persist_decisions(path, registry)
    return {"message": f"{count} duplicates updated", "count": count, **saved}


@app.post("/api/registry/dismiss")
def dismiss_codelists(req: DismissRequest, path: str | None = Query(None)):
    """Écarte (ou réintègre) des CodeLists sans doublon ; enregistré dans decisions.json."""
    result: dict[str, list[str]] = {"updated": [], "not_found": [], "skipped_with_duplicates": []}
    with _registry_lock:
        path, registry = _loaded(path)
        for cl_id in dict.fromkeys(req.cl_ids):
            cl_data = registry.get(cl_id)
            if cl_data is None:
                result["not_found"].append(cl_id)
            elif cl_data.get("duplicates"):
                result["skipped_with_duplicates"].append(cl_id)
            else:
                if req.dismissed:
                    cl_data["dismissed"] = True
                else:
                    cl_data.pop("dismissed", None)
                result["updated"].append(cl_id)
        saved = _persist_decisions(path, registry) if result["updated"] else {"saved": True}
    return {**result, "dismissed": req.dismissed, **saved}


@app.post("/api/registry/swap-parent")
def swap_parent_route(req: SwapParentRequest, path: str | None = Query(None)):
    """Fait d'un doublon la maître de son groupe.

    Le changement de maître ne se déduit pas de decisions.json : le registre des
    doublons est réécrit en plus de decisions.json.
    """
    with _registry_lock:
        path, registry = _loaded(path)
        try:
            entry = swap_parent(registry, req.parent_id, req.new_parent_id)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=f"CodeList introuvable : {e}")
        except SwapParentError as e:
            raise HTTPException(status_code=409, detail=str(e))
        saved = _persist_decisions(path, registry)
        if saved["saved"]:
            try:
                write_registry(registry, path)
            except Exception as e:
                logger.exception("Registre non enregistré après changement de maître")
                saved = {"saved": False, "save_error": str(e)}
    return {"old_parent_id": req.parent_id, "entry": entry, **saved}


@app.get("/api/registry/codelists", response_model=FilteredResult)
def list_codelists(
    request: CodelistFilter = Query(CodelistFilter()),
    path: str | None = Query(None),
):
    """Liste pageable des CodeLists avec filtres."""
    with _registry_lock:
        _, registry = _loaded(path, status_code=404)
        result = filter_codelists(
            registry=registry,
            decision_filter=request.decision_filter,
            search=request.search,
            sort_by=request.sort_by,
            page=request.page,
            page_size=request.page_size,
        )
    return FilteredResult(**result)


@app.get("/api/registry/codelist/{cl_id}")
def get_codelist(cl_id: str, path: str | None = Query(None)):
    """Donnees detaillees d'une CodeList."""
    with _registry_lock:
        _, registry = _loaded(path, status_code=404)
        result = get_duplicates_for_codelist(registry, cl_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"CodeList {cl_id} not found")
    return result


# ============================================================================
# Pré-contrôle d'un fichier DDI (moteur du service scrubber-check)
# ============================================================================

_precheck_holder = None
_PRECHECK_MAX_BYTES = 50 * 1024 * 1024


def _precheck_index(path: str | None):
    global _precheck_holder
    from metadata_scrubber.check.api import _IndexHolder

    if _precheck_holder is None:
        _precheck_holder = _IndexHolder()
    return _precheck_holder.get(path)


@app.post("/api/precheck")
async def precheck(
    file: UploadFile = File(...),
    index: str | None = Query(None, description="Index de référence (défaut : SCRUBBER_CHECK_INDEX)."),
    exclude_operation: list[str] = Query(default=[]),
    format: str = Query("json", pattern="^(json|csv)$"),
):
    """Contrôle un fichier DDI contre les CodeLists publiées (index de scrubber-check)."""
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import PlainTextResponse

    from metadata_scrubber.check.report import check_bytes, to_csv

    raw = await file.read(_PRECHECK_MAX_BYTES + 1)
    if len(raw) > _PRECHECK_MAX_BYTES:
        raise HTTPException(status_code=413, detail="Fichier trop volumineux (max 50 Mo)")
    try:
        ref = await run_in_threadpool(_precheck_index, index or None)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Index de référence illisible : {e}")
    ref = ref.restrict(exclude_operations=exclude_operation)
    try:
        report = await run_in_threadpool(check_bytes, raw, file.filename or "fichier.xml", ref)
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"Fichier DDI illisible : {e}")
    if format == "csv":
        return PlainTextResponse(to_csv(report), media_type="text/csv; charset=utf-8")
    return report


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
