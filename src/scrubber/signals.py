"""Signaux d'usage : croiser les références Variables → CodeLists."""

from __future__ import annotations

from dataclasses import dataclass

from .types import CodeList


# ---------------------------------------------------------------------------
# 1. Signature d'usage
# ---------------------------------------------------------------------------

def compute_usage_signatures(codelists: list[CodeList]) -> list[CodeList]:
    """
    Attache la signature d'usage à chaque codelist.

    var_sig = tuple trié et dédup des variables qui référencent la
    codelist. Deux codelists avec la même var_sig ont exactement le même
    contexte d'utilisation.
    """
    for cl in codelists:
        cl.var_sig = tuple(sorted(set(cl.vars)))
    return codelists


def find_usage_groups(
    codelists: list[CodeList],
    *,
    min_members: int = 2,
) -> dict[tuple[str, ...], list[CodeList]]:
    """
    Groupe les codelists par signature d'usage identique.

    Returns dict {var_sig_tuple: [codelists]} pour les signatures
    partagées par >= min_members listes.
    """
    groups: dict[tuple[str, ...], list[CodeList]] = {}
    for cl in codelists:
        if not cl.var_sig:
            continue
        groups.setdefault(cl.var_sig, []).append(cl)
    return {sig: g for sig, g in groups.items() if len(g) >= min_members}


# ---------------------------------------------------------------------------
# 2. Croisement signaux d'usage
# ---------------------------------------------------------------------------

@dataclass
class CrossSignal:
    """Résultat du croisement entre une paire de codelists et leurs usages."""
    a: CodeList
    b: CodeList
    shared_vars: tuple[str, ...]
    only_a: tuple[str, ...]
    only_b: tuple[str, ...]
    usage_type: str = "unknown"

    @property
    def same_usage(self) -> bool:
        return self.usage_type == "same"

    @property
    def shared_ratio(self) -> float:
        all_vars = set(self.a.vars) | set(self.b.vars)
        if not all_vars:
            return 0.0
        return len(self.shared_vars) / len(all_vars)


def cross_check(a: CodeList, b: CodeList) -> CrossSignal:
    """
    Calcule le signal d'usage entre deux codelists.

    Returns:
        CrossSignal avec :
        - shared_vars : variables communes à a ET b
        - only_a : présent seulement dans a
        - only_b : présent seulement dans b
        - usage_type : "same", "partial", "disjoint"
    """
    vars_a = set(a.vars)
    vars_b = set(b.vars)
    shared = vars_a & vars_b
    only_a = vars_a - vars_b
    only_b = vars_b - vars_a

    if vars_a == vars_b and bool(vars_a):
        usage_type = "same"
    elif shared:
        usage_type = "partial"
    else:
        usage_type = "disjoint"

    return CrossSignal(
        a=a,
        b=b,
        shared_vars=tuple(sorted(shared)),
        only_a=tuple(sorted(only_a)),
        only_b=tuple(sorted(only_b)),
        usage_type=usage_type,
    )


# ---------------------------------------------------------------------------
# 3. Fusionner signaux d'usage avec les paires détectées
# ---------------------------------------------------------------------------

def enrich_pairs_with_usage(pairs: list[tuple]) -> list[dict[str, object]]:
    """
    Pour chaque paire de quasi-doublons, calcule et attache le signal d'usage.

    Args:
        pairs: list of (score: float, a: CodeList, b: CodeList, ...)

    Returns:
        list of dicts with added 'signal' key.
    """
    enriched: list[dict[str, object]] = []
    for entry in pairs:
        sig = cross_check(entry["a"], entry["b"])
        item: dict[str, object] = dict(entry)
        item["signal"] = sig
        enriched.append(item)
    return enriched
