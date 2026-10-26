"""Pipeline de détection de doublons — entonnoir en trois niveaux."""

from __future__ import annotations

import itertools
from difflib import SequenceMatcher

from .normalize import concat_text
from .types import CodeList

# Nombre de codes maximum d'une CodeList pour être comparée en phase floue.
# Les codes de même valeur sont appariés directement (coût linéaire) ; seuls
# les très grands référentiels (communes ~35 000 codes, IRIS) sont exclus.
MAX_CODES_FOR_FUZZY = 5000

# Nombre maximum de codes restants (sans valeur identique dans l'autre liste)
# pour la recherche du meilleur appariement, en O(n * m) appels SequenceMatcher.
# Au-delà, ces codes restent non appariés.
MAX_CODES_PAIRWISE = 100

# Score minimal d'appariement de deux codes
_CODE_MATCH_THRESHOLD = 0.65


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


def _pair_score(va: str, la: str, vb: str, lb: str) -> float:
    """Score d'appariement de deux codes (valeur, libellé)."""
    # Similarite des valeurs
    val_sim = 1.0 if va == vb else SequenceMatcher(None, va, vb).ratio()

    # Similarite des libelles
    lbl_sim = 1.0 if la == lb else SequenceMatcher(None, la.lower(), lb.lower()).ratio()

    if val_sim >= 0.9:
        # Valeurs similaires → combiner valeurs + libelles
        return val_sim * 0.3 + lbl_sim * 0.7
    if val_sim >= 0.6:
        # Valeurs partiellement similaires → penaliser
        return val_sim * 0.2 + lbl_sim * 0.2
    # Valeurs totalement differentes → penaliser fortement
    return 0.0


def _code_similarity(
    a: CodeList,
    b: CodeList,
    max_pairwise: int = MAX_CODES_PAIRWISE,
) -> float:
    """Score de similarite entre deux listes de codes.

    Chaque code de `a` est apparié à au plus un code de `b`, en deux temps :
      1. codes de même valeur (index par valeur, coût linéaire) ;
      2. codes restants : meilleur appariement parmi les codes restants de `b`
         (O(n * m)), seulement si les deux restes comptent au plus
         `max_pairwise` codes.
    Le score est le nombre de codes apparies rapporte a la taille de la
    PLUS GRANDE liste : il est symetrique et une liste incluse dans une
    plus grande n'obtient pas 100 %.

    Un match est valide si son score d'appariement atteint 0.65 (pour deux
    valeurs identiques : libellés similaires a plus de 0.5).
    """
    codes_a = [(v, lab) for v, lab in a.codes]
    codes_b = [(v, lab) for v, lab in b.codes]

    if not codes_a and not codes_b:
        return 1.0
    if not codes_a or not codes_b:
        return 0.0

    matches = 0
    used_b: set[int] = set()

    # 1. Codes de même valeur
    by_value: dict[str, list[int]] = {}
    for j, (vb, _) in enumerate(codes_b):
        by_value.setdefault(vb, []).append(j)

    rest_a: list[tuple[str, str]] = []
    for va, la in codes_a:
        best = 0.0
        best_j = -1
        for j in by_value.get(va, ()):
            if j in used_b:
                continue
            score = _pair_score(va, la, *codes_b[j])
            if score > best:
                best = score
                best_j = j
        if best >= _CODE_MATCH_THRESHOLD:
            matches += 1
            used_b.add(best_j)
        else:
            rest_a.append((va, la))

    # 2. Codes restants : meilleur appariement, si les restes sont petits
    rest_b = [j for j in range(len(codes_b)) if j not in used_b]
    if rest_a and rest_b and len(rest_a) <= max_pairwise and len(rest_b) <= max_pairwise:
        for va, la in rest_a:
            best = 0.0
            best_j = -1
            for j in rest_b:
                if j in used_b:
                    continue
                score = _pair_score(va, la, *codes_b[j])
                if score > best:
                    best = score
                    best_j = j
            if best >= _CODE_MATCH_THRESHOLD:
                matches += 1
                used_b.add(best_j)

    return matches / max(len(codes_a), len(codes_b))


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
    max_codes: int = MAX_CODES_FOR_FUZZY,
) -> tuple[list, dict, dict, list[CodeList]]:
    """
    Phase 2 — Quasi-doublons (similarite hybride).

    Ne compare que les representatives uniques (1 par signature exacte),
    et seulement celles dont le nombre de codes ne depasse pas `max_codes`.
    Une paire dont le rapport des tailles est sous `min_code_sim` est ecartee
    sans calcul : sa similarite de codes ne peut pas l'atteindre.

    Score hybride = 0.6 * similarite_codes + 0.4 * similarite_noms.
    Boost : si code_sim >= 0.90, le score minimum est élever a 0.95 pour
    capturer les doublons exacts mal nommes.

    Args:
        codelists: Liste complete de CodeList avec signature calculee.
        threshold: Seuil minimal pour une detection (>= 0.90 par defaut).
        inspect_threshold: Seuil minimal pour l'inspection (>= 0.80 par defaut).
        min_code_sim: Similarite minimale sur les codes pour etre considere.
        max_codes: Nombre de codes maximum d'une CodeList pour etre comparee
            (au-dela, elle est exclue de la phase floue).

    Returns:
        Tuple (paires_detectees, toutes_paires_dict, toutes_paires_list, exclues).
        - paires_detectees: liste de scores >= threshold
        - toutes_paires_dict: dict {score: entry} pour scores >= inspect_threshold
        - toutes_paires_list: liste de tous SCORES >= inspect_threshold (triée)
        - exclues: CodeLists dont len(codes) > max_codes, non comparees
    """
    groups = _group_by_sig(codelists)
    uniques = [g[0] for g in groups.values()]

    excluded = [cl for cl in uniques if len(cl.codes) > max_codes]
    eligible = [cl for cl in uniques if len(cl.codes) <= max_codes]

    nearby: list = []
    for a, b in itertools.combinations(eligible, 2):
        # Au plus min(len) codes apparies sur max(len) : borne de code_sim
        if min(len(a.codes), len(b.codes)) < min_code_sim * max(len(a.codes), len(b.codes)):
            continue

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
    return detected, {e["score"]: e for e in nearby}, detected, excluded


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
