"""pipeline_runner.py — Wrapper du pipeline pour exécution depuis Streamlit.

Gère :
- Lancement asynchrone (thread + queue) pour Streamlit non-bloquant
- Upload des outputs vers S3 si nécessaire
- Retour d'un objet PipelineResult sérialisable
"""

from __future__ import annotations

import io
import os
import queue
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Capture de logs thread-safe pour le mode async
# ---------------------------------------------------------------------------


class _LogCapture(io.StringIO):
    """Capture stdout / stderr pendant l'exécution du pipeline."""

    def write(self, s: str) -> int:
        sys.__stdout__.write(s)  # write to original stdout, NOT print()
        sys.__stdout__.flush()
        return super().write(s)


# ---------------------------------------------------------------------------
# Résultat & pipeline base
# ---------------------------------------------------------------------------

@dataclass
class PipelineResult:
    """Résultat sérialisable d'une exécution de pipeline."""

    status: str  # "success" | "error"
    output_files: dict[str, str] | None = None
    logs: str = ""
    duration_seconds: float = 0.0
    error_message: str | None = None


def _run_pipeline_internal(
    xml_source: str,
    local_tmp: str,
    run_llm: bool,
    verbose: bool,
    capture: _LogCapture,
) -> PipelineResult:
    """Logique interne — peut être réutilisée."""
    print(
        f"[scrubber] ========== debut pipeline =========="
        f"\n  xml_source : {xml_source}"
        f"\n  output (local tmp) : {local_tmp}"
        f"\n  run_llm : {run_llm}"
        f"\n  verbose : {verbose}",
        file=capture,
        flush=True,
    )

    try:
        # Exécuter le pipeline
        from metadata_scrubber.main import run_pipeline as main_run_pipeline

        main_run_pipeline(
            xml_source=xml_source,
            audit_dir=local_tmp,
            run_llm=run_llm,
            verbose=verbose,
        )

        # --- Résultat ---
        result = PipelineResult(status="success", output_files={})

        local_path = os.path.join(local_tmp, "codelist_duplicates.json")
        if os.path.isfile(local_path):
            fsize = os.path.getsize(local_path)
            print(f"[scrubber] codelist_duplicates.json: {fsize} octets", file=capture, flush=True)
            result.output_files["codelist_duplicates.json"] = local_path
            print("[scrubber] Registre des doublons généré avec succès", file=capture, flush=True)
        else:
            print("[scrubber] codelist_duplicates.json: ABSENT", file=capture, flush=True)

        # Diagnostic si rien n'a été écrit
        if not result.output_files:
            try:
                actual = os.listdir(local_tmp)
                print(
                    f"[scrubber] WARNING: aucun fichier dans {local_tmp} — "
                    f"fichiers reels : {actual}",
                    file=capture,
                    flush=True,
                )
            except Exception:
                pass

        return result

    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[scrubber] ERREUR : {exc}", file=capture, flush=True)
        print(tb, file=capture, flush=True)
        return PipelineResult(status="error", error_message=str(exc), logs=tb)


# ---------------------------------------------------------------------------
# Logger de progression pour le mode asynchrone
# ---------------------------------------------------------------------------

class _ProgressLogger:
    """Injecte les logs du pipeline dans un callback de progression."""

    def __init__(self, callback, phase_map=None):
        self.callback = callback
        self.phase_map = phase_map or {
            1: 0.10, 2: 0.15, 3: 0.20, 4: 0.30, 5: 0.45,
            6: 0.55, 7: 0.70, 8: 0.80, 9: 0.95,
        }
        self._current_phase = 0

    def _detect_phase(self, line):
        """Détecte la phase courante depuis le message '[N/9]'.
        """
        stripped = line.strip()
        for i in range(1, 10):
            if f"[{i}/9]" in stripped:
                return i
        return None

    def log(self, message, is_phase_update=False):
        """Ajoute un log et met à jour la progression si nécessaire."""
        line = str(message)

        if is_phase_update:
            phase = self._detect_phase(line)
            if phase and phase != self._current_phase:
                self._current_phase = phase
                progress = self.phase_map.get(phase, 0.5)
                short_msg = line.replace("[", "").replace("]", "")[:40]
                self.callback("phase", short_msg, progress, phase)
                return
            # Pas de changement de phase — on ne met pas à jour la progression
            # mais on ajoute tout de même le log (ex: logs intermédiaires)

        # Log simple (pas de mise à jour de progress) — on l'ajoute quand même
        self.callback("log", line, None, None)


