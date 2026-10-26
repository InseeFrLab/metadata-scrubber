"""cleaned_registry.py — Cycle de vie du registre des CodeLists nettoyées.

Le registre nettoyé (cleaned_codelists.json, schéma v2) est un magasin
persistant et ÉDITABLE : la validation des doublons l'alimente (sync
incrémental), l'utilisateur peut modifier noms/libellés/codes, et le
pipeline le réinjecte dans la détection (les entrées du registre sont
prioritaires, les listes déjà remplacées sont exclues).

Schéma v2 :
{
  "version": 2,
  "generated_at": "<iso utc>",
  "source_registry": "<chemin du registre des doublons>",
  "codelists": {
    "<cl_id>": {
      "id", "name", "label",
      "codes": [["valeur", "libellé"], ...],
      "codes_count", "vars",
      "replaces": [{"id", "name", "detection_types", "confidence"}, ...],
      "first_added_at", "updated_at"
    }
  }
}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .registry_io import read_json_registry, write_json_registry
from .run_output import RUN_DIR_PATTERN
from .types import CodeList


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def output_root_path(registry_path: str, filename: str) -> str:
    """Chemin d'un fichier partagé entre les runs, à la racine des sorties.

    Le fichier est placé dans le dossier du registre des doublons ; si ce
    dossier est un dossier de run daté (AAAAMMJJ-HHMMSS), il remonte à la
    racine de sortie, au niveau supérieur, pour être partagé entre les runs.

    Manipulation de chaîne (pas de pathlib) pour rester compatible S3.
    """
    if "/" in registry_path:
        base, _ = registry_path.rsplit("/", 1)
        parent, _, last = base.rpartition("/")
        if parent and RUN_DIR_PATTERN.match(last):
            base = parent
        return f"{base}/{filename}"
    return filename


def cleaned_registry_path(registry_path: str) -> str:
    """Dérive le chemin du registre nettoyé (cleaned_codelists.json, racine des sorties)."""
    return output_root_path(registry_path, "cleaned_codelists.json")


def empty_cleaned_doc() -> dict[str, Any]:
    """Document registre nettoyé vide (schéma v2)."""
    return {
        "version": 2,
        "generated_at": _now(),
        "source_registry": "",
        "codelists": {},
    }


def migrate_cleaned_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Migre un document v1 vers le schéma v2 (merged_duplicates → replaces)."""
    for entry in (doc.get("codelists") or {}).values():
        if "merged_duplicates" in entry and "replaces" not in entry:
            entry["replaces"] = entry.pop("merged_duplicates")
        entry.setdefault("replaces", [])
    doc["version"] = 2
    doc.setdefault("codelists", {})
    return doc


def read_cleaned_registry(path: str) -> dict[str, Any]:
    """Lit le registre nettoyé (local ou S3) et le migre au schéma v2."""
    return migrate_cleaned_doc(read_json_registry(path))


