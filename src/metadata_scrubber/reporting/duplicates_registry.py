"""Registre des doublons par CodeList.

Génère un dictionnaire JSON qui, pour chaque CodeList identifiée, liste tous
ses doublons potentiels détectés par les différentes méthodes du pipeline.

Usage :
    from metadata_scrubber.reporting.duplicates_registry import (
        build_duplicates_registry,
        write_duplicates_registry,
    )

    registry = build_duplicates_registry(candidates, codelists)
    write_duplicates_registry(candidates, codelists, "./audit/codelist_duplicates.json")

"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field

from metadata_scrubber.quality import split_codes


@dataclass
class DuplicateInfo:
    """Informations minimales sur un duplicate détecté."""

    id: str
    name: str
    label: str
    codes_count: int
    codes: list[tuple[str, str]]
    cat_ids: list[str]
    vars: list[str]
    detection_types: list[str] = field(default_factory=list)
    confidence: float = 0.0
    origin: str = "xml"
    via: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)


def _serialize_minimal(cl) -> dict:
    """Sérialise une CodeList avec juste ce qui est utile pour valider un duplicate."""
    return {
        "id": cl.id,
        "name": cl.name or "",
        "label": cl.label or "",
        "codes_count": len(cl.codes),
        "codes": [list(pair) for pair in cl.codes],
        "escape_codes": [list(pair) for pair in split_codes(cl.codes)[1]],
        "cat_ids": sorted(cl.cat_ids),
        "vars": cl.vars or [],
        "origin": getattr(cl, "origin", "xml"),
    }


def detection_against_parent(
    links: list[dict], parent_id: str
) -> tuple[list[str], float, list[str]]:
    """Types de détection, confiance et `via` d'un membre de groupe vis-à-vis du parent.

    La paire directe avec le parent est ce que l'interface présente. À défaut
    (lien transitif via un autre membre), on retombe sur toutes les paires du
    membre et `via` liste les membres avec lesquels il est réellement lié.

    Args:
        links: Liens du membre ({id, detection_types, confidence}), un par
            autre membre du groupe avec lequel une paire a été détectée.
        parent_id: Id du parent du groupe.

    Returns:
        (detection_types triés, confidence, via).
    """
    direct = [lk for lk in links if lk["id"] == parent_id]
    used = direct or links
    types = sorted({t for lk in used for t in lk["detection_types"]})
    conf = max((lk["confidence"] for lk in used), default=0.0)
    via = [] if direct else [lk["id"] for lk in links]
    return types, conf, via


def build_duplicates_registry(
    candidates,  # list[CandidateFusion]
    codelists,  # list[CodeList] — garde l'ordre XML
    preferred_parents: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """Construit le registre JSON « doublons par CodeList ».

    Chaque paire/groupe de doublons n'apparaît qu'UNE fois : les paires sont
    regroupées par composante connexe. Le parent d'un groupe est, dans
    l'ordre : une entrée du registre nettoyé, une liste choisie comme maître
    par l'utilisateur (`preferred_parents`, marquée `master_chosen`), sinon la
    liste la plus tôt dans l'ordre de `codelists`. Les autres membres du
    groupe sont ses « duplicates » et n'ont pas d'entrée top-level. Les
    détections multiples sur une même paire sont fusionnées (union des
    `detection_types`, max des `confidence`). Chaque membre d'un groupe porte
    ses `links` : toutes ses paires avec les autres membres, ce qui permet de
    changer de parent sans relancer le pipeline. Les listes sans doublon ont
    une entrée avec `duplicates: []`.

    Args:
        candidates: Liste de `CandidateFusion` issue de `_build_candidates()`.
        codelists: Liste des `CodeList` dans l'ordre d'apparition XML.
        preferred_parents: Ids choisis comme maîtres lors de validations
            précédentes (decisions.json).

    Returns:
        Dict {cl_id: {id, name, label, codes, cat_ids, vars, duplicates: [...]}}
    """
    # ── 1. Index xml_order : id → position dans le XML ──
    xml_order: dict[str, int] = {}
    for idx, cl in enumerate(codelists):
        if cl.id not in xml_order:
            xml_order[cl.id] = idx

    # ── 2. Index cl_by_id ──
    cl_by_id: dict[str, CodeList] = {cl.id: cl for cl in codelists}

    # ── 3. Collecter tous les duplicates avec fusion des détections ──
    # key = (source_cl_id, target_cl_id)  →  { types: set, confidence: float }
    pair_data: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "types": set(),
        "confidence": 0.0,
    })

    for cand in candidates:
        # Tous les membres de ce groupe
        members = [cand.master_cl] + list(cand.slave_cls)

        # Pour chaque paire unique dans ce groupe
        for i, a in enumerate(members):
            for j, b in enumerate(members):
                if i == j:
                    continue
                key = (a.id, b.id)
                data = pair_data[key]
                data["types"].add(cand.detection_type)
                data["confidence"] = max(data["confidence"], cand.confidence)

    # Graphe non orienté des paires : id → partenaires
    partners: dict[str, set[str]] = defaultdict(set)
    for a_id, b_id in pair_data:
        partners[a_id].add(b_id)
        partners[b_id].add(a_id)

    def links_of(member: str, component: set[str]) -> list[dict]:
        """Paires détectées entre `member` et les autres membres du groupe."""
        links = []
        for other in sorted(component, key=lambda i: xml_order.get(i, 9999)):
            data = pair_data.get((member, other))
            if other != member and data:
                links.append({
                    "id": other,
                    "detection_types": sorted(data["types"]),
                    "confidence": data["confidence"],
                })
        return links

    def parent_rank(cl_id: str) -> tuple:
        cl = cl_by_id.get(cl_id)
        from_registry = cl is not None and getattr(cl, "origin", "xml") == "registry"
        return (not from_registry, cl_id not in preferred_parents, xml_order.get(cl_id, 9999))

    # ── 4. Construire les entrées par CL — une paire n'apparaît qu'une fois ──
    entries: dict[str, dict] = {}
    visited: set[str] = set()

    for cl in codelists:
        if cl.id in visited:
            continue
        visited.add(cl.id)

        if cl.id not in partners:
            # Liste sans doublon détecté (ajout manuel au registre possible)
            entries[cl.id] = {**_serialize_minimal(cl), "duplicates": []}
            continue

        # Composante connexe de la liste (groupe de doublons)
        component: set[str] = {cl.id}
        stack = [cl.id]
        while stack:
            node = stack.pop()
            for nb in partners.get(node, ()):
                if nb not in component:
                    component.add(nb)
                    stack.append(nb)
        visited |= component

        parent_id = min((i for i in component if i in cl_by_id), key=parent_rank)
        parent = cl_by_id[parent_id]

        dupes_infos: list[DuplicateInfo] = []
        for tid in component:
            if tid == parent_id:
                continue
            target_cl = cl_by_id.get(tid)
            if target_cl is None:
                continue
            links = links_of(tid, component)
            types, conf, via = detection_against_parent(links, parent_id)
            dupes_infos.append(
                DuplicateInfo(
                    id=tid,
                    name=target_cl.name or "",
                    label=target_cl.label or "",
                    codes_count=len(target_cl.codes),
                    codes=list(target_cl.codes),
                    cat_ids=sorted(target_cl.cat_ids),
                    vars=list(target_cl.vars),
                    detection_types=types,
                    confidence=conf,
                    origin=getattr(target_cl, "origin", "xml"),
                    via=via,
                    links=links,
                )
            )

        # Trier les duplicates par ordre XML
        dupes = sorted(dupes_infos, key=lambda d: xml_order.get(d.id, 9999))

        entries[parent_id] = {
            **_serialize_minimal(parent),
            "links": links_of(parent_id, component),
            **({"master_chosen": True} if parent_id in preferred_parents else {}),
            "duplicates": [
                {
                    "id": d.id,
                    "name": d.name,
                    "label": d.label,
                    "codes_count": d.codes_count,
                    "codes": [list(c) for c in d.codes],
                    "escape_codes": [list(c) for c in split_codes(d.codes)[1]],
                    "cat_ids": d.cat_ids,
                    "vars": d.vars,
                    "detection_types": sorted(d.detection_types),
                    "confidence": d.confidence,
                    "origin": d.origin,
                    "decision": "pending",
                    # Présent seulement pour un lien indirect (pas de paire avec le parent)
                    **({"via": d.via} if d.via else {}),
                    "links": d.links,
                }
                for d in dupes
            ],
        }

    # ── 5. Trier par ordre d'apparition XML ──
    ordered = {}
    for cl_id, data in sorted(entries.items(),
                               key=lambda kv: xml_order.get(kv[0], 9999)):
        ordered[cl_id] = data

    return ordered


def write_duplicates_registry(
    candidates,  # list[CandidateFusion]
    codelists,  # list[CodeList]
    output_path: str,
) -> str:
    """Écrit le registre des doublons par CodeList dans un fichier JSON.

    Args:
        candidates: Liste de `CandidateFusion`.
        codelists: Liste des `CodeList` dans l'ordre XML.
        output_path: Chemin du fichier JSON à écrire.

    Returns:
        Chemin du fichier écrit.
    """
    registry = build_duplicates_registry(candidates, codelists)
    formatted = json.dumps(registry, indent=2, ensure_ascii=False)

    if output_path.startswith("s3://"):
        from metadata_scrubber.s3 import make_s3_filesystem

        fs = make_s3_filesystem()
        with fs.open(output_path, "w", encoding="utf-8") as f:
            f.write(formatted)
    else:
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(formatted)

    return output_path
