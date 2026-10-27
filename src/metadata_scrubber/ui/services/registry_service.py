"""registry_service.py — Décisions/bulk/filtres du registre des doublons.

L'IO (local/S3) et le cycle de vie du registre nettoyé vivent dans le
package `scrubber` (scrubber.registry_io, scrubber.cleaned_registry) —
ce module ne garde que la logique propre à l'app (décisions, stats, filtres).
"""

from __future__ import annotations

import logging
from typing import Any

# Ré-exports : IO générique depuis le package scrubber
from metadata_scrubber.registry_io import (  # noqa: F401
    read_json_registry as read_registry,
    write_json_registry as write_registry,
)

from metadata_scrubber.reporting.duplicates_registry import detection_against_parent

logger = logging.getLogger(__name__)

# Champs d'une CodeList communs à un parent et à un doublon du registre
_MEMBER_FIELDS = (
    "id", "name", "label", "codes_count", "codes", "escape_codes", "quality_flags",
    "agency", "version", "based_on", "cat_ids", "vars", "origin", "links",
    "urn", "operations", "series",
)


class SwapParentError(ValueError):
    """Changement de maître impossible (maître du registre nettoyé, registre ancien)."""


def get_stats(registry: dict[str, Any]) -> dict[str, Any]:
    """Calcule les statistiques globales du registre.

    Args:
        registry: Le registre des doublons.

    Returns:
        Un dictionnaire contenant les statistiques.
    """
    total_cls = len(registry)
    total_dups = sum(len(cl.get("duplicates", [])) for cl in registry.values())
    decisions_list: list[str] = []
    type_counts: dict[str, int] = {}
    
    for cl_data in registry.values():
        for dup in cl_data.get("duplicates", []):
            decisions_list.append(dup.get("decision", "pending"))
            for t in dup.get("detection_types", []):
                type_counts[t] = type_counts.get(t, 0) + 1

    n_approved = sum(1 for d in decisions_list if d == "approve")
    n_rejected = sum(1 for d in decisions_list if d == "reject")
    n_pending = len(decisions_list) - n_approved - n_rejected

    return {
        "total_code_lists": total_cls,
        "total_duplicates": total_dups,
        "approved": n_approved,
        "rejected": n_rejected,
        "pending": n_pending,
        "by_detection_type": type_counts,
    }


def get_decision(registry: dict[str, Any], cl_id: str, dup_id: str) -> str | None:
    """Décision courante d'un doublon ("pending" par défaut), None s'il est introuvable."""
    for dup in (registry.get(cl_id) or {}).get("duplicates", []):
        if dup.get("id") == dup_id:
            return dup.get("decision", "pending")
    return None


def set_decision(registry: dict[str, Any], cl_id: str, dup_id: str, decision: str) -> bool:
    """Modifie la decision d'un duplicate.

    Args:
        registry: Le registre des doublons.
        cl_id: L'identifiant de la CodeList.
        dup_id: L'identifiant du duplicate.
        decision: "approve", "reject" ou "pending".

    Returns:
        True si la decision a été modifige.
    """
    for cl_data in registry.values():
        if cl_data.get("id") == cl_id:
            for dup in cl_data.get("duplicates", []):
                if dup.get("id") == dup_id:
                    dup["decision"] = decision
                    dup.pop("decision_carried", None)
                    return True
    return False


def bulk_set_decisions(registry: dict[str, Any], criteria: str, action: str) -> int:
    """Applique une decision en masse sur plusieurs duplicates.

    Args:
        registry: Le registre des doublons.
        criteria: "exact", "high-confidence", "all", "approved_exact", "rejected_all", "pending_all".
        action: "approve", "reject" ou "pending".

    Returns:
        Le nombre de duplicates modifies.
    """
    count = 0
    for cl_data in registry.values():
        for dup in cl_data.get("duplicates", []):
            should = False
            if criteria == "all":
                should = True
            elif criteria == "exact":
                should = "exact" in dup.get("detection_types", [])
            elif criteria == "high-confidence":
                should = dup.get("confidence", 0) >= 0.95
            elif criteria == "approved_exact":
                should = (
                    "exact" in dup.get("detection_types", [])
                    and dup.get("decision", "pending") == "pending"
                )
            elif criteria == "rejected_all":
                should = True
            elif criteria == "pending_all":
                should = True

            if should:
                dup["decision"] = action
                dup.pop("decision_carried", None)
                count += 1
    return count


def get_duplicates_for_codelist(
    registry: dict[str, Any],
    cl_id: str,
) -> dict[str, Any] | None:
    """Récupère les données d'une CodeList et ses duplicates.

    Args:
        registry: Le registre des doublons.
        cl_id: L'identifiant de la CodeList.

    Returns:
        Les donnees de la CodeList ou None si non trouvée.
    """
    return registry.get(cl_id)


