"""run_output.py — Dossier de sortie daté par exécution et capture des logs.

Chaque exécution du pipeline écrit dans ``<racine>/<AAAAMMJJ-HHMMSS>/`` :
codelist_duplicates.json, run_summary.json et pipeline.log. Le registre nettoyé,
lui, reste à la racine (voir ``cleaned_registry.cleaned_registry_path``).
"""

from __future__ import annotations

import io
import logging
import os
import re
import sys
from datetime import datetime, timezone

from .registry_io import write_text

RUN_ID_FORMAT = "%Y%m%d-%H%M%S"
RUN_DIR_PATTERN = re.compile(r"^\d{8}-\d{6}$")


def make_run_dir(audit_dir: str, now: datetime | None = None) -> tuple[str, str]:
    """Retourne (run_id, run_dir) : sous-dossier daté de la racine de sortie.

    Manipulation de chaîne (pas de pathlib) pour rester compatible S3.
    """
    run_id = (now or datetime.now(timezone.utc)).strftime(RUN_ID_FORMAT)
    return run_id, f"{audit_dir.rstrip('/')}/{run_id}"


def list_run_dirs(audit_dir: str) -> list[str]:
    """Dossiers de run (AAAAMMJJ-HHMMSS) de la racine de sortie, du plus récent au plus ancien.

    Racine absente → liste vide.
    """
    root = audit_dir.rstrip("/")
    if root.startswith("s3://"):
        from .s3 import make_s3_filesystem

        try:
            entries = make_s3_filesystem().ls(root, detail=False)
        except FileNotFoundError:
            return []
        names = [e.rstrip("/").rsplit("/", 1)[-1] for e in entries]
    else:
        if not os.path.isdir(root):
            return []
        names = os.listdir(root)
    runs = sorted((n for n in names if RUN_DIR_PATTERN.match(n)), reverse=True)
    return [f"{root}/{n}" for n in runs]


class _Tee(io.TextIOBase):
    """Écrit dans le flux d'origine ET dans un tampon mémoire."""

    def __init__(self, original, buffer: io.StringIO) -> None:
        self._original = original
        self._buffer = buffer

    def write(self, s: str) -> int:
        self._buffer.write(s)
        return self._original.write(s)

    def flush(self) -> None:
        self._original.flush()

    def isatty(self) -> bool:
        return False


class RunLogCapture:
    """Contexte qui copie stdout, stderr et le logging INFO dans un tampon.

    La sortie d'origine continue de fonctionner (console, redirection de l'UI).
    À la sortie du contexte, le tampon est écrit dans ``log_path`` — même en cas
    d'exception, traceback compris (il est écrit sur stderr par le appelant).
    """

    def __init__(self, log_path: str) -> None:
        self.log_path = log_path
        self.buffer = io.StringIO()
        self._handler: logging.Handler | None = None
        self._root_level: int | None = None

    def __enter__(self) -> "RunLogCapture":
        self._stdout, self._stderr = sys.stdout, sys.stderr
        sys.stdout = _Tee(self._stdout, self.buffer)
        sys.stderr = _Tee(self._stderr, self.buffer)

        self._handler = logging.StreamHandler(self.buffer)
        self._handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root = logging.getLogger()
        self._root_level = root.level
        if root.level == logging.NOTSET or root.level > logging.INFO:
            root.setLevel(logging.INFO)
        root.addHandler(self._handler)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        root = logging.getLogger()
        if self._handler is not None:
            root.removeHandler(self._handler)
        if self._root_level is not None:
            root.setLevel(self._root_level)
        sys.stdout, sys.stderr = self._stdout, self._stderr

        if exc is not None:
            import traceback

            self.buffer.write("".join(traceback.format_exception(exc_type, exc, tb)))
        try:
            write_text(self.buffer.getvalue(), self.log_path)
        except Exception as write_exc:  # ne jamais masquer l'erreur d'origine
            print(f"[scrubber] Impossible d'écrire {self.log_path}: {write_exc}", file=sys.stderr)
