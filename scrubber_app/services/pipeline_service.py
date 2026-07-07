"""pipeline_service.py — Orchestration du pipeline dans un thread séparė.

Ce service s'occupe d'exécuter le pipeline metadonnées-scrubber dans un thread
et d'injecter les événements de progression dans le JobManager (SSE).
"""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import traceback
from typing import Any

from services.job_manager import JobManager, JobStatus, job_manager


class _ProgressLogger:
    """Détecte la phase courante depuis le message '[N/9]' et injecte progress."""

    def __init__(self, progress):
        self.progress = progress
        self.phase_map = {
            1: 0.10, 2: 0.15, 3: 0.20, 4: 0.30, 5: 0.45,
            6: 0.55, 7: 0.70, 8: 0.80, 9: 0.95,
        }
        self._current_phase = 0

    def _detect_phase(self, line):
        """Détecte la phase courante depuis le message '[N/9]'."""
        stripped = line.strip()
        for i in range(1, 10):
            if f"[{i}/9]" in stripped:
                return i
        return None

    def log(self, message=None, is_phase_update=False):
        """Ajoute un log et met à jour la progression si nécessaire."""
        line = str(message) if message is not None else ""

        if is_phase_update:
            phase = self._detect_phase(line)
            if phase and phase != self._current_phase:
                self._current_phase = phase
                progress_val = self.phase_map.get(phase, 0.5)
                short_msg = line.replace("[", "").replace("]", "")[:40]
                self.progress("phase", short_msg, progress_val, phase)
                return
            # Pas de changement de phase — on ne met pas à jour la progression
            # mais on ajoute tout de même le log (ex: logs intermédiaires)

        # Log simple (pas de mise à jour de progress — on l'ajoute quand même
        self.progress("log", line, None, None)


def _run_pipeline_internal(
    xml_source: str,
    local_tmp: str,
    run_llm: bool,
    verbose: bool,
    capture,
    progress,
) -> dict[str, Any]:
    """Logique interne — peut être réutilisée."""
    print(
        f"[scrubber] ========== debut pipeline =========="
        f"\n  xml_source: {xml_source}"
        f"\n  output (local tmp): {local_tmp}"
        f"\n  run_llm: {run_llm}"
        f"\n  verbose: {verbose}",
        file=capture,
        flush=True,
    )

    try:
        # Injector les chemins d'imports
        _scrub_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.normpath(os.path.join(_scrub_dir, ".."))
        _src = os.path.join(_scrub_dir, "..", "src")
        _src = os.path.normpath(_src)
        for p in [_project_root, _src]:
            if p not in sys.path:
                sys.path.insert(0, p)

        # Execute the pipeline
        from main import run_pipeline as main_run_pipeline

        main_run_pipeline(
            xml_source=xml_source,
            audit_dir=local_tmp,
            run_llm=run_llm,
            verbose=verbose,
        )

        # --- Result ---
        output_files = {}

        local_path = os.path.join(local_tmp, "codelist_duplicates.json")
        if os.path.isfile(local_path):
            fsize = os.path.getsize(local_path)
            print(f"[scrubber] codelist_duplicates.json: {fsize} octets", file=capture, flush=True)
            output_files["codelist_duplicates.json"] = local_path
            print("[scrubber] Registre des doublons genere avec succes", file=capture, flush=True)
        else:
            print("[scrubber] codelist_duplicates.json: ABSENT", file=capture, flush=True)

        # Diagnostic if nothing was written
        if not output_files:
            try:
                actual = os.listdir(local_tmp)
                print(
                    f"[scrubber] WARNING: aucun fichier dans {local_tmp} — "
                    f"fichiers reels: {actual}",
                    file=capture,
                    flush=True,
                )
            except Exception:
                pass

        return {"status": "success", "output_files": output_files}

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[scrubber] ERREUR: {exc}", file=capture, flush=True)
        print(tb, file=capture, flush=True)
        return {
            "status": "error",
            "error_message": str(exc),
            "logs": tb,
        }


