"""report.py — Rapport de contrôle : JSON, CSV et tableau pour le terminal."""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from ..types import CodeList
from .engine import (
    DOUBLON,
    INCLUSION,
    NOUVELLE,
    PROCHE,
    REUTILISEE,
    CheckResult,
    check_codelists,
    code_diff,
    internal_duplicates,
    load_codelists,
    suggested_action,
)
from .index import ReferenceIndex

STATUS_LABELS = {
    REUTILISEE: "réutilisée",
    DOUBLON: "doublon",
    INCLUSION: "inclusion",
    PROCHE: "proche",
    NOUVELLE: "nouvelle",
}
DETECTION_LABELS = {
    "exact": "exact",
    "exact_business": "exact hors échappement",
    "labels": "mêmes libellés",
    "inclusion": "sous-ensemble",
    "extension": "extension",
    "lineage": "lignée (BasedOnObject)",
    "fuzzy": "proche",
}


def _ref(cl: CodeList, index: ReferenceIndex) -> dict[str, Any]:
    return {
        "id": cl.id,
        "urn": cl.urn,
        "name": cl.name,
        "label": cl.label,
        "level": index.level(cl),
        "codes_count": len(cl.codes),
        "operations": sorted(cl.operations),
        "series": sorted(cl.series),
    }


def build_report(
    source: str,
    results: list[CheckResult],
    internal: list[dict[str, Any]],
    index: ReferenceIndex,
) -> dict[str, Any]:
    """Rapport complet (sérialisable en JSON)."""
    codelists = []
    for res in results:
        cl = res.codelist
        entry: dict[str, Any] = {
            "id": cl.id,
            "urn": cl.urn,
            "name": cl.name,
            "label": cl.label,
            "codes_count": len(cl.codes),
            "variables": sorted(set(cl.vars)),
            "quality_flags": cl.quality_flags,
            "status": res.status,
            "action": suggested_action(res, index),
            "matches": [
                {
                    **_ref(m.target, index),
                    "detection": m.detection,
                    "confidence": m.confidence,
                    "diff": code_diff(cl, m.target),
                }
                for m in res.matches
            ],
        }
        if res.reused is not None:
            entry["reused"] = _ref(res.reused, index)
        codelists.append(entry)

    counts = Counter(res.status for res in results)
    return {
        "source": source,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "index": {k: index.meta.get(k) for k in ("built_at", "api_base_url", "scope", "counts", "restricted_to")
                  if k in index.meta},
        "summary": {
            "codelists": len(results),
            "by_status": {status: counts.get(status, 0) for status in STATUS_LABELS},
            "internal_duplicates": len(internal),
        },
        "codelists": codelists,
        "internal_duplicates": [
            {
                "a": {"id": d["a"].id, "name": d["a"].name},
                "b": {"id": d["b"].id, "name": d["b"].name},
                "detection": d["detection"],
                "confidence": d["confidence"],
            }
            for d in internal
        ],
    }


def check_bytes(raw: bytes, source: str, index: ReferenceIndex) -> dict[str, Any]:
    """Contrôle complet d'un fichier DDI : rapport prêt à sérialiser."""
    codelists = load_codelists(raw)
    return build_report(source, check_codelists(codelists, index), internal_duplicates(codelists), index)


def has_findings(report: dict[str, Any], fail_on: str) -> bool:
    """Vrai si le rapport contient un constat au niveau `fail_on` (doublon, proche, never)."""
    if fail_on == "never":
        return False
    by_status = report["summary"]["by_status"]
    statuses = [DOUBLON, INCLUSION] + ([PROCHE] if fail_on == "proche" else [])
    return any(by_status.get(s) for s in statuses) or bool(report["summary"]["internal_duplicates"])


def to_csv(report: dict[str, Any]) -> str:
    """Une ligne par (liste fournie, correspondance) ; une ligne seule sans correspondance."""
    out = io.StringIO()
    writer = csv.writer(out, delimiter=";")
    writer.writerow([
        "liste_id", "liste_nom", "codes", "statut", "action", "correspondance_id",
        "correspondance_nom", "niveau", "detection", "confiance", "codes_en_plus",
        "codes_en_moins", "libelles_differents", "series", "suspecte",
    ])
    for cl in report["codelists"]:
        base = [cl["id"], cl["name"], cl["codes_count"], STATUS_LABELS[cl["status"]], cl["action"]]
        flags = ",".join(cl["quality_flags"])
        if not cl["matches"]:
            writer.writerow(base + [""] * 9 + [flags])
        for m in cl["matches"]:
            writer.writerow(base + [
                m["id"], m["name"], m["level"], DETECTION_LABELS.get(m["detection"], m["detection"]),
                m["confidence"], len(m["diff"]["codes_en_plus"]), len(m["diff"]["codes_en_moins"]),
                len(m["diff"]["libelles_differents"]), ",".join(m["series"]), flags,
            ])
    return out.getvalue()


def _cut(text: str, width: int) -> str:
    text = text or ""
    return text if len(text) <= width else text[: width - 1] + "…"


def to_table(report: dict[str, Any]) -> str:
    """Tableau lisible dans un terminal, suivi des doublons internes."""
    lines = [f"Contrôle de {report['source']}"]
    idx = report["index"]
    lines.append(
        f"Index de référence : {idx.get('scope', '?')} · construit le {str(idx.get('built_at', '?'))[:16]}"
    )
    summary = report["summary"]
    lines.append(
        f"{summary['codelists']} liste(s) : "
        + " · ".join(f"{n} {STATUS_LABELS[s]}" for s, n in summary["by_status"].items() if n)
        + (f" · {summary['internal_duplicates']} doublon(s) interne(s)" if summary["internal_duplicates"] else "")
    )
    lines.append("")
    header = f"{'Statut':<11} {'Liste':<32} {'Correspondance':<32} {'Niveau':<11} {'Détection':<22} {'Conf.':>5}"
    lines += [header, "-" * len(header)]
    order = {s: i for i, s in enumerate([DOUBLON, INCLUSION, PROCHE, NOUVELLE, REUTILISEE])}
    for cl in sorted(report["codelists"], key=lambda c: (order[c["status"]], c["name"])):
        best = cl["matches"][0] if cl["matches"] else None
        target = best or cl.get("reused")
        lines.append(
            f"{STATUS_LABELS[cl['status']]:<11} {_cut(cl['name'] or cl['id'], 32):<32} "
            f"{_cut(target['name'] if target else '', 32):<32} "
            f"{(target or {}).get('level', ''):<11} "
            f"{_cut(DETECTION_LABELS.get(best['detection'], '') if best else '', 22):<22} "
            f"{(f'{best['confidence']:.2f}' if best else ''):>5}"
        )
        if cl["status"] not in (REUTILISEE, NOUVELLE):
            lines.append(f"{'':<11} → {cl['action']}")
        if cl["quality_flags"]:
            lines.append(f"{'':<11} ⚠ liste suspecte : {', '.join(cl['quality_flags'])}")
    if report["internal_duplicates"]:
        lines += ["", "Doublons internes au fichier :"]
        for d in report["internal_duplicates"]:
            lines.append(
                f"  {d['a']['name'] or d['a']['id']} ↔ {d['b']['name'] or d['b']['id']} "
                f"({DETECTION_LABELS.get(d['detection'], d['detection'])}, {d['confidence']:.2f})"
            )
    return "\n".join(lines)
