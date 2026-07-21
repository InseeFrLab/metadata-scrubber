"""Pipeline de détection de doublons — entonnoir en trois niveaux."""

from __future__ import annotations

import itertools
from difflib import SequenceMatcher

from .normalize import concat_text
from .types import CodeList


def _group_by_sig(codelists: list[CodeList]) -> dict[tuple, list[CodeList]]:
    """Groupe les codelists par signature de contenu."""
    groups: dict[tuple, list[CodeList]] = {}
    for cl in codelists:
        groups.setdefault(cl.sig, []).append(cl)
    return groups


def detect_exact_duplicates(codelists: list[CodeList]) -> dict[tuple, list[CodeList]]:
    """
    Phase 1 — Doublons exacts.

    Retourne un dictionnaire {signature: liste} pour toutes les signatures
    partagees par >= 2 CodeLists.

    Args:
        codelists: Liste complete de CodeList avec signature calculee.

    Returns:
        Dict de signatures -> groupes de codelists identiques.
    """
    groups = _group_by_sig(codelists)
    return {sig: g for sig, g in groups.items() if len(g) > 1}


def _text_for_dedup(cl: CodeList) -> str:
    """Version normalisee concatenee d'une codelist (nom + label + desc + codes)."""
    return concat_text(cl.name, cl.label, cl.codes, cl.description)


def _text_map(codelists: list[CodeList]) -> dict[str, str]:
    """Retourne un dict {cl.id -> texte concatene normalise}."""
    return {cl.id: _text_for_dedup(cl) for cl in codelists}


def _code_similarity(a: CodeList, b: CodeList) -> float:
    """Score de similarite entre deux listes de codes.

    Pour chaque code de la liste la plus petite, on cherche le meilleur match
    dans l'autre liste (sur la valeur + libelle). Le score est la fraction
    de codes bien apparies.

    Un match est valide si les valeurs sont identiques ET les libelles
    ont une similarite élevée (> 0.6 par defaut).
    """
    from difflib import SequenceMatcher

    codes_a = [(v, lab) for v, lab in a.codes]
    codes_b = [(v, lab) for v, lab in b.codes]

    if not codes_a and not codes_b:
        return 1.0
    if not codes_a or not codes_b:
        return 0.0

    # Pour chaque code de a, trouver le meilleur match dans b
    matches = 0
    used_b: set[int] = set()
    for i, (va, la) in enumerate(codes_a):
        best = 0.0
        best_j = -1
        for j, (vb, lb) in enumerate(codes_b):
            if j in used_b:
                continue

            # Similarite des valeurs
            val_sim = 1.0 if va == vb else SequenceMatcher(None, va, vb).ratio()

            # Similarite des libelles
            lbl_sim = (
                1.0
                if la == lb
                else SequenceMatcher(None, la.lower(), lb.lower()).ratio()
            )

            if val_sim >= 0.9:
                # Valeurs similaires → combiner valeurs + libelles
                score = val_sim * 0.3 + lbl_sim * 0.7
            elif val_sim >= 0.6:
                # Valeurs partiellement similaires → penaliser
                score = val_sim * 0.2 + lbl_sim * 0.2
            else:
                # Valeurs totalement differentes → penaliser fortement
                score = 0.0

            if score > best:
                best = score
                best_j = j

        if best >= 0.65:
            matches += 1
            used_b.add(best_j)

    return matches / len(codes_a)


def _name_similarity(a: CodeList, b: CodeList) -> tuple[float, bool]:
    """Similarite SequenceMatcher entre les noms des CodeLists.

    Returns:
        Tuple (score, is_trustworthy).
        Si un nom est trop court (< 3 chars), on le juge
        non informatif → retourne (1.0, False).
    """
    a_name = a.name or ""
    b_name = b.name or ""
    if not a_name and not b_name:
        return 1.0, True
    if not a_name or not b_name:
        return 0.0, True

    # Noms courts = non informatifs (ex: "a", "b", "CL1")
    if len(a_name) < 3 or len(b_name) < 3:
        return 1.0, False

    name_sim = SequenceMatcher(None, a_name.lower(), b_name.lower()).ratio()
    return name_sim, True