def _run_pipeline_in_thread(
    job_id: str,
    job_manager_instance: JobManager,
    xml_source: str,
    audit_dir: str,
    run_llm: bool,
    verbose: bool,
) -> None:
    """Target du thread qui execute le pipeline et publie sur SSE."""

    def _progress(type_: str, msg: object, prog: float | None, phase: int | None) -> None:
        msg_str = str(msg)
        if type_ == "phase":
            phase_label = {
                0: "Lancement...",
                1: "Lecture & parsing XML...",
                2: "Extraction CodeLists...",
                3: "Extraction variables...",
                4: "Signature de contenu...",
                5: "Detection exacte...",
                6: "Detection floue...",
                7: "Signaux d'usage...",
                8: "Detection semantics...",
                9: "Generation du registre...",
            }.get(phase, f"Phase {phase}") if phase else ""
            job_manager_instance.update_job(
                job_id=job_id,
                status=JobStatus.RUNNING,
                progress=prog,
                phase=phase,
                phase_label=phase_label,
                current_log=msg_str[:200],
            )
            job_manager_instance.jobs[job_id].broadcast("progress", {
                "progress": prog or 0.0,
                "phase": phase,
                "phase_label": phase_label,
                "message": msg_str[:200],
            })
        elif type_ == "log":
            job_manager_instance.jobs[job_id].add_log(msg_str[:500])
            job_manager_instance.jobs[job_id].broadcast("log", {"message": msg_str[:500]})
        elif type_ == "done":
            job_manager_instance.jobs[job_id].broadcast("progress", {
                "progress": 1.0,
                "message": msg_str[:200],
            })
        elif type_ == "error":
            job_manager_instance.jobs[job_id].add_log(msg_str[:500])
            job_manager_instance.jobs[job_id].broadcast("log", {"message": msg_str[:500]})
            job_manager_instance.set_error(job_id, msg_str[:800])
        elif type_ == "result":
            job_manager_instance.set_result(job_id, msg or {})

    # Phase 0 — lancement
    _progress("phase", "Lancement du pipeline...", 0.0, 0)

    capture = io.StringIO()
    old_stdout, old_stderr = sys.stdout, sys.stderr

    # Rediriger stdout vers le logger de progression
    pl = _ProgressLogger(_progress)

    class _Redirector(io.StringIO):
        def write(self, s: str) -> int:
            pl.log(s, is_phase_update=True)
            return super().write(s)

        def flush(self) -> None:
            pass

    sys.stdout = _Redirector()
    sys.stderr = capture

    start = time.time()
    try:
        result = _run_pipeline_internal(
            xml_source, audit_dir, run_llm, verbose, capture, _progress,
        )
        duration = time.time() - start
        result["duration_seconds"] = duration

        # Logger dans capture aussi
        internal_logs = capture.getvalue()
        if internal_logs:
            for line in internal_logs.splitlines():
                job_manager_instance.jobs[job_id].add_log(line[:500])

        _progress("done", f"Termine en {duration:.1f}s", 1.0, 9)
        job_manager_instance.set_result(job_id, result)

    except Exception as exc:
        tb = traceback.format_exc()
        duration = time.time() - start
        _progress("error", str(exc), None, None)
        job_manager_instance.set_error(job_id, str(exc))
        # Logger le traceback
        for line in tb.splitlines():
            job_manager_instance.jobs[job_id].add_log(line[:500])
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr


def start_pipeline_job(
    xml_source: str,
    audit_dir: str,
    run_llm: bool = True,
    verbose: bool = False,
) -> str:
    """Creer un job de pipeline et le lancer dans un thread separe.

    Returns:
        L'identifiant unique du job (job_id).
    """
    job_id = job_manager.create_job()

    t = threading.Thread(
        target=_run_pipeline_in_thread,
        args=(job_id, job_manager, xml_source, audit_dir, run_llm, verbose),
        daemon=True,
        name="pipeline-worker",
    )
    t.start()

    return job_id
