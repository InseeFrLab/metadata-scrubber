"""Mesures de similarité textuelle — code porté de l'archive.

Repris de ``archives/dedoub_deterministe/concepts_variables_alignment.py``
(``normalize`` l.57, ``text_similarity`` l.66, ``jaccard`` l.72,
``concat_codelist_text`` l.392). Utilisé pour la couche **quasi-doublons** :
le scoring flou n'est appliqué qu'à des paires candidates bornées par buckets
(cf. ``dedup.near_duplicate_pairs``).

NB : ce ``normalize`` (suppression de la ponctuation) est volontairement distinct
de ``signature.normalize`` (NFC, conservation de la ponctuation) — l'un sert au
scoring flou, l'autre aux signatures exactes.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from collections.abc import Iterable

_PUNCT = re.compile(r"[^\w\s]")
_WS = re.compile(r"\s+")


def normalize(txt: str | None) -> str:
    """Minuscule, ponctuation → espace, espaces multiples réduits (version archive)."""
    if not txt:
        return ""
    txt = txt.lower()
    txt = _PUNCT.sub(" ", txt)
    txt = _WS.sub(" ", txt)
    return txt.strip()


def text_similarity(a: str, b: str) -> float:
    """Ratio ``SequenceMatcher`` sur texte normalisé (0.0 si vide)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def jaccard(a: str, b: str) -> float:
    """Similarité Jaccard sur les tokens normalisés — repli peu coûteux."""
    sa = set(normalize(a).split())
    sb = set(normalize(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def concat_codelist_text(
    name: str, description: str, pairs: Iterable[tuple[str, str | None]]
) -> str:
    """Concatène nom + description + ``value``/``label`` de chaque paire.

    Esprit de ``concat_codelist_text`` de l'archive, adapté à notre modèle
    (les libellés viennent des catégories résolues).
    """
    parts: list[str] = []
    if name:
        parts.append(name)
    if description:
        parts.append(description)
    for value, label in pairs:
        if value:
            parts.append(value)
        if label:
            parts.append(label)
    return " ".join(parts)
