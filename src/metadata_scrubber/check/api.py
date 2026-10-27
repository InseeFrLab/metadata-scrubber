"""api.py — API HTTP du service de contrôle (`scrubber-check-api`), sans interface.

    POST /v1/check   fichier DDI (multipart `file`, ou corps XML brut) → rapport
    GET  /v1/index   métadonnées de l'index chargé
    GET  /health

Variables d'environnement :
    SCRUBBER_CHECK_INDEX          index de référence (local ou S3)
    SCRUBBER_CHECK_MAX_MB         taille maximale d'un fichier reçu (défaut 50)
    SCRUBBER_CHECK_AUTH_PASSWORD  si défini, Basic Auth sur /v1/* (mot de passe partagé)
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from .index import ReferenceIndex, default_index_path
from .report import check_bytes, to_csv

_RELOAD_CHECK_SECONDS = 300


def _max_bytes() -> int:
    return int(float(os.environ.get("SCRUBBER_CHECK_MAX_MB", "50")) * 1024 * 1024)


class _IndexHolder:
    """Index chargé une fois, rechargé quand le fichier change (vérifié toutes les 5 min)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.index: ReferenceIndex | None = None
        self.path: str | None = None
        self.stamp: Any = None
        self.checked_at = 0.0

    @staticmethod
    def _stamp(path: str) -> Any:
        if path.startswith("s3://"):
            from ..s3 import make_s3_filesystem

            info = make_s3_filesystem().info(path)
            return info.get("ETag") or info.get("LastModified")
        return os.path.getmtime(path)

    def get(self, path: str | None = None) -> ReferenceIndex:
        path = path or default_index_path()
        with self.lock:
            now = time.time()
            if self.index is not None and path == self.path and now - self.checked_at < _RELOAD_CHECK_SECONDS:
                return self.index
            try:
                stamp = self._stamp(path)
            except FileNotFoundError:
                raise HTTPException(status_code=503, detail=f"Index de référence absent : {path}")
            if self.index is None or path != self.path or stamp != self.stamp:
                self.index = ReferenceIndex.load(path)
                self.path, self.stamp = path, stamp
            self.checked_at = now
            return self.index


_holder = _IndexHolder()

app = FastAPI(
    title="scrubber-check",
    description=(
        "Contrôle d'un fichier DDI 3.3 : ses CodeLists sont-elles déjà publiées dans l'API "
        "Métadonnées de l'Insee (doublons, sous-ensembles, listes proches) ?"
    ),
    version="1.0.0",
    root_path=os.environ.get("X_SCRIPT_NAME", ""),
)


@app.middleware("http")
async def _basic_auth(request: Request, call_next):
    """Basic Auth optionnelle (mot de passe partagé) sur les routes /v1/*."""
    password = os.environ.get("SCRUBBER_CHECK_AUTH_PASSWORD")
    if password and request.url.path.startswith("/v1/"):
        header = request.headers.get("Authorization", "")
        try:
            given = base64.b64decode(header.removeprefix("Basic ")).decode().partition(":")[2]
        except Exception:
            given = ""
        if not header.startswith("Basic ") or not secrets.compare_digest(given, password):
            return JSONResponse(
                {"detail": "Authentication required"},
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="scrubber-check"'},
            )
    return await call_next(request)


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "healthy", "index_loaded": _holder.index is not None}


@app.get("/v1/index")
def index_info() -> dict[str, Any]:
    meta = dict(_holder.get().meta)
    meta.pop("operations", None)
    return meta


async def _read_upload(request: Request) -> tuple[bytes, str]:
    """Fichier reçu (multipart `file` ou corps brut), refusé au-delà de la taille maximale."""
    limit = _max_bytes()
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit + 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (max {limit // 2**20} Mo)")

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            raise HTTPException(status_code=422, detail="Champ « file » attendu (multipart/form-data)")
        raw = await upload.read(limit + 1)
        name = upload.filename or "fichier.xml"
    else:
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (max {limit // 2**20} Mo)")
            chunks.append(chunk)
        raw, name = b"".join(chunks), "corps de la requête"
    if len(raw) > limit:
        raise HTTPException(status_code=413, detail=f"Fichier trop volumineux (max {limit // 2**20} Mo)")
    if not raw.strip():
        raise HTTPException(status_code=422, detail="Fichier vide")
    return raw, name


@app.post("/v1/check")
async def check(
    request: Request,
    serie: list[str] = Query(default=[], description="Restreindre la référence à ces séries."),
    operation: list[str] = Query(default=[], description="Restreindre la référence à ces opérations."),
    exclude_operation: list[str] = Query(
        default=[], description="Ignorer les listes propres à ces opérations (re-contrôle)."
    ),
    format: str = Query("json", pattern="^(json|csv)$"),
) -> Response:
    """Contrôle un fichier DDI contre l'index de référence."""
    raw, name = await _read_upload(request)
    index = (await run_in_threadpool(_holder.get)).restrict(
        series=serie, operations=operation, exclude_operations=exclude_operation,
    )
    try:
        report = await run_in_threadpool(check_bytes, raw, name, index)
    except Exception as exc:  # XML invalide, DDI inattendu
        raise HTTPException(status_code=422, detail=f"Fichier DDI illisible : {exc}")
    if format == "csv":
        return PlainTextResponse(to_csv(report), media_type="text/csv; charset=utf-8")
    return Response(json.dumps(report, ensure_ascii=False), media_type="application/json")


def main() -> None:
    """Entrée console `scrubber-check-api`."""
    import uvicorn

    uvicorn.run(
        "metadata_scrubber.check.api:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )
