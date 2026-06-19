"""Normalisation et calcul des signatures de listes de codes.

Phase 1 = égalité exacte de chaînes. On calcule trois signatures :

- ``sig_pairs``  (PRIMAIRE, clé de fusion) : hash de l'ensemble des paires
  ``(valeur, libellé)`` normalisées, trié et dédupliqué.
- ``sig_values`` (auxiliaire) : hash des valeurs seules. Capte « mêmes codes,
  libellé différent » → candidats proches pour la phase 2.
- ``sig_name``   (auxiliaire) : nom de la liste normalisé. Triage uniquement.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from .model import CodeListRecord

_WS = re.compile(r"\s+")

# Séparateurs non imprimables pour éviter toute collision de concaténation.
_UNIT = "\x1f"  # entre valeur et libellé
_REC = "\x1e"  # entre paires


def normalize(text: str | None) -> str:
    """NFC, suppression des espaces de bord, espaces multiples → un seul, minuscule."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = _WS.sub(" ", text).strip()
    return text.lower()


def _hash(parts: list[str]) -> str:
    h = hashlib.sha256()
    h.update(_REC.join(parts).encode("utf-8"))
    return h.hexdigest()


def compute_signatures(record: CodeListRecord) -> None:
    """Calcule et affecte les trois signatures sur ``record`` (mutation en place)."""
    pairs = sorted(
        {
            normalize(c.value) + _UNIT + normalize(c.label)
            for c in record.codes
        }
    )
    values = sorted({normalize(c.value) for c in record.codes})

    record.sig_pairs = _hash(pairs)
    record.sig_values = _hash(values)
    record.sig_name = _hash([normalize(record.name)])