def run_pipeline_async(
    xml_source: str,
    output_base: str,
    run_llm: bool = True,
    verbose: bool = False,
) -> tuple[threading.Thread, queue.Queue]:
    """Lance le pipeline dans un thread séparė.

    Le thread place les messages dans une ``queue.Queue`` que le thread
    principal Streamlit peut poller a chaque rerun.  Jamais de mutation
    de *session_state* depuis le thread secondaire.

    Returns:
        Un tuple ``(thread, msg_queue)``.
    """
    use_s3 = output_base.startswith("s3://") if output_base else False
    local_tmp = tempfile.mkdtemp(prefix="scrubber_out_")
    msg_queue: queue.Queue = queue.Queue(maxsize=2048)

    def _progress(type_: str, msg: object, prog: float | None, phase: int | None):
        """Appelé par _ProgressLogger et les callbacks finaux — passe les messages a la queue."""
        try:
            msg_queue.put((type_, msg, prog, phase), timeout=1.0)
        except queue.Full:
            pass  # on perd des logs en cas de surcharge, ce n'est pas critique

    def _target():
        # Callback de progression pour la progress-bar UI
        pl = _ProgressLogger(_progress)

        # Phase 0 : lancement
        _progress("phase", "Lancement du pipeline…", 0.0, 0)

        capture = _LogCapture()
        old_stdout, old_stderr = sys.stdout, sys.stderr

        # Rediriger stdout vers le logger
        class _Redirector(io.StringIO):
            """Redirige vers le logger de progression."""

            def write(self, s: str) -> int:
                pl.log(s, is_phase_update=True)
                # Garde les logs internes dans capture aussi
                return super().write(s)

            def flush(self):
                pass  # _Redirector inherits from StringIO; parent flush is sufficient

        sys.stdout = _Redirector()
        sys.stderr = capture

        start = time.time()
        try:
            _run_pipeline_internal(xml_source, local_tmp, run_llm, verbose, capture)
            duration = time.time() - start

            # Fusionner les logs
            internal_logs = capture.getvalue()

            # Upload S3 si nécessaire
            output_files = {}
            output_files_s3 = {}
            local_path = os.path.join(local_tmp, "codelist_duplicates.json")
            if os.path.isfile(local_path):
                output_files["codelist_duplicates.json"] = local_path
                print(f"[scrubber] codelist_duplicates.json: {os.path.getsize(local_path)} octets",
                      file=capture, flush=True)

            if use_s3 and output_files:
                for fname, lpath in output_files.items():
                    s3_path = _upload_file_to_s3(lpath, output_base, fname, capture)
                    if s3_path:
                        output_files_s3[fname] = s3_path

            if output_files_s3:
                output_files = output_files_s3

            logs = internal_logs or f"(aucun log — durée: {duration:.1f}s)"

            result = PipelineResult(
                status="success",
                output_files=output_files,
                logs=logs,
                duration_seconds=duration,
            )

            _progress("done", f"Terminé en {duration:.1f}s", 1.0, 9)
            _progress("result", result, None, None)

        except Exception as exc:
            tb = traceback.format_exc()
            duration = time.time() - start
            logs = tb
            _progress("error", str(exc), None, None)
            _progress("result", PipelineResult(status="error", error_message=str(exc), logs=logs,
                                                        duration_seconds=duration), None, None)
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    return t, msg_queue


def _upload_file_to_s3(
    local_path: str,
    s3_base: str,
    filename: str,
    capture: _LogCapture | None = None,
    output: io.TextIOBase | None = None,
) -> str | None:
    """Upload un fichier local vers S3. Retourne la clé S3 ou None."""
    import s3fs

    def _log(msg: str) -> None:
        print(f"[upload] {msg}", flush=True)
        if capture:
            print(f"[upload] {msg}", file=capture, flush=True)

    endpoint = os.environ.get("AWS_S3_ENDPOINT", "minio.lab.sspcloud.fr")
    # s3fs exige un endpoint URL complet avec schéma
    if not endpoint.startswith("http://") and not endpoint.startswith("https://"):
        endpoint = "https://" + endpoint
    key = os.environ.get("AWS_ACCESS_KEY_ID", "")
    secret = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
    token = os.environ.get("AWS_SESSION_TOKEN", "")

    _log(f"endpoint={endpoint}")
    _log(f"key prefix={key[:8] if key else 'NONE'}")

    if not key or not secret:
        _log("Pas de credentials S3 — upload sauté")
        return None

    try:
        s3 = s3fs.S3FileSystem(
            endpoint_url=endpoint,
            key=key,
            secret=secret,
            token=token or None,
        )

        # Vérifier connexion
        try:
            s3.ls("projet-metadonnees-rmes", detail=False)
            _log("Connexion S3 OK (bucket vérifié)")
        except Exception as e:
            _log(f"Connexion S3 KO: {e}")
            return None

        # Construire la destination
        s3_key = s3_base.replace("s3://", "")
        dest_key = s3_key.rstrip("/") + "/" + filename

        _log(f"source local : {local_path}")
        _log(f"destination S3 : {dest_key}")
        _log(f"taille fichier : {os.path.getsize(local_path)} octets")

        s3.upload(local_path, dest_key)

        # Vérifier
        try:
            exists = s3.exists(dest_key)
            _log(f"fichier vérifié sur S3 : {'OK' if exists else 'NOK'}")
        except Exception:
            pass

        return f"s3://{dest_key}"

    except Exception as exc:
        _log(f"Upload échoué : {exc}")
        traceback.print_exc()
        return None