def filter_codelists(
    registry: dict[str, Any],
    decision_filter: list[str] | None = None,
    search: str | None = None,
    sort_by: str = "duplicates_count",
    page: int = 0,
    page_size: int = 50,
) -> dict[str, Any]:
    """Filtre et paginene les CodeLists du registre.

    Args:
        registry: Le registre des doublons.
        decision_filter: Liste de decisions à filtrer.
        search: Terme de recherche.
        sort_by: "duplicates_count" ou "name".
        page: Numéro de page (0-indexed).
        page_size: Nombre de CodeLists par page.

    Returns:
        Un dictionnaire contenant les CodeLists filtrées et les meta-données de pagination.
    """
    # Construire la liste structurée
    cl_list: list[dict[str, Any]] = []
    for cl_id, cl_data in registry.items():
        duplicates = cl_data.get("duplicates", [])
        decisions = [d.get("decision", "pending") for d in duplicates]
        
        # Appliquer le filtre decision
        if decision_filter and decision_filter != ["approve", "reject", "pending"]:
            if not decisions:
                continue
            # Au moins un duplicate avec decision matching
            if not any(d in decision_filter for d in decisions):
                continue
        
        # Appliquer le filtre recherche
        if search:
            search_lower = search.lower()
            cl_name = (cl_data.get("name") or cl_id[:10]).lower()
            if search_lower not in cl_name:
                dup_names = [
                    (d.get("name") or d.get("id", "")).lower()
                    for d in duplicates
                ]
                if not any(search_lower in n for n in dup_names):
                    continue
        
        cl_list.append({
            "id": cl_data.get("id", cl_id),
            "name": cl_data.get("name", cl_id),
            "label": cl_data.get("label", ""),
            "codes_count": cl_data.get("codes_count", len(cl_data.get("codes", []))),
            "vars_count": len(cl_data.get("vars", [])),
            "vars": cl_data.get("vars", [])[:5],  # max 5 vars affichées
            "cat_ids_count": len(cl_data.get("cat_ids", [])),
            "duplicates_count": len(duplicates),
            "duplicates": duplicates,
            "decisions": decisions,
        })

    # Tri
    if sort_by == "duplicates_count":
        cl_list.sort(key=lambda x: len(x.get("decisions", [])), reverse=True)
    elif sort_by == "name":
        cl_list.sort(key=lambda x: x.get("name", "").lower())

    # Pagination
    total = len(cl_list)
    start = page * page_size
    end = start + page_size
    items = cl_list[start:end]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
    }


def save_registry(
    registry: dict[str, Any],
    path: str | None = None,
) -> str:
    """Sauvegarde le registre et retourne le chemin.

    Args:
        registry: Le registre des doublons.
        path: Chemin de destination optionnel.

    Returns:
        Le chemin dans lequel le registre a été sauvegardė.
    """
    if path:
        write_registry(registry, path)
        return path
    # Chercher un registry_path existant côté client via le premier cl_id
    # (géré côté client en pratique)
    raise ValueError("Chemin de sauvegarde requis — aucune destination connue")


def swap_parent(registry: dict[str, Any], parent_id: str, new_parent_id: str) -> dict[str, Any]:
    """Fait d'un doublon la maître de son groupe ; l'ancienne maître devient doublon.

    Types de détection, confiance et `via` de chaque doublon sont recalculés
    par rapport à la nouvelle maître à partir des `links`. Les décisions des
    autres doublons sont conservées ; l'ancienne maître est mise en attente.
    La nouvelle entrée porte `master_chosen` (choix conservé entre les runs)
    et `previous_parent` (pour la synchronisation du registre nettoyé).
    L'entrée est remplacée à la même position dans le registre.

    Raises:
        KeyError: maître ou doublon introuvable.
        SwapParentError: maître issue du registre nettoyé, ou registre sans `links`
            (produit par une version antérieure du pipeline).

    Returns:
        La nouvelle entrée.
    """
    old = registry.get(parent_id)
    if old is None:
        raise KeyError(parent_id)
    if old.get("origin") == "registry":
        raise SwapParentError(
            "La maître vient du registre nettoyé : modifiez-la depuis l'onglet Registre nettoyé."
        )
    duplicates = old.get("duplicates", [])
    promoted = next((d for d in duplicates if d.get("id") == new_parent_id), None)
    if promoted is None:
        raise KeyError(new_parent_id)
    if "links" not in promoted or "links" not in old:
        raise SwapParentError(
            "Registre produit par une version antérieure (sans liens) : relancez le pipeline."
        )

    old_as_dup = {k: old[k] for k in _MEMBER_FIELDS if k in old}
    old_as_dup["decision"] = "pending"
    new_dups = []
    for dup in [old_as_dup] + [d for d in duplicates if d.get("id") != new_parent_id]:
        types, conf, via = detection_against_parent(dup.get("links", []), new_parent_id)
        new_dup = {k: v for k, v in dup.items() if k != "via"}
        new_dup["detection_types"] = types
        new_dup["confidence"] = conf
        if via:
            new_dup["via"] = via
        new_dups.append(new_dup)

    entry = {k: promoted[k] for k in _MEMBER_FIELDS if k in promoted}
    entry["master_chosen"] = True
    entry["previous_parent"] = parent_id
    entry["duplicates"] = new_dups

    items = list(registry.items())
    registry.clear()
    for cl_id, data in items:
        if cl_id == parent_id:
            registry[new_parent_id] = entry
        else:
            registry[cl_id] = data
    return entry
