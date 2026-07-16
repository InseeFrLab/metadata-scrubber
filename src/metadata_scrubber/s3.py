"""s3.py — Création du filesystem S3 (MinIO) avec la bonne source de credentials.

Sur Onyxia, les credentials injectés en variables d'environnement (AWS_ACCESS_KEY_ID,
AWS_SESSION_TOKEN...) expirent au bout de quelques heures, alors que le profil
`default` de ~/.aws/credentials est renouvelé par l'utilisateur. La chaîne de
credentials standard de boto donne la priorité aux variables d'environnement,
ce qui provoque des erreurs 403 avec des credentials périmés. On privilégie
donc explicitement le profil `default` quand il existe.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

import s3fs

_DEFAULT_ENDPOINT = "https://minio.lab.sspcloud.fr"


def _default_profile_exists() -> bool:
    """True si un profil [default] existe dans ~/.aws/credentials."""
    creds_path = Path(
        os.environ.get("AWS_SHARED_CREDENTIALS_FILE", "")
        or Path.home() / ".aws" / "credentials"
    )
    if not creds_path.is_file():
        return False
    parser = configparser.ConfigParser()
    try:
        parser.read(creds_path)
    except configparser.Error:
        return False
    return parser.has_section("default")


def make_s3_filesystem() -> s3fs.S3FileSystem:
    """Filesystem S3 pointé sur l'endpoint MinIO.

    Utilise le profil AWS `default` (~/.aws/credentials) s'il existe,
    sinon la chaîne de credentials standard (variables d'environnement...).
    """
    endpoint = os.environ.get("AWS_S3_ENDPOINT", _DEFAULT_ENDPOINT)
    if not endpoint.startswith(("http://", "https://")):
        endpoint = "https://" + endpoint

    kwargs: dict = {
        "endpoint_url": endpoint,
        "client_kwargs": {
            "region_name": os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
        },
    }
    if _default_profile_exists():
        kwargs["profile"] = "default"
    return s3fs.S3FileSystem(**kwargs)
