"""quality.py — Codes d'échappement et listes suspectes.

Codes d'échappement : valeurs non métier (sans objet, non réponse, non
concerné…) souvent ajoutées directement aux listes de codes au lieu d'un
`MissingCodeRepresentation`. Les reconnaître permet de comparer les listes sur
leurs seuls codes métier.

Listes suspectes (`quality_flags`) :
  - placeholder : la liste renvoie ailleurs au lieu de lister ses codes
    (« Voir dictionnaire des nomenclatures », « Code quartier du QVA »,
    valeurs `cf` ou `xxxxxxx`) ;
  - numeric_range : représentation numérique mise en liste (« 0 à 7 »,
    « 00 à 23 », « de 0 à N ») ;
  - single_code : un seul code métier.

Les règles portent sur le texte normalisé (minuscules, espaces) et sont
ancrées en début de libellé : « Non concerné par le zonage » est un code
d'échappement, « Contrat à durée indéterminée » non.
"""

from __future__ import annotations

import re

from .normalize import normalize

_ESCAPE_LABEL = re.compile(
    r"^(sans objet|non concerné|non renseigné|non réponse|non répondu|ne sait pas"
    r"|nsp|refus|indéterminé|hors champ|valeur manquante|non disponible|inconnu"
    r"|secret|non déclaré|non applicable)"
)
_ESCAPE_VALUE = re.compile(r"^(_[a-z]|/|)$")

_PLACEHOLDER_VALUE = re.compile(r"^(cf|x{3,})$")
_PLACEHOLDER_LABEL = re.compile(
    r"^(voir\b|code\b)|cliquer ici|\bsur \d+ positions\b|\bà \d+ caractères\b"
)
_NUMERIC_RANGE = re.compile(r"^(de\s+)?\d+\s*(à|a|-)\s*(\d+|n)\b")

PLACEHOLDER = "placeholder"
NUMERIC_RANGE = "numeric_range"
SINGLE_CODE = "single_code"


def is_escape_code(value: str, label: str) -> bool:
    """Vrai si (valeur, libellé) ressemble à un code d'échappement."""
    v, lab = normalize(value), normalize(label)
    return bool(_ESCAPE_VALUE.match(v) and lab) or bool(_ESCAPE_LABEL.match(lab))


def split_codes(
    codes: list[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Sépare les codes en (métier, échappement)."""
    business, escape = [], []
    for value, label in codes:
        (escape if is_escape_code(value, label) else business).append((value, label))
    return business, escape


def quality_flags(codes: list[tuple[str, str]]) -> list[str]:
    """Indicateurs de liste suspecte (voir l'en-tête du module)."""
    business, _ = split_codes(codes)
    flags = []
    if any(
        _PLACEHOLDER_VALUE.match(normalize(v)) or _PLACEHOLDER_LABEL.search(normalize(lab))
        for v, lab in business
    ):
        flags.append(PLACEHOLDER)
    if len(business) <= 2 and any(
        _NUMERIC_RANGE.match(normalize(v)) or _NUMERIC_RANGE.match(normalize(lab))
        for v, lab in business
    ):
        flags.append(NUMERIC_RANGE)
    if len(business) == 1:
        flags.append(SINGLE_CODE)
    return flags


def name_key(*names: str) -> str:
    """Nom de liste normalisé pour comparer des listes « placeholder » entre elles.

    `N-categorie_juridique` et `N-CATEGORIE-JURIDIQUE` donnent la même clé.
    """
    for name in names:
        key = re.sub(r"[\s_\-]+", " ", normalize(name)).strip()
        if key:
            return key
    return ""
