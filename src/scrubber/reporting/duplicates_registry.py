"""Registre des doublons par CodeList.

Génère un dictionnaire JSON qui, pour chaque CodeList identifiée, liste tous
ses doublons potentiels détectés par les différentes méthodes du pipeline.

Usage :
    from scrubber.reporting.duplicates_registry import (
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


def _serialize_minimal(cl) -> dict:
    """Sérialise une CodeList avec juste ce qui est utile pour valider un duplicate."""
    return {
        "id": cl.id,
        "name": cl.name or "",
        "label": cl.label or "",
        "codes_count": len(cl.codes),
        "codes": [list(pair) for pair in cl.codes],
        "cat_ids": sorted(cl.cat_ids),
        "vars": cl.vars or [],
    }


def build_duplicates_registry(
    candidates,  # list[CandidateFusion]
    codelists,  # list[CodeList] — garde l'ordre XML
) -> dict:
    """Construit le registre JSON « doublons par CodeList ».

    Pour chaque CandidateFusion (master + slaves), on ajoute pour CHAQUE CodeList
    (master ET slaves) les autres membres du groupe comme « duplicates ».
    On fusionne les multiples détections sur une même paire en agrégeant les
    `detection_types` et en gardant le max `confidence`.

    Args:
        candidates: Liste de `CandidateFusion` issue de `_build_candidates()`.
        codelists: Liste des `CodeList` dans l'ordre d'apparition XML.

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

    # Collecte de toutes les IDs rencontrées
    seen_ids: set[str] = set()

    for cand in candidates:
        # Tous les membres de ce groupe
        members = [cand.master_cl] + list(cand.slave_cls)
        member_ids = [c.id for c in members]
        seen_ids.update(member_ids)

        # Pour chaque paire unique dans ce groupe
        for i, a in enumerate(members):
            for j, b in enumerate(members):
                if i == j:
                    continue
                key = (a.id, b.id)
                data = pair_data[key]
                data["types"].add(cand.detection_type)
                data["confidence"] = max(data["confidence"], cand.confidence)

    # ── 4. Construire les entrées par CL ──
    entries: dict[str, dict] = {}

    for cl_id in seen_ids:
        cl = cl_by_id.get(cl_id)
        if cl is None:
            # Fallback : essayer de retrouver la CL dans les candidates
            found = None
            for cand in candidates:
                if cand.master_cl.id == cl_id:
                    found = cand.master_cl
                    break
                for slave in cand.slave_cls:
                    if slave.id == cl_id:
                        found = slave
                        break
                if found:
                    break
            cl = found
            if cl is None:
                continue

        # Construire la liste des duplicates pour cette CL
        dupes_by_target_id: dict[str, DuplicateInfo] = {}

        for source_id, tid in pair_data:
            if source_id != cl_id:
                continue
            data = pair_data[(source_id, tid)]

            target_cl = cl_by_id.get(tid, None)

            if tid in dupes_by_target_id:
                existing = dupes_by_target_id[tid]
                existing.detection_types.update(data["types"])
                existing.confidence = max(existing.confidence, data["confidence"])
            else:
                dupes_by_target_id[tid] = DuplicateInfo(
                    id=tid,
                    name=target_cl.name if target_cl and target_cl.name else "",
                    label=target_cl.label if target_cl and target_cl.label else "",
                    codes_count=len(target_cl.codes) if target_cl else 0,
                    codes=list(target_cl.codes) if target_cl else [],
                    cat_ids=sorted(target_cl.cat_ids) if target_cl else [],
                    vars=list(target_cl.vars) if target_cl else [],
                    detection_types=data["types"].copy(),
                    confidence=data["confidence"],
                )

        # Trier les duplicates par ordre XML
        dupes = sorted(dupes_by_target_id.values(),
                       key=lambda d: xml_order.get(d.id, 9999))

        entries[cl_id] = {
            **_serialize_minimal(cl),
            "duplicates": [
                {
                    "id": d.id,
                    "name": d.name,
                    "label": d.label,
                    "codes_count": d.codes_count,
                    "codes": [list(c) for c in d.codes],
                    "cat_ids": d.cat_ids,
                    "vars": d.vars,
                    "detection_types": sorted(d.detection_types),
                    "confidence": d.confidence,
                    "decision": "pending",
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

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)

    return output_path
