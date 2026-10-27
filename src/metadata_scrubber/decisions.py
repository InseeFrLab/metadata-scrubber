"""decisions.py — Décisions de validation conservées d'un run à l'autre.

Le fichier decisions.json se trouve à la racine des sorties, à côté de
cleaned_codelists.json. La sauvegarde du registre des doublons l'alimente ; le
pipeline le relit pour réappliquer les décisions aux paires de nouveau
détectées. Les CodeLists écartées (sans doublon, à ne pas ajouter au registre
nettoyé) et les maîtres choisies par l'utilisateur y sont aussi conservées.

Schéma :
{
  "version": 1,
  "updated_at": "<iso utc>",
  "pairs": {"<id_a>|<id_b>": {"decision": "approve" | "reject", "updated_at"}},
  "dismissed": {"<cl_id>": {"updated_at"}},
  "masters": {"<cl_id>": {"updated_at"}}
}

Les clés de paire sont les deux ids triés : la décision vaut quel que soit
le sens (maître / doublon). Une paire remise « en attente » est retirée.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .cleaned_registry import output_root_path
from .registry_io import read_json_registry, write_json_registry

DECISIONS_FILENAME = "decisions.json"
_KEPT_DECISIONS = ("approve", "reject")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def decisions_path(registry_path: str) -> str:
    """Chemin de decisions.json pour un registre des doublons (racine des sorties)."""
    return output_root_path(registry_path, DECISIONS_FILENAME)


def pair_key(id_a: str, id_b: str) -> str:
    """Clé d'une paire, indépendante de l'ordre des ids."""
    return "|".join(sorted((id_a, id_b)))


def empty_decisions_doc() -> dict[str, Any]:
    return {"version": 1, "updated_at": _now(), "pairs": {}, "dismissed": {}, "masters": {}}


def read_decisions(path: str) -> dict[str, Any]:
    """Lit decisions.json ; fichier absent → document vide.

    Les autres erreurs (fichier illisible, S3 indisponible) remontent : la
    sauvegarde ne doit pas écraser un fichier qu'elle n'a pas pu lire.
    """
    try:
        doc = read_json_registry(path)
    except FileNotFoundError:
        return empty_decisions_doc()
    doc.setdefault("pairs", {})
    doc.setdefault("dismissed", {})
    doc.setdefault("masters", {})
    return doc


def load_decisions(path: str) -> dict[str, Any]:
    """Comme `read_decisions`, mais tolérant : une erreur donne un document vide."""
    try:
        return read_decisions(path)
    except Exception as exc:
        print(f"  [décisions] Illisible ({path}) — ignoré : {exc}")
        return empty_decisions_doc()


def update_decisions(doc: dict[str, Any], registry: dict[str, Any]) -> None:
    """Reporte dans `doc` les décisions du registre des doublons.

    Seules les paires et listes présentes dans `registry` sont mises à jour :
    les décisions venant d'autres fichiers sources sont conservées.
    """
    now = _now()
    pairs: dict[str, Any] = doc.setdefault("pairs", {})
    dismissed: dict[str, Any] = doc.setdefault("dismissed", {})
    masters: dict[str, Any] = doc.setdefault("masters", {})

    for cl_id, cl_data in registry.items():
        if cl_data.get("dismissed"):
            dismissed.setdefault(cl_id, {"updated_at": now})
        else:
            dismissed.pop(cl_id, None)

        if cl_data.get("master_chosen"):
            masters.setdefault(cl_id, {"updated_at": now})
        else:
            masters.pop(cl_id, None)

        for dup in cl_data.get("duplicates", []):
            if not dup.get("id"):
                continue
            masters.pop(dup["id"], None)
            key = pair_key(cl_id, dup["id"])
            decision = dup.get("decision", "pending")
            if decision in _KEPT_DECISIONS:
                if pairs.get(key, {}).get("decision") != decision:
                    pairs[key] = {"decision": decision, "updated_at": now}
            else:
                pairs.pop(key, None)

    doc["updated_at"] = now


def write_decisions(registry: dict[str, Any], registry_path: str) -> tuple[str, int]:
    """Met à jour decisions.json à la racine des sorties du registre.

    Returns:
        (chemin de decisions.json, nombre de paires décidées).
    """
    path = decisions_path(registry_path)
    doc = read_decisions(path)
    update_decisions(doc, registry)
    write_json_registry(doc, path)
    return path, len(doc["pairs"])


def apply_decisions(registry: dict[str, Any], doc: dict[str, Any]) -> int:
    """Réapplique au registre des doublons les décisions d'un run précédent.

    Une décision reprise est marquée `decision_carried: true` ; une liste
    écartée reçoit `dismissed: true`.

    Returns:
        Nombre de décisions et d'écarts repris.
    """
    pairs = doc.get("pairs", {})
    dismissed = doc.get("dismissed", {})
    carried = 0
    for cl_id, cl_data in registry.items():
        if cl_id in dismissed:
            cl_data["dismissed"] = True
            carried += 1
        for dup in cl_data.get("duplicates", []):
            saved = pairs.get(pair_key(cl_id, dup.get("id", "")))
            if saved and saved.get("decision") in _KEPT_DECISIONS:
                dup["decision"] = saved["decision"]
                dup["decision_carried"] = True
                carried += 1
    return carried


def reapply_decisions(registry: dict[str, Any], doc: dict[str, Any]) -> int:
    """Remet le registre chargé dans l'appli à l'état de decisions.json.

    Les décisions sont enregistrées dans decisions.json à chaque changement,
    sans réécrire le registre des doublons : au chargement, decisions.json
    fait foi. Contrairement à `apply_decisions` (début de run), les décisions
    ne sont pas marquées `decision_carried` ; une décision reprise en début de
    run puis remise en attente (absente de decisions.json) repasse en attente.

    Returns:
        Nombre de décisions et d'écarts modifiés.
    """
    pairs = doc.get("pairs", {})
    dismissed = doc.get("dismissed", {})
    changed = 0
    for cl_id, cl_data in registry.items():
        if cl_id in dismissed and not cl_data.get("dismissed"):
            cl_data["dismissed"] = True
            changed += 1
        for dup in cl_data.get("duplicates", []):
            saved = pairs.get(pair_key(cl_id, dup.get("id", "")))
            if saved and saved.get("decision") in _KEPT_DECISIONS:
                if dup.get("decision") != saved["decision"]:
                    dup["decision"] = saved["decision"]
                    dup.pop("decision_carried", None)
                    changed += 1
            elif dup.get("decision_carried"):
                dup["decision"] = "pending"
                dup.pop("decision_carried")
                changed += 1
    return changed
