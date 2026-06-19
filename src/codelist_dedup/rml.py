"""Sortie RML/TTL — schéma repris de l'archive.

Reprend ``RML_HEADER`` et la structure ``rr:TriplesMap`` de
``archives/dedoub_deterministe/concepts_variables_alignment.py`` (``RML_HEADER``
l.512, ``rml_codelist_duplicates`` l.584), avec deux corrections :

- déclaration du préfixe ``rdfs`` (l'archive émet ``rdfs:label`` sans le
  déclarer → Turtle invalide) ;
- repli d'URN quand ``r:URN`` est absent (l'archive produisait des ``<>`` vides).

Deux sections :
- **doublons exacts** (phase 1) : ``skos:exactMatch`` de chaque liste en doublon
  vers la liste canonique à conserver (le représentant) ;
- **quasi-doublons** : ``skos:closeMatch`` entre représentants proches.
"""

from __future__ import annotations

from pathlib import Path

from .dedup import near_duplicate_pairs
from .registry import Registry

# Repris de l'archive + @prefix rdfs (corrige le bug connu).
RML_HEADER = """@prefix rr: <http://www.w3.org/ns/r2rml#> .
@prefix skos: <http://www.w3.org/2004/02/skos/core#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix ex: <http://example.org/ddi-align/> .

"""


def _uri(urn: str | None, ddi_id: str | None, version: str | None) -> str:
    """URN s'il existe, sinon repli reconstruit (évite les `<>` vides)."""
    if urn:
        return urn
    if ddi_id:
        v = version or "1"
        return f"urn:ddi:fr.insee:{ddi_id}:{v}"
    return ""


def _lit(text: str | None) -> str:
    """Échappe une chaîne pour un littéral Turtle entre guillemets."""
    s = text or ""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")


def _triples_map(name: str, subject: str, predicate: str, obj: str,
                 score: float, label: str) -> str:
    return f"""
ex:{name}
  a rr:TriplesMap ;
  rr:subjectMap [ rr:constant <{subject}> ] ;
  rr:predicateObjectMap [
    rr:predicate {predicate} ;
    rr:object <{obj}>
  ] ;
  rr:predicateObjectMap [
    rr:predicate ex:similarityScore ;
    rr:objectMap [
      rr:constant "{score:.3f}"^^xsd:decimal
    ]
  ] ;
  rr:predicateObjectMap [
    rr:predicate rdfs:label ;
    rr:objectMap [
      rr:constant "{_lit(label)}"
    ]
  ] .
"""


def write_rml(
    reg: Registry, operation: str, out_dir: Path, threshold: float = 0.90,
    semantic_model: str | None = None,
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rml_codelist_dedup_{operation}.ttl"

    with path.open("w", encoding="utf-8") as f:
        f.write(RML_HEADER)

        # --- doublons exacts : membre (de cette opération) → représentant ---
        dup_members = reg.conn.execute(
            """
            SELECT m.urn AS m_urn, m.ddi_id AS m_id, m.version AS m_ver,
                   m.codelist_name AS m_name,
                   c.rep_urn, c.rep_ddi_id, c.rep_version, c.rep_name
            FROM member m
            JOIN canonical c ON c.canonical_id = m.canonical_id
            WHERE m.operation = ?
              AND NOT (m.ddi_id = c.rep_ddi_id AND m.version = c.rep_version)
            ORDER BY m.canonical_id, m.ddi_id
            """,
            (operation,),
        ).fetchall()

        i = 0
        for m in dup_members:
            i += 1
            f.write(
                _triples_map(
                    name=f"CodeListDup{i}",
                    subject=_uri(m["m_urn"], m["m_id"], m["m_ver"]),
                    predicate="skos:exactMatch",
                    obj=_uri(m["rep_urn"], m["rep_ddi_id"], m["rep_version"]),
                    score=1.0,
                    label=f"{m['m_name']} ↔ {m['rep_name']}",
                )
            )

        # --- quasi-doublons : closeMatch entre représentants proches ---
        near = near_duplicate_pairs(reg, threshold=threshold)
        rep = {}
        for nd in near:
            for cid in (nd.canonical_a, nd.canonical_b):
                if cid not in rep:
                    rep[cid] = reg.conn.execute(
                        "SELECT rep_urn, rep_ddi_id, rep_version FROM canonical"
                        " WHERE canonical_id = ?",
                        (cid,),
                    ).fetchone()

        for j, nd in enumerate(near, 1):
            ra, rb = rep[nd.canonical_a], rep[nd.canonical_b]
            f.write(
                _triples_map(
                    name=f"CodeListNear{j}",
                    subject=_uri(ra["rep_urn"], ra["rep_ddi_id"], ra["rep_version"]),
                    predicate="skos:closeMatch",
                    obj=_uri(rb["rep_urn"], rb["rep_ddi_id"], rb["rep_version"]),
                    score=nd.score,
                    label=f"{nd.name_a} ↔ {nd.name_b}",
                )
            )

        # --- rapprochements sémantiques confirmés par le LLM (phase 2) ---
        if semantic_model:
            for k, s in enumerate(reg.confirmed_semantic_pairs(semantic_model), 1):
                f.write(
                    _triples_map(
                        name=f"CodeListSem{k}",
                        subject=_uri(s["a_urn"], s["a_id"], s["a_ver"]),
                        predicate="skos:closeMatch",
                        obj=_uri(s["b_urn"], s["b_id"], s["b_ver"]),
                        score=s["confidence"] or 0.0,
                        label=f"{s['a_name']} ↔ {s['b_name']}",
                    )
                )

    return path
