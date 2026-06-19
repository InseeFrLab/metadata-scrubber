"""Couche quasi-doublons : candidats bornés + scoring flou (code archive).

Phase 1 ne fusionne que sur ``sig_pairs`` (égalité exacte). Les listes
*proches* (mêmes valeurs / même nom, mais contenu non identique) sont détectées
ici, sans O(n²) global :

1. **génération de candidats** bornée par buckets (``sig_values`` ou ``sig_name``) ;
2. **scoring** réutilisant le code de l'archive (``text_similarity``), avec
   repli ``jaccard`` au-delà de ``max_len`` pour éviter l'explosion du
   ``SequenceMatcher`` sur les grosses listes.

Rien n'est fusionné : on produit des propositions (amorce de la phase 2).
"""

from __future__ import annotations

from dataclasses import dataclass

from .registry import Registry
from .similarity import concat_codelist_text, jaccard, text_similarity

# Au-delà de cette taille de bucket par nom, on saute (évite les paquets
# pathologiques de listes homonymes / sans nom).
_MAX_BUCKET = 50


@dataclass
class NearDuplicate:
    canonical_a: str
    canonical_b: str
    score: float
    name_a: str
    name_b: str


def _candidate_pairs(reg: Registry) -> set[tuple[str, str]]:
    """Paires de canons candidates, bornées par buckets sig_values / sig_name."""
    pairs: set[tuple[str, str]] = set()
    for column in ("sig_values", "sig_name"):
        buckets = reg.conn.execute(
            f"""
            SELECT {column} AS key, GROUP_CONCAT(canonical_id) AS ids,
                   COUNT(*) AS n
            FROM canonical
            WHERE is_empty = 0
            GROUP BY {column}
            HAVING COUNT(DISTINCT sig_pairs) > 1
            """
        ).fetchall()
        for b in buckets:
            ids = b["ids"].split(",")
            if len(ids) > _MAX_BUCKET:
                continue  # bucket trop large : on ne génère pas ces paires
            for i in range(len(ids)):
                for j in range(i + 1, len(ids)):
                    a, c = sorted((ids[i], ids[j]))
                    pairs.add((a, c))
    return pairs


def _load_texts(reg: Registry, cids: set[str]) -> dict[str, tuple[str, str]]:
    """Pour chaque canon : (display_name, texte concaténé) pour le scoring."""
    texts: dict[str, tuple[str, str]] = {}
    for cid in cids:
        name, label, pairs = reg.canonical_content(cid)
        texts[cid] = (name, concat_codelist_text(name, label, pairs))
    return texts


def near_duplicate_pairs(
    reg: Registry, threshold: float = 0.90, max_len: int = 20000
) -> list[NearDuplicate]:
    """Paires de canons proches (score ≥ ``threshold``), triées par score décroissant."""
    candidates = _candidate_pairs(reg)
    cids = {c for pair in candidates for c in pair}
    texts = _load_texts(reg, cids)

    results: list[NearDuplicate] = []
    for a, b in candidates:
        name_a, text_a = texts[a]
        name_b, text_b = texts[b]
        if max(len(text_a), len(text_b)) > max_len:
            score = jaccard(text_a, text_b)  # repli peu coûteux
        else:
            score = text_similarity(text_a, text_b)
        if score >= threshold:
            results.append(NearDuplicate(a, b, score, name_a, name_b))

    results.sort(key=lambda r: r.score, reverse=True)
    return results
