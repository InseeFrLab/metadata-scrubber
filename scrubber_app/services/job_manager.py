"""Job tracker thread-safe avec broadcast SSE pour tous les listener enregistrés."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    ERROR = "error"


def _safe_put(queue: asyncio.Queue, msg: dict) -> None:
    """Dépose un message dans la queue d'un client SSE (exécuté dans la boucle asyncio)."""
    try:
        queue.put_nowait(msg)
    except asyncio.QueueFull:
        pass  # client trop lent → on droppe l'événement


@dataclass
class Job:
    """État d'un pipeline job."""
    job_id: str
    status: JobStatus = JobStatus.PENDING
    progress: float = 0.0
    phase: int | None = None
    phase_label: str = ""
    current_log: str = ""
    logs: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error_message: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    listeners: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)  # historique pour replay SSE
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_log(self, msg: str) -> None:
        self.logs.append(msg)

    def broadcast(self, event: str, data: Any) -> None:
        """Enregistre l'événement et le pousse aux clients SSE connectés.

        Appelé depuis le thread worker : la livraison passe par
        call_soon_threadsafe pour réveiller la boucle asyncio
        (asyncio.Queue.put_nowait seul ne réveille pas epoll depuis
        un autre thread).
        """
        msg = {"event": event, "data": data}
        with self.lock:
            self.events.append(msg)
            for listener in list(self.listeners):
                queue = listener.get("queue")
                loop = listener.get("loop")
                if queue is None or loop is None:
                    continue
                try:
                    loop.call_soon_threadsafe(_safe_put, queue, msg)
                except RuntimeError:
                    pass  # boucle fermée → listener obsolète


# Singleton global
class JobManager:
    """Gestionnaire global des jobs — unique par process."""

    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}

    def create_job(self) -> str:
        job_id = uuid.uuid4().hex[:12]
        self.jobs[job_id] = Job(job_id=job_id)
        return job_id

    def get_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def update_job(
        self, job_id: str, status: JobStatus | None,
        progress: float | None, phase: int | None,
        phase_label: str | None, current_log: str | None,
    ) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            logger.warning("update_job: job %s not found", job_id)
            return
        if status is not None:
            job.status = status
        if progress is not None:
            job.progress = progress
        if phase is not None:
            job.phase = phase
        if phase_label:
            job.phase_label = phase_label
        if current_log:
            job.current_log = current_log
            job.add_log(current_log)

    def set_result(self, job_id: str, result: dict[str, Any]) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.result = result
        job.status = JobStatus.SUCCESS
        job.finished_at = time.time()

        data = {
            "status": "success",
            "progress": 1.0,
            "result": result,
        }
        if job.phase_label:
            data["phase_label"] = job.phase_label
        if job.logs:
            data["logs"] = job.logs[-20:]  # 20 derniers logs

        job.broadcast("done", data)

    def set_error(self, job_id: str, error_message: str) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        job.status = JobStatus.ERROR
        job.error_message = error_message
        job.finished_at = time.time()

        data = {"status": "error", "error_message": error_message}
        job.broadcast("done", data)

    def cleanup(self, max_age: float = 3600) -> int:
        """Remove finished jobs older than max_age seconds."""
        now = time.time()
        to_remove = [
            jid for jid, job in self.jobs.items()
            if job.finished_at
            and (now - job.finished_at) > max_age
        ]
        for jid in to_remove:
            del self.jobs[jid]
        return len(to_remove)


# Singleton instance
job_manager = JobManager()
