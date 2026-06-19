"""Génération des artefacts (tout proposé, aucune écriture dans la base source)."""

from __future__ import annotations

import csv
import json
import statistics
from pathlib import Path

from . import rml
from .dedup import near_duplicate_pairs
from .registry import Registry


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def canonical_rows(reg: Registry) -> list[dict]:
    return [
        dict(r)
        for r in reg.conn.execute(
            """
            SELECT c.canonical_id, c.display_name, c.label, c.n_pairs, c.is_empty,
                   c.first_seen_op,
                   COUNT(m.ddi_id) AS n_members,
                   COUNT(DISTINCT m.operation) AS n_operations
            FROM canonical c
            LEFT JOIN member m ON m.canonical_id = c.canonical_id
            GROUP BY c.canonical_id
            ORDER BY n_members DESC, c.n_pairs DESC
            """
        ).fetchall()
    ]


def write_canonical_list(reg: Registry, out_dir: Path) -> Path:
    rows = canonical_rows(reg)
    csv_path = out_dir / "canonical_codelists.csv"
    _write_csv(
        csv_path,
        [
            "canonical_id",
            "display_name",
            "label",
            "n_pairs",
            "n_members",
            "n_operations",
            "first_seen_op",
            "is_empty",
        ],
        rows,
    )
    (out_dir / "canonical_codelists.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return csv_path


def write_mapping(reg: Registry, operation: str, out_dir: Path) -> Path:
    members = reg.conn.execute(
        """
        SELECT m.ddi_id, m.version, m.urn, m.source_id, m.codelist_name,
               m.n_pairs, m.canonical_id, c.first_seen_op,
               (SELECT COUNT(*) FROM member m2
                WHERE m2.canonical_id = m.canonical_id AND m2.operation = m.operation)
                   AS n_in_op
        FROM member m
        JOIN canonical c ON c.canonical_id = m.canonical_id
        WHERE m.operation = ?
        ORDER BY m.canonical_id, m.ddi_id
        """,
        (operation,),
    ).fetchall()

    # le représentant intra-op d'un canon = (ddi_id, version) le plus petit
    reps: dict[str, tuple[str, str]] = {}
    for m in members:
        cid = m["canonical_id"]
        key = (m["ddi_id"], m["version"])
        if cid not in reps or key < reps[cid]:
            reps[cid] = key

    rows = []
    for m in members:
        cid = m["canonical_id"]
        key = (m["ddi_id"], m["version"])
        if m["first_seen_op"] != operation:
            match_type = "inter_op_dup"
        elif m["n_in_op"] > 1 and key != reps[cid]:
            match_type = "intra_op_dup"
        else:
            match_type = "new"
        rows.append(
            {
                "operation": operation,
                "source_id": m["source_id"],
                "ddi_id": m["ddi_id"],
                "version": m["version"],
                "urn": m["urn"],
                "codelist_name": m["codelist_name"],
                "n_pairs": m["n_pairs"],
                "canonical_id": cid,
                "match_type": match_type,
            }
        )

    path = out_dir / f"mapping_{operation}.csv"
    _write_csv(
        path,
        [
            "operation",
            "source_id",
            "ddi_id",
            "version",
            "urn",
            "codelist_name",
            "n_pairs",
            "canonical_id",
            "match_type",
        ],
        rows,
    )
    return path


def write_recommendations(reg: Registry, operation: str, out_dir: Path) -> Path:
    """Canons redondants (>1 membre), triés par priorité = (n_membres-1) × n_pairs."""
    rows = []
    for c in canonical_rows(reg):
        if c["is_empty"] or c["n_members"] <= 1:
            continue
        priority = (c["n_members"] - 1) * c["n_pairs"]
        rows.append(
            {
                "canonical_id": c["canonical_id"],
                "display_name": c["display_name"],
                "label": c["label"],
                "n_pairs": c["n_pairs"],
                "n_members": c["n_members"],
                "n_operations": c["n_operations"],
                "priority": priority,
            }
        )
    rows.sort(key=lambda r: r["priority"], reverse=True)

    path = out_dir / f"recommendations_{operation}.csv"
    _write_csv(
        path,
        [
            "canonical_id",
            "display_name",
            "label",
            "n_pairs",
            "n_members",
            "n_operations",
            "priority",
        ],
        rows,
    )
    return path


def write_near_duplicates(
    reg: Registry, operation: str, out_dir: Path, threshold: float = 0.90
) -> Path:
    rows = [
        {
            "canonical_a": nd.canonical_a,
            "canonical_b": nd.canonical_b,
            "score": round(nd.score, 3),
            "name_a": nd.name_a,
            "name_b": nd.name_b,
        }
        for nd in near_duplicate_pairs(reg, threshold=threshold)
    ]
    path = out_dir / f"near_duplicates_{operation}.csv"
    _write_csv(
        path,
        ["canonical_a", "canonical_b", "score", "name_a", "name_b"],
        rows,
    )
    return path


def write_semantic_candidates(
    reg: Registry, operation: str, out_dir: Path, candidates: list, chat_model: str
) -> Path:
    """CSV des candidats sémantiques (cosinus + verdict LLM si disponible)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for c in candidates:
        v = reg.conn.execute(
            "SELECT same_concept, confidence, rationale FROM semantic_verdict"
            " WHERE canonical_a=? AND canonical_b=? AND model=?",
            (c.canonical_a, c.canonical_b, chat_model),
        ).fetchone()
        name_a = reg.conn.execute(
            "SELECT display_name FROM canonical WHERE canonical_id=?",
            (c.canonical_a,),
        ).fetchone()[0]
        name_b = reg.conn.execute(
            "SELECT display_name FROM canonical WHERE canonical_id=?",
            (c.canonical_b,),
        ).fetchone()[0]
        rows.append(
            {
                "canonical_a": c.canonical_a,
                "canonical_b": c.canonical_b,
                "cosine": round(c.cosine, 4),
                "name_a": name_a,
                "name_b": name_b,
                "same_concept": "" if not v or v["same_concept"] is None else bool(v["same_concept"]),
                "confidence": "" if not v else v["confidence"],
                "rationale": "" if not v else v["rationale"],
            }
        )
    rows.sort(
        key=lambda r: (r["confidence"] or 0 if r["same_concept"] is True else -1, r["cosine"]),
        reverse=True,
    )
    path = out_dir / f"semantic_candidates_{operation}.csv"
    _write_csv(
        path,
        ["canonical_a", "canonical_b", "cosine", "name_a", "name_b",
         "same_concept", "confidence", "rationale"],
        rows,
    )
    return path


def _stats(xs: list[float]) -> tuple[float, float, float] | None:
    if not xs:
        return None
    return (min(xs), statistics.median(xs), max(xs))


def write_semantic_summary(
    reg: Registry, operation: str, out_dir: Path, candidates: list, chat_model: str
) -> dict:
    """Écrit `semantic_summary_<op>.md` et renvoie les compteurs clés."""
    out_dir.mkdir(parents=True, exist_ok=True)
    confirmed: list[tuple] = []  # (candidate, confidence)
    rejected = undetermined = not_judged = 0
    for c in candidates:
        v = reg.conn.execute(
            "SELECT same_concept, confidence FROM semantic_verdict"
            " WHERE canonical_a=? AND canonical_b=? AND model=?",
            (c.canonical_a, c.canonical_b, chat_model),
        ).fetchone()
        if v is None:
            not_judged += 1
        elif v["same_concept"] == 1:
            confirmed.append((c, v["confidence"]))
        elif v["same_concept"] == 0:
            rejected += 1
        else:
            undetermined += 1

    involved: set[str] = set()
    for c, _ in confirmed:
        involved.update((c.canonical_a, c.canonical_b))

    cos_stats = _stats([c.cosine for c, _ in confirmed])
    conf_stats = _stats([cf for _, cf in confirmed if cf is not None])
    counts = {
        "candidates": len(candidates),
        "judged": len(candidates) - not_judged,
        "confirmed": len(confirmed),
        "rejected": rejected,
        "undetermined": undetermined,
        "not_judged": not_judged,
        "codelists_involved": len(involved),
        "cosine": cos_stats,
        "confidence": conf_stats,
    }

    def _name(cid: str) -> str:
        r = reg.conn.execute(
            "SELECT display_name FROM canonical WHERE canonical_id=?", (cid,)
        ).fetchone()
        return r[0] if r else cid

    top = sorted(confirmed, key=lambda t: (t[1] or 0, t[0].cosine), reverse=True)[:20]
    lines = [
        f"# Rapprochements sémantiques — {operation}",
        "",
        f"- Modèle juge : `{chat_model}`",
        f"- Candidats (cosinus) : **{counts['candidates']}**",
        f"- Jugés : **{counts['judged']}** "
        f"(non jugés / plafond : {counts['not_judged']})",
        f"- **Doublons sémantiques confirmés : {counts['confirmed']} paires** "
        f"portant sur **{counts['codelists_involved']} listes**",
        f"- Rejetés : {counts['rejected']} · indéterminés : {counts['undetermined']}",
    ]
    if cos_stats:
        lines.append(
            f"- Cosinus (confirmés) min/méd/max : "
            f"{cos_stats[0]:.3f} / {cos_stats[1]:.3f} / {cos_stats[2]:.3f}"
        )
    if conf_stats:
        lines.append(
            f"- Confiance (confirmés) min/méd/max : "
            f"{conf_stats[0]:.2f} / {conf_stats[1]:.2f} / {conf_stats[2]:.2f}"
        )
    lines += ["", "## Top paires confirmées", "", "| paire | cosinus | confiance |", "|---|---|---|"]
    for c, cf in top:
        lines.append(
            f"| {_name(c.canonical_a)} ↔ {_name(c.canonical_b)} "
            f"| {c.cosine:.3f} | {cf if cf is not None else ''} |"
        )
    (out_dir / f"semantic_summary_{operation}.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )
    return counts


def write_all(
    reg: Registry,
    operation: str,
    out_dir: Path,
    emit_rml: bool = False,
    near_threshold: float = 0.90,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = [
        write_canonical_list(reg, out_dir),
        write_mapping(reg, operation, out_dir),
        write_recommendations(reg, operation, out_dir),
        write_near_duplicates(reg, operation, out_dir, threshold=near_threshold),
    ]
    if emit_rml:
        paths.append(rml.write_rml(reg, operation, out_dir, threshold=near_threshold))
    return paths
