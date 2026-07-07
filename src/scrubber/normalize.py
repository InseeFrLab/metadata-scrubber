"""Utilitaires de normalisation et signatures."""

from __future__ import annotations

import re
import unicodedata


def normalize(txt: str | None) -> str:
    """Normalise un texte : NFC, minuscule, espaces multiples → espace unique, trim."""
    txt = txt or ""
    txt = unicodedata.normalize("NFC", txt)
    txt = re.sub(r"\s+", " ", txt).strip().lower()
    return txt


def signature_from_codes(codes: list[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """
    Signature de contenu : ensemble trié et dédupliqué des paires (valeur, libellé).

    Le nom/identifiant de la liste est volontairement exclu (sinon deux listes
    identiques nommées différemment ne seraient pas reconnues).
    """
    return tuple(sorted({(normalize(v), normalize(label)) for v, label in codes}))


def concat_text(
    name: str,
    label: str,
    codes: list[tuple[str, str]],
) -> str:
    """
    Concatène nom, label et codes en un seul texte normalisé.

    Utilisé pour les comparaisons floues et les embeddings.
    """
    parts = [name, label]
    parts += [f"{normalize(v)}: {normalize(label)}" for v, label in codes]
    return normalize(" ".join(parts))