def _has_context_link(a: CodeList, b: CodeList) -> bool:
    """Verifie si deux CodeLists partagent un contexte DDI (Variable ou Catégorie)."""
    if not a.var_ids or not b.var_ids:
        return False
    return bool(a.var_ids & b.var_ids)


def _build_pair_entry(
    a: CodeList,
    b: CodeList,
    score: float,
    code_sim: float,
    name_sim: float,
    shared_vars: set[str],
    shared_cats: set[str],
) -> dict:
    """Construit un dict de paire avec ses metadonnees detaillees."""
    return {
        "score": round(score, 4),
        "a": a,
        "b": b,
        "a_name": a.name,
        "b_name": b.name,
        "a_codes": len(a.codes),
        "b_codes": len(b.codes),
        "code_similarity": round(code_sim, 4),
        "name_similarity": round(name_sim, 4),
        "shared_vars": sorted(shared_vars),
        "shared_cats": sorted(shared_cats),
    }


def detect_fuzzy_duplicates(
    codelists: list[CodeList],
    *,
    threshold: float = 0.90,
    inspect_threshold: float = 0.80,
    min_code_sim: float = 0.75,
) -> tuple[list, dict, dict]:
    """
    Phase 2 — Quasi-doublons (similarite hybride).

    Ne compare que les representatives uniques (1 par signature exacte).

    Score hybride = 0.6 * similarite_codes + 0.4 * similarite_noms.
    Boost : si code_sim >= 0.90, le score minimum est élever a 0.95 pour
    capturer les doublons exacts mal nommes.

    Args:
        codelists: Liste complete de CodeList avec signature calculee.
        threshold: Seuil minimal pour une detection (>= 0.90 par defaut).
        inspect_threshold: Seuil minimal pour l'inspection (>= 0.80 par defaut).
        min_code_sim: Similarite minimale sur les codes pour etre considere.

    Returns:
        Tuple (paires_detectees, toutes_paires_dict, toutes_paires_list).
        - paires_detectees: liste de scores >= threshold
        - toutes_paires_dict: dict {score: entry} pour scores >= inspect_threshold
        - toutes_paires_list: liste de tous SCORES >= inspect_threshold (triée)
    """
    groups = _group_by_sig(codelists)
    uniques = [g[0] for g in groups.values()]

    nearby: list = []
    for a, b in itertools.combinations(uniques, 2):
        code_sim = _code_similarity(a, b)

        # Filtre prealable : similarite codes minimale
        if code_sim < min_code_sim:
            continue

        name_sim, name_is_trustworthy = _name_similarity(a, b)

        # Score hybride : si nom non fiable (trop court), compte uniquement les codes
        if not name_is_trustworthy:
            hybrid_score = code_sim
        else:
            hybrid_score = 0.6 * code_sim + 0.4 * name_sim

        # Boost : codes quasi-identiques + noms differents → suspect
        if code_sim >= 0.95 and name_sim < 0.5:
            hybrid_score = max(hybrid_score, 0.95)

        if hybrid_score < inspect_threshold:
            continue

        # Calcul des contextes communs
        shared_vars = a.var_ids & b.var_ids
        shared_cats = a.cat_ids & b.cat_ids

        entry = _build_pair_entry(a, b, hybrid_score, code_sim, name_sim, shared_vars, shared_cats)
        nearby.append(entry)

    nearby.sort(key=lambda e: e["score"], reverse=True)
    detected = [e for e in nearby if e["score"] >= threshold]
    return detected, {e["score"]: e for e in nearby}, detected


# Alias pour compatibilite avec les appels existants
detect_fuzzy = detect_fuzzy_duplicates


def detect_fuzzy_from_dict(
    pairs: list[dict],
    *,
    threshold: float = 0.50,
) -> list[dict]:
    """Filtre une liste de pairs (dictionnaires) par seuil de similarite.

    Args:
        pairs: Liste de pairs (dictionnaires) retournees par detect_fuzzy_duplicates.
        threshold: Seuil minimal de similarite.

    Returns:
        Liste de pairs ayant un score >= threshold.
    """
    return [p for p in pairs if p.get("score", 0) >= threshold]
