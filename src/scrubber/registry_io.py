"""registry_io.py — Lecture/écriture JSON générique (filesystem local ou S3).

Utilisé pour le registre des doublons (codelist_duplicates.json) et le
registre nettoyé (cleaned_codelists.json), côté pipeline comme côté web app.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _is_s3_path(path: str) -> bool:
    """Retourne True si la chaîne ressemble à un chemin S3."""
    return path.startswith("s3://")


def read_json_registry(path: str) -> dict[str, Any]:
    """Lit un document JSON depuis un chemin local ou S3.

    Args:
        path: chemin local ou URL S3.

    Returns:
        Le document sous forme de dict.

    Raises:
        FileNotFoundError: fichier local absent.
        ConnectionError: erreur d'accès S3.
    """
    if _is_s3_path(path):
        from botocore.exceptions import ClientError

        from scrubber.s3 import make_s3_filesystem

        s3 = make_s3_filesystem()
        try:
            with s3.open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except ClientError as e:
            raise ConnectionError(f"Erreur S3 pour {path}: {e}") from e
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Le fichier {path} n'existe pas")
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_registry(doc: dict[str, Any], path: str) -> None:
    """Écrit un document JSON vers un chemin local ou S3.

    Args:
        doc: Le document à écrire.
        path: chemin de destination (local ou S3).
    """
    formatted = json.dumps(doc, indent=2, ensure_ascii=False)
    if _is_s3_path(path):
        from scrubber.s3 import make_s3_filesystem

        s3 = make_s3_filesystem()
        parent = Path(path.replace("s3://", "")).parent
        try:
            s3.makedirs(parent, exist_ok=True)
        except Exception:
            pass  # bucket existe probablement déjà
        with s3.open(path, "w", encoding="utf-8") as f:
            f.write(formatted)
        logger.info("Document écrit sur S3: %s", path)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(formatted)
        logger.info("Document écrit en local: %s", path)
