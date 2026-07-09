"""pipeline_service.py — Orchestration du pipeline dans un thread séparė.

Ce service s'occupe d'exécuter le pipeline metadonnées-scrubber dans un thread
et d'injecter les événements de progression dans le JobManager (SSE).
"""

from __future__ import annotations

import io
import logging
import os
import sys
import threading
import time
import traceback
from typing import Any

from services.job_manager import JobManager, JobStatus, job_manager

logger = logging.getLogger(__name__)


class _Tee(io.StringIO):
    """Bufferise en mémoire et recopie vers le flux original (console serveur)."""

    def __init__(self, original):
        super().__init__()
        self._original = original

    def write(self, s: str) -> int:
        try:
            self._original.write(s)
            self._original.flush()
        except Exception:
            pass
        return super().write(s)


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
    registry_path: str | None = None,
) -> dict[str, Any]:
    """Logique interne — peut être réutilisée."""
    print(
        f"[scrubber] ========== debut pipeline =========="
        f"\n  xml_source: {xml_source}"
        f"\n  output (local tmp): {local_tmp}"
        f"\n  run_llm: {run_llm}"
        f"\n  verbose: {verbose}"
        f"\n  registre nettoye: {registry_path or '—'}",
        file=capture,
        flush=True,
    )

    try:
        # Injecter les chemins d'imports (ce fichier est dans scrubber_app/services/)
        _services_dir = os.path.dirname(os.path.abspath(__file__))
        _project_root = os.path.normpath(os.path.join(_services_dir, "..", ".."))
        _src = os.path.join(_project_root, "src")
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
            registry_path=registry_path,
        )

        # --- Result ---
        output_files = {}

        if local_tmp.startswith("s3://"):
            from scrubber.s3 import make_s3_filesystem

            dest = f"{local_tmp.rstrip('/')}/codelist_duplicates.json"
            fs = make_s3_filesystem()
            if fs.exists(dest):
                fsize = fs.size(dest)
                print(f"[scrubber] codelist_duplicates.json: {fsize} octets (S3)", file=capture, flush=True)
                output_files["codelist_duplicates.json"] = dest
                print("[scrubber] Registre des doublons genere avec succes", file=capture, flush=True)
            else:
                print(f"[scrubber] codelist_duplicates.json: ABSENT de {local_tmp}", file=capture, flush=True)
        else:
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
    registry_path: str | None = None,
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

    old_stdout, old_stderr = sys.stdout, sys.stderr
    capture = _Tee(old_stderr)

    # Rediriger stdout vers le logger de progression (+ copie console)
    pl = _ProgressLogger(_progress)

    class _Redirector(_Tee):
        def write(self, s: str) -> int:
            if s.strip():  # ignorer les écritures vides ("\n" de print)
                pl.log(s, is_phase_update=True)
            return super().write(s)

    sys.stdout = _Redirector(old_stdout)
    sys.stderr = capture

    start = time.time()
    try:
        result = _run_pipeline_internal(
            xml_source, audit_dir, run_llm, verbose, capture, _progress,
            registry_path=registry_path,
        )
        duration = time.time() - start
        result["duration_seconds"] = duration

        # Logger dans capture aussi
        internal_logs = capture.getvalue()
        if internal_logs:
            for line in internal_logs.splitlines():
                job_manager_instance.jobs[job_id].add_log(line[:500])

        if result.get("status") == "error":
            # Échec interne capturé par _run_pipeline_internal : ne pas
            # marquer le job en succès.
            err_msg = result.get("error_message", "Erreur pipeline")
            _progress("error", err_msg, None, None)
            logger.error("Job %s échoué: %s", job_id, err_msg)
        else:
            _progress("done", f"Termine en {duration:.1f}s", 1.0, 9)
            job_manager_instance.set_result(job_id, result)
            logger.info("Job %s terminé en %.1fs", job_id, duration)

    except Exception as exc:
        tb = traceback.format_exc()
        duration = time.time() - start
        _progress("error", str(exc), None, None)
        job_manager_instance.set_error(job_id, str(exc))
        logger.error("Job %s échoué: %s", job_id, exc)
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
    registry_path: str | None = None,
) -> str:
    """Creer un job de pipeline et le lancer dans un thread separe.

    Returns:
        L'identifiant unique du job (job_id).
    """
    job_id = job_manager.create_job()
    logger.info(
        "Pipeline job %s démarré — source: %s, sortie: %s", job_id, xml_source, audit_dir
    )

    t = threading.Thread(
        target=_run_pipeline_in_thread,
        args=(job_id, job_manager, xml_source, audit_dir, run_llm, verbose, registry_path),
        daemon=True,
        name="pipeline-worker",
    )
    t.start()

    return job_id
