"""levels.py — Niveaux de CodeLists et choix de la liste maître.

Trois niveaux cibles :
  - officielle : nomenclature officielle (COG, NAF, PCS…) ;
  - commune    : liste standard gérée par la division métadonnées, partagée ;
  - specifique : liste propre à une enquête (une série d'opérations).

Le niveau validé vient du registre nettoyé (`CodeList.level`). À défaut, un
niveau est seulement *suggéré* d'après la provenance (source API) : une liste
utilisée par au moins deux séries est candidate « commune ».
"""

from __future__ import annotations

from .types import CodeList

OFFICIELLE, COMMUNE, SPECIFIQUE = "officielle", "commune", "specifique"
LEVELS = (OFFICIELLE, COMMUNE, SPECIFIQUE)
LEVEL_RANK = {OFFICIELLE: 0, COMMUNE: 1, SPECIFIQUE: 2, "": 3}


def suggest_level(cl: CodeList) -> str:
    """Niveau validé s'il existe, sinon niveau suggéré ("" sans information)."""
    if cl.level:
        return cl.level
    if cl.origin == "api-geo":
        return OFFICIELLE
    if len(cl.series) >= 2:
        return COMMUNE
    return SPECIFIQUE if cl.series else ""


def master_rank(cl: CodeList, preferred: set[str] = frozenset()) -> tuple:
    """Clé de tri : la première liste d'un groupe de doublons en devient la maître.

    Ordre : entrées du registre nettoyé, niveau validé (officielle d'abord),
    maître choisie par l'expert lors d'un run précédent, niveau suggéré, nombre
    de séries puis de variables utilisatrices. Un choix de l'expert l'emporte
    donc sur une simple suggestion. Le tri étant stable, l'ordre du document
    départage.
    """
    return (
        cl.origin != "registry",
        LEVEL_RANK.get(cl.level, 3),
        cl.id not in preferred,
        LEVEL_RANK.get(suggest_level(cl), 3),
        -len(cl.series),
        -len(set(cl.vars)),
    )


# Mots sans valeur pour rapprocher un nom de liste d'une nomenclature
_STOP_WORDS = frozenset({
    "code", "codes", "liste", "des", "les", "du", "de", "la", "le", "et", "cog", "insee",
    "nomenclature", "n",
})


def _name_tokens(text: str) -> set[str]:
    """Mots d'un nom de liste, sans accents ni pluriel : « N_Départements » → {"departement"}."""
    import re
    import unicodedata

    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    words = re.split(r"[^a-z]+", text)
    return {w.rstrip("sx") for w in words if len(w) >= 2 and w not in _STOP_WORDS and not w.isdigit()}


def suggest_official(cl: CodeList, officials: list[CodeList]) -> CodeList | None:
    """Nomenclature officielle que désigne une liste « placeholder » (« Voir dictionnaire… »).

    Rapproche les mots du nom de la liste de ceux des nomenclatures officielles :
    « N-region » → « Régions (COG) ». Au moins la moitié des mots du nom doivent
    se retrouver dans celui de la nomenclature ; None sinon.
    """
    tokens = _name_tokens(cl.name or cl.label)
    if not tokens:
        return None
    best, best_score = None, 0.0
    for official in officials:
        score = len(tokens & _name_tokens(official.name)) / len(tokens)
        if score > best_score:
            best, best_score = official, score
    return best if best_score >= 0.5 else None