def sync_cleaned_registry(
    registry: dict[str, Any],
    cleaned: dict[str, Any] | None,
    source_path: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Synchronise le registre nettoyé depuis les décisions du registre des doublons.

    Sync incrémental (jamais de régénération complète) :
      - un doublon approuvé est ajouté au `replaces` de l'entrée parente
        (entrée créée depuis les données du parent si absente) ;
      - un doublon dé-approuvé est retiré de `replaces` — uniquement s'il
        appartient aux duplicates de CE parent dans le registre courant
        (les ids issus d'autres runs ou ajoutés à la main sont préservés) ;
      - une entrée dont `replaces` devient vide est supprimée ;
      - après un changement de maître (`previous_parent`), l'entrée de
        l'ancienne maître perd les listes du groupe (supprimée si vide) ;
      - name/label/codes/vars des entrées existantes ne sont JAMAIS modifiés
        (les éditions manuelles sont préservées).

    Args:
        registry: Le registre des doublons (état courant des décisions).
        cleaned: Le registre nettoyé existant (None → document vide).
        source_path: Chemin du registre des doublons (traçabilité).

    Returns:
        (registre nettoyé synchronisé, stats du sync).
    """
    now = _now()
    if not isinstance(cleaned, dict):
        cleaned = empty_cleaned_doc()
    cleaned = migrate_cleaned_doc(cleaned)
    entries: dict[str, Any] = cleaned["codelists"]

    stats = {
        "entries_created": 0,
        "entries_deleted": 0,
        "replaces_added": 0,
        "replaces_removed": 0,
    }

    for cl_id, cl_data in registry.items():
        dups_ici = {d.get("id"): d for d in cl_data.get("duplicates", []) if d.get("id")}
        approuves = {
            did: d for did, d in dups_ici.items() if d.get("decision") == "approve"
        }

        # Changement de maître : le groupe ne reste pas rattaché à l'ancienne
        prev = cl_data.get("previous_parent")
        if prev and prev != cl_id and prev in entries:
            groupe = {cl_id, *dups_ici}
            prev_entry = entries[prev]
            restants = [r for r in prev_entry.get("replaces", []) if r.get("id") not in groupe]
            retires = len(prev_entry.get("replaces", [])) - len(restants)
            if retires:
                stats["replaces_removed"] += retires
                if restants:
                    prev_entry["replaces"] = restants
                    prev_entry["updated_at"] = now
                else:
                    del entries[prev]
                    stats["entries_deleted"] += 1

        entry = entries.get(cl_id)

        if entry is None:
            if not approuves:
                continue
            entries[cl_id] = {
                "id": cl_data.get("id", cl_id),
                "name": cl_data.get("name", ""),
                "label": cl_data.get("label", ""),
                "codes": cl_data.get("codes", []),
                "codes_count": cl_data.get("codes_count", len(cl_data.get("codes", []))),
                "vars": cl_data.get("vars", []),
                "replaces": [
                    {
                        "id": d.get("id", ""),
                        "name": d.get("name", ""),
                        "detection_types": d.get("detection_types", []),
                        "confidence": d.get("confidence", 0),
                    }
                    for d in approuves.values()
                ],
                "first_added_at": now,
                "updated_at": now,
            }
            stats["entries_created"] += 1
            stats["replaces_added"] += len(approuves)
            continue

        # Entrée existante — sync du périmètre géré uniquement (dups_ici)
        modified = False
        existants = {r.get("id") for r in entry.get("replaces", [])}

        for did, d in approuves.items():
            if did not in existants:
                entry.setdefault("replaces", []).append(
                    {
                        "id": d.get("id", ""),
                        "name": d.get("name", ""),
                        "detection_types": d.get("detection_types", []),
                        "confidence": d.get("confidence", 0),
                    }
                )
                stats["replaces_added"] += 1
                modified = True

        kept: list[dict[str, Any]] = []
        for r in entry.get("replaces", []):
            rid = r.get("id")
            if rid in dups_ici and rid not in approuves:
                stats["replaces_removed"] += 1
                modified = True
                continue
            kept.append(r)
        entry["replaces"] = kept

        # Suppression uniquement si ce sont les retraits de CE sync qui ont
        # vidé replaces — une entrée ajoutée manuellement (replaces vide)
        # survit aux syncs.
        if not kept and modified:
            del entries[cl_id]
            stats["entries_deleted"] += 1
        elif modified:
            entry["updated_at"] = now

    cleaned["generated_at"] = now
    cleaned["source_registry"] = source_path
    cleaned["version"] = 2
    return cleaned, stats


def write_cleaned_registry(
    registry: dict[str, Any],
    registry_path: str,
) -> tuple[str, int]:
    """Synchronise et écrit le registre nettoyé à côté du registre des doublons.

    Returns:
        (chemin du registre nettoyé, nombre d'entrées).
    """
    path = cleaned_registry_path(registry_path)
    try:
        previous = read_cleaned_registry(path)
    except Exception:
        previous = None
    cleaned, _stats = sync_cleaned_registry(registry, previous, registry_path)
    write_json_registry(cleaned, path)
    return path, len(cleaned["codelists"])


def add_entry_from_parent(cleaned: dict[str, Any], cl_data: dict[str, Any]) -> bool:
    """Ajoute au registre nettoyé une entrée créée depuis un parent du registre
    des doublons (liste sans doublon validée manuellement, replaces vide).

    Returns:
        True si l'entrée a été créée, False si elle existait déjà.
    """
    cl_id = cl_data.get("id", "")
    entries = cleaned.setdefault("codelists", {})
    if not cl_id or cl_id in entries:
        return False
    now = _now()
    entries[cl_id] = {
        "id": cl_id,
        "name": cl_data.get("name", ""),
        "label": cl_data.get("label", ""),
        "codes": cl_data.get("codes", []),
        "codes_count": cl_data.get("codes_count", len(cl_data.get("codes", []))),
        "vars": cl_data.get("vars", []),
        "replaces": [],
        "first_added_at": now,
        "updated_at": now,
    }
    cleaned["generated_at"] = now
    return True


def add_entries_from_parents(
    cleaned: dict[str, Any],
    registry: dict[str, Any],
    cl_ids: list[str] | None = None,
) -> dict[str, list[str]]:
    """Ajoute en lot au registre nettoyé des listes sans doublon du registre.

    Args:
        cleaned: registre nettoyé, modifié en place.
        registry: registre des doublons ({cl_id: entrée parent}).
        cl_ids: ids à ajouter ; None = toutes les listes sans doublon du registre,
            hors listes écartées.

    Returns:
        Ids classés en added / already_present / not_found / skipped_with_duplicates.
    """
    result: dict[str, list[str]] = {
        "added": [],
        "already_present": [],
        "not_found": [],
        "skipped_with_duplicates": [],
    }
    if cl_ids is None:
        cl_ids = [
            cl_id for cl_id, cl in registry.items()
            if not cl.get("duplicates") and not cl.get("dismissed")
        ]

    for cl_id in dict.fromkeys(cl_ids):
        cl_data = registry.get(cl_id)
        if cl_data is None:
            result["not_found"].append(cl_id)
        elif cl_data.get("duplicates"):
            result["skipped_with_duplicates"].append(cl_id)
        elif add_entry_from_parent(cleaned, cl_data):
            result["added"].append(cl_id)
        else:
            result["already_present"].append(cl_id)
    return result


def remove_entries(cleaned: dict[str, Any], cl_ids: list[str]) -> dict[str, list[str]]:
    """Retire du registre nettoyé des entrées ajoutées manuellement.

    Seules les entrées sans ``replaces`` (listes sans doublon ajoutées à la main)
    sont retirables : les autres résultent de décisions approuvées et se
    corrigent en changeant la décision puis en sauvegardant le registre.

    Args:
        cleaned: registre nettoyé, modifié en place.
        cl_ids: ids à retirer.

    Returns:
        Ids classés en removed / not_present / protected.
    """
    result: dict[str, list[str]] = {"removed": [], "not_present": [], "protected": []}
    entries = cleaned.get("codelists", {})
    for cl_id in dict.fromkeys(cl_ids):
        entry = entries.get(cl_id)
        if entry is None:
            result["not_present"].append(cl_id)
        elif entry.get("replaces"):
            result["protected"].append(cl_id)
        else:
            del entries[cl_id]
            result["removed"].append(cl_id)
    if result["removed"]:
        cleaned["generated_at"] = _now()
    return result


def validate_cleaned_doc(doc: Any) -> list[str]:
    """Valide (et normalise) un document registre nettoyé soumis à l'édition.

    Normalise codes_count et les paires de codes en listes [valeur, libellé].

    Returns:
        Liste des erreurs (vide = document valide).
    """
    errors: list[str] = []
    if not isinstance(doc, dict) or not isinstance(doc.get("codelists"), dict):
        return ["Document invalide : objet avec une clé 'codelists' attendu"]

    for cl_id, entry in doc["codelists"].items():
        prefix = f"codelists[{cl_id}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} : objet attendu")
            continue
        if not isinstance(entry.get("id"), str) or not entry["id"]:
            errors.append(f"{prefix}.id : chaîne non vide attendue")
        elif entry["id"] != cl_id:
            errors.append(f"{prefix}.id : doit être égal à la clé ({cl_id})")
        for field in ("name", "label"):
            if not isinstance(entry.get(field, ""), str):
                errors.append(f"{prefix}.{field} : chaîne attendue")
        codes = entry.get("codes", [])
        if not isinstance(codes, list):
            errors.append(f"{prefix}.codes : liste attendue")
        else:
            normalized = []
            for i, pair in enumerate(codes):
                if (
                    not isinstance(pair, (list, tuple))
                    or len(pair) != 2
                    or not all(isinstance(x, str) for x in pair)
                ):
                    errors.append(f"{prefix}.codes[{i}] : paire [valeur, libellé] attendue")
                    continue
                if not pair[0].strip():
                    errors.append(f"{prefix}.codes[{i}] : valeur vide")
                    continue
                normalized.append([pair[0], pair[1]])
            entry["codes"] = normalized
            entry["codes_count"] = len(normalized)
        replaces = entry.get("replaces", [])
        if not isinstance(replaces, list) or not all(
            isinstance(r, dict) and isinstance(r.get("id"), str) for r in replaces
        ):
            errors.append(f"{prefix}.replaces : liste d'objets avec un id attendue")

    return errors


def load_cleaned_codelists(path: str) -> tuple[list[CodeList], set[str]]:
    """Charge le registre nettoyé pour l'injection dans le pipeline.

    Tolérant : fichier absent ou invalide → ([], set()).

    Returns:
        (CodeLists du registre avec origin="registry",
         ids de toutes les listes remplacées — union des replaces).
    """
    try:
        doc = read_cleaned_registry(path)
    except Exception as exc:
        print(f"  [registre] Illisible ({path}) — ignoré : {exc}")
        return [], set()

    codelists: list[CodeList] = []
    replaced_ids: set[str] = set()
    for cl_id, entry in doc.get("codelists", {}).items():
        codes = [
            (str(pair[0]), str(pair[1]) if len(pair) > 1 else "")
            for pair in entry.get("codes", [])
            if isinstance(pair, (list, tuple)) and pair
        ]
        codelists.append(
            CodeList(
                id=entry.get("id", cl_id),
                name=entry.get("name", ""),
                label=entry.get("label", ""),
                codes=codes,
                vars=list(entry.get("vars", [])),
                origin="registry",
            )
        )
        for r in entry.get("replaces") or entry.get("merged_duplicates") or []:
            rid = r.get("id") if isinstance(r, dict) else r
            if rid:
                replaced_ids.add(rid)

    return codelists, replaced_ids
