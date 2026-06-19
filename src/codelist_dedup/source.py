"""Acquisition de la source : fichier local ou objet S3 (flux, sans fichier temp)."""

from __future__ import annotations

import contextlib
import subprocess
import sys
from collections.abc import Iterator
from typing import BinaryIO


@contextlib.contextmanager
def open_source(source: str) -> Iterator[BinaryIO]:
    """Ouvre ``source`` en flux binaire.

    - ``s3://...`` → ``aws s3 cp s3://... -`` (stdout piped, pas de fichier temp).
    - sinon → fichier local.
    """
    if source.startswith("s3://"):
        proc = subprocess.Popen(
            ["aws", "s3", "cp", source, "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proc.stdout is not None
        try:
            yield proc.stdout
        finally:
            # vide le pipe et attend la fin pour récupérer un éventuel code d'erreur
            proc.stdout.close()
            err = proc.stderr.read() if proc.stderr else b""
            ret = proc.wait()
            # ne pas masquer une exception déjà en cours (ex. erreur de parsing)
            if ret != 0 and sys.exc_info()[1] is None:
                raise RuntimeError(
                    f"`aws s3 cp {source} -` a échoué (code {ret}): "
                    f"{err.decode('utf-8', 'replace').strip()}"
                )
    else:
        with open(source, "rb") as fh:
            yield fh
