"""upload_service.py — Upload de fichiers vers S3 (MinIO SSP Cloud).

Réutilise la logique de pipeline_runner.py avec une interface API propre.
"""

from __future__ import annotations

import io
import logging
import os
import traceback
from pathlib import Path

logger = logging.getLogger(__name__)


def upload_file_to_s3(
    local_path: str,
    s3_base: str,
    filename: str | None = None,
    capture: io.StringIO | None = None,
) -> str | None:
    """Upload un fichier local vers S3.

    Args:
        local_path: chemin local du fichier.
        s3_base: URL S3 de destination (ex. s3://bucket/output/).
        filename: nom du fichier dans S3 (optionnel, extrait de local_path si omitted).
        capture: capture de logs optionnelle.

    Returns:
        URL S3 du fichier uploadé, ou None en cas d'erreur.
    """
    if filename is None:
        filename = Path(local_path).name
        if not filename:
            logger.error("Impossible d'extraire le nom du fichier: %s", local_path)
            return None

    def _log(msg: str) -> None:
        logger.info("[upload] %s", msg)

    try:
        from scrubber.s3 import make_s3_filesystem

        s3 = make_s3_filesystem()

        # Vérifier connexion
        try:
            s3.ls("projet-metadonnees-rmes", detail=False)
            _log("Connexion S3 OK")
        except Exception as e:
            _log(f"Connexion S3 échouée: {e}")
            return s3

        # Construire la destination
        s3_key = s3_base.replace("s3://", "")
        dest_key = f"{s3_key.rstrip('/')}/{filename}"

        _log(f"source: {local_path}")
        _log(f"destination: s3://{dest_key}")
        _log(f"taille: {os.path.getsize(local_path)} octets")

        s3.upload(local_path, dest_key)

        # Vérifier
        exists = s3.exists(dest_key)
        _log(f"vérifié sur S3: {'OK' if exists else 'NOK'}")

        return f"s3://{dest_key}"

    except Exception as exc:
        _log(f"Upload échoué: {exc}")
        traceback.print_exc()
        return None
