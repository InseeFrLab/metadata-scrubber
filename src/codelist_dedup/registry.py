"""Registre canonique incrémental des listes de codes (SQLite).

Source de vérité du dédoublonnage cumulatif. Un identifiant canonique est dérivé
du contenu (``sig_pairs``) → stable et idempotent : réingérer une opération
n'ajoute ni canon ni membre en double.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .model import CodeListRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS canonical (
    canonical_id   TEXT PRIMARY KEY,
    sig_pairs      TEXT UNIQUE NOT NULL,
    sig_values     TEXT NOT NULL,
    sig_name       TEXT NOT NULL,
    display_name   TEXT NOT NULL,
    label          TEXT,
    n_pairs        INTEGER NOT NULL,
    is_empty       INTEGER NOT NULL,
    first_seen_op  TEXT NOT NULL,
    rep_urn        TEXT,
    rep_ddi_id     TEXT,
    rep_version    TEXT,
    rep_name       TEXT
);
CREATE TABLE IF NOT EXISTS canonical_pairs (
    canonical_id   TEXT NOT NULL REFERENCES canonical(canonical_id),
    ord            INTEGER NOT NULL,
    value          TEXT NOT NULL,
    label          TEXT,
    PRIMARY KEY (canonical_id, ord)
);
CREATE TABLE IF NOT EXISTS member (
    canonical_id   TEXT NOT NULL REFERENCES canonical(canonical_id),
    operation      TEXT NOT NULL,
    ddi_id         TEXT NOT NULL,
    version        TEXT NOT NULL,
    urn            TEXT,
    source_id      TEXT,
    codelist_name  TEXT,
    n_pairs        INTEGER NOT NULL,
    n_unresolved   INTEGER NOT NULL,
    sig_pairs      TEXT NOT NULL,
    sig_values     TEXT NOT NULL,
    PRIMARY KEY (operation, ddi_id, version)
);
CREATE TABLE IF NOT EXISTS run (
    operation      TEXT NOT NULL,
    source         TEXT NOT NULL,
    n_codelists    INTEGER NOT NULL,
    n_new          INTEGER NOT NULL,
    n_merged       INTEGER NOT NULL,
    n_unresolved   INTEGER NOT NULL,
    ts             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS embedding (
    canonical_id   TEXT NOT NULL REFERENCES canonical(canonical_id),
    model          TEXT NOT NULL,
    dim            INTEGER NOT NULL,
    vector         BLOB NOT NULL,
    PRIMARY KEY (canonical_id, model)
);
CREATE TABLE IF NOT EXISTS semantic_verdict (
    canonical_a    TEXT NOT NULL,
    canonical_b    TEXT NOT NULL,
    model          TEXT NOT NULL,
    cosine         REAL NOT NULL,
    same_concept   INTEGER,
    confidence     REAL,
    rationale      TEXT,
    ts             TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (canonical_a, canonical_b, model)
);
CREATE TABLE IF NOT EXISTS review (
    entity_type    TEXT NOT NULL,   -- 'canonical' | 'semantic'
    entity_key     TEXT NOT NULL,   -- canonical_id, ou '<a>|<b>' (ids triés)
    decision       TEXT NOT NULL,   -- 'accepted' | 'rejected' | 'pending'
    note           TEXT,
    author         TEXT,
    ts             TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (entity_type, entity_key)
);
CREATE INDEX IF NOT EXISTS idx_canonical_sig_values ON canonical(sig_values);
CREATE INDEX IF NOT EXISTS idx_member_canonical ON member(canonical_id);
"""


def canonical_id_for(sig_pairs: str) -> str:
    return "cl-" + sig_pairs[:16]


def pair_key(a: str, b: str) -> str:
    """Clé stable d'une paire de canons (ids triés)."""
    x, y = sorted((a, b))
    return f"{x}|{y}"


@dataclass
class FoldResult:
    n_codelists: int
    n_new: int  # nouveaux canons créés par cette opération
    n_merged: int  # listes rattachées à un canon existant (doublon inter-op)
    n_intra_dup: int  # listes en doublon parfait au sein de l'opération
    n_unresolved: int


class Registry:
    # Colonnes de `canonical` ajoutées au fil des versions (migration additive).
    _CANONICAL_COLUMNS = ("label", "rep_urn", "rep_ddi_id", "rep_version", "rep_name")

    def __init__(self, path: str) -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Ajoute les colonnes manquantes aux bases antérieures (ALTER additif)."""
        existing = {
            row[1] for row in self.conn.execute("PRAGMA table_info(canonical)")
        }
        with self.conn:
            for col in self._CANONICAL_COLUMNS:
                if col not in existing:
                    self.conn.execute(
                        f"ALTER TABLE canonical ADD COLUMN {col} TEXT"
                    )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Registry:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ fold-in
    def fold_in(
        self, operation: str, source: str, records: list[CodeListRecord]
    ) -> FoldResult:
        """Intègre une opération dans le registre (transaction atomique)."""
        n_new = n_merged = n_intra_dup = n_unresolved = 0
        seen_sig_in_op: set[str] = set()

        with self.conn:  # transaction
            for rec in records:
                n_unresolved += rec.n_unresolved
                cid = canonical_id_for(rec.sig_pairs)

                existed = self._canonical_exists(rec.sig_pairs)
                if not existed:
                    self._insert_canonical(cid, operation, rec)
                    n_new += 1
                else:
                    if rec.sig_pairs in seen_sig_in_op:
                        n_intra_dup += 1
                    elif not self._created_this_op(cid, operation):
                        n_merged += 1
                seen_sig_in_op.add(rec.sig_pairs)

                self._upsert_member(cid, operation, rec)

            self.conn.execute(
                "INSERT INTO run (operation, source, n_codelists, n_new, n_merged,"
                " n_unresolved) VALUES (?,?,?,?,?,?)",
                (operation, source, len(records), n_new, n_merged, n_unresolved),
            )

        return FoldResult(
            n_codelists=len(records),
            n_new=n_new,
            n_merged=n_merged,
            n_intra_dup=n_intra_dup,
            n_unresolved=n_unresolved,
        )

    # ----------------------------------------------------- contenu / phase 2
    def canonical_content(
        self, cid: str
    ) -> tuple[str, str, list[tuple[str, str | None]]]:
        """(display_name, label, [(value, label_code), ...]) d'un canon."""
        row = self.conn.execute(
            "SELECT display_name, label FROM canonical WHERE canonical_id = ?", (cid,)
        ).fetchone()
        name = row["display_name"] if row else ""
        clabel = (row["label"] if row else "") or ""
        pairs = [
            (r["value"], r["label"])
            for r in self.conn.execute(
                "SELECT value, label FROM canonical_pairs WHERE canonical_id = ?"
                " ORDER BY ord",
                (cid,),
            )
        ]
        return name, clabel, pairs

    def all_canonical_ids(self, include_empty: bool = False) -> list[str]:
        sql = "SELECT canonical_id FROM canonical"
        if not include_empty:
            sql += " WHERE is_empty = 0"
        return [r[0] for r in self.conn.execute(sql)]

    def missing_embedding_ids(self, model: str) -> list[str]:
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT canonical_id FROM canonical c WHERE c.is_empty = 0"
                " AND NOT EXISTS (SELECT 1 FROM embedding e"
                " WHERE e.canonical_id = c.canonical_id AND e.model = ?)",
                (model,),
            )
        ]

    def store_embedding(self, cid: str, model: str, dim: int, blob: bytes) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO embedding (canonical_id, model, dim, vector)"
                " VALUES (?,?,?,?)",
                (cid, model, dim, blob),
            )

    def load_embeddings(self, model: str) -> list[tuple[str, bytes]]:
        return [
            (r["canonical_id"], r["vector"])
            for r in self.conn.execute(
                "SELECT canonical_id, vector FROM embedding WHERE model = ?", (model,)
            )
        ]

    def has_verdict(self, a: str, b: str, model: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM semantic_verdict WHERE canonical_a=? AND canonical_b=?"
                " AND model=?",
                (a, b, model),
            ).fetchone()
            is not None
        )

    def store_verdict(
        self, a: str, b: str, model: str, cosine: float,
        same_concept: bool | None, confidence: float | None, rationale: str | None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO semantic_verdict (canonical_a, canonical_b,"
                " model, cosine, same_concept, confidence, rationale)"
                " VALUES (?,?,?,?,?,?,?)",
                (
                    a, b, model, cosine,
                    None if same_concept is None else int(same_concept),
                    confidence, rationale,
                ),
            )

    def confirmed_semantic_pairs(self, model: str) -> list[dict]:
        return [
            dict(r)
            for r in self.conn.execute(
                "SELECT v.*, ca.rep_urn AS a_urn, ca.rep_ddi_id AS a_id,"
                " ca.rep_version AS a_ver, ca.display_name AS a_name,"
                " cb.rep_urn AS b_urn, cb.rep_ddi_id AS b_id,"
                " cb.rep_version AS b_ver, cb.display_name AS b_name"
                " FROM semantic_verdict v"
                " JOIN canonical ca ON ca.canonical_id = v.canonical_a"
                " JOIN canonical cb ON cb.canonical_id = v.canonical_b"
                " WHERE v.model = ? AND v.same_concept = 1"
                " ORDER BY v.confidence DESC, v.cosine DESC",
                (model,),
            )
        ]

    # -------------------------------------------------- parcours / révision
    def browse_canonicals(
        self, *, operation: str | None = None, redundant: bool = False,
        min_members: int = 0, search: str | None = None, include_empty: bool = True,
        sort: str = "members", limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        where, having, params = [], [], []
        if not include_empty:
            where.append("c.is_empty = 0")
        if search:
            where.append("(lower(c.display_name) LIKE ? OR lower(c.label) LIKE ?)")
            like = f"%{search.lower()}%"
            params += [like, like]
        having_params: list = []
        if operation:
            having.append("SUM(CASE WHEN m.operation = ? THEN 1 ELSE 0 END) > 0")
            having_params.append(operation)
        if redundant:
            having.append("COUNT(m.ddi_id) > 1")
        if min_members:
            having.append("COUNT(m.ddi_id) >= ?")
            having_params.append(min_members)
        order = {
            "members": "n_members DESC, c.n_pairs DESC",
            "pairs": "c.n_pairs DESC",
            "name": "c.display_name ASC",
        }.get(sort, "n_members DESC")
        sql = (
            "SELECT c.canonical_id, c.display_name, c.label, c.n_pairs, c.is_empty,"
            " c.first_seen_op, COUNT(m.ddi_id) AS n_members,"
            " COUNT(DISTINCT m.operation) AS n_operations, rv.decision AS review"
            " FROM canonical c"
            " LEFT JOIN member m ON m.canonical_id = c.canonical_id"
            " LEFT JOIN review rv ON rv.entity_type='canonical'"
            "   AND rv.entity_key = c.canonical_id"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " GROUP BY c.canonical_id"
            + (" HAVING " + " AND ".join(having) if having else "")
            + f" ORDER BY {order} LIMIT ? OFFSET ?"
        )
        rows = self.conn.execute(
            sql, [*params, *having_params, limit, offset]
        ).fetchall()
        return [dict(r) for r in rows]

    def browse_semantic(
        self, *, confirmed_only: bool = False, min_cosine: float = 0.0,
        limit: int = 50, offset: int = 0,
    ) -> list[dict]:
        where = ["v.cosine >= ?"]
        params: list = [min_cosine]
        if confirmed_only:
            where.append("v.same_concept = 1")
        rows = self.conn.execute(
            "SELECT v.canonical_a, v.canonical_b, v.model, v.cosine, v.same_concept,"
            " v.confidence, ca.display_name AS a_name, cb.display_name AS b_name"
            " FROM semantic_verdict v"
            " JOIN canonical ca ON ca.canonical_id = v.canonical_a"
            " JOIN canonical cb ON cb.canonical_id = v.canonical_b"
            " WHERE " + " AND ".join(where)
            + " ORDER BY v.confidence DESC, v.cosine DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["review"] = self.get_review("semantic", pair_key(r["canonical_a"], r["canonical_b"]))
            out.append(d)
        return out

    def semantic_for_canonical(self, cid: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT v.canonical_a, v.canonical_b, v.cosine, v.same_concept,"
            " v.confidence, ca.display_name AS a_name, cb.display_name AS b_name"
            " FROM semantic_verdict v"
            " JOIN canonical ca ON ca.canonical_id = v.canonical_a"
            " JOIN canonical cb ON cb.canonical_id = v.canonical_b"
            " WHERE v.canonical_a = ? OR v.canonical_b = ?"
            " ORDER BY v.confidence DESC, v.cosine DESC",
            (cid, cid),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_canonical_field(self, cid: str, field: str, value: str) -> None:
        if field not in ("display_name", "label"):
            raise ValueError(f"Champ non modifiable : {field}")
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE canonical SET {field} = ? WHERE canonical_id = ?", (value, cid)
            )
            if cur.rowcount == 0:
                raise KeyError(f"Canon introuvable : {cid}")

    def set_representative(
        self, cid: str, ddi_id: str, version: str | None = None
    ) -> dict:
        q = (
            "SELECT ddi_id, version, urn, codelist_name FROM member"
            " WHERE canonical_id = ? AND ddi_id = ?"
        )
        args: list = [cid, ddi_id]
        if version is not None:
            q += " AND version = ?"
            args.append(version)
        q += " ORDER BY version DESC LIMIT 1"
        m = self.conn.execute(q, args).fetchone()
        if m is None:
            raise KeyError(f"Aucun membre {ddi_id} pour le canon {cid}")
        with self.conn:
            self.conn.execute(
                "UPDATE canonical SET rep_urn=?, rep_ddi_id=?, rep_version=?,"
                " rep_name=? WHERE canonical_id=?",
                (m["urn"], m["ddi_id"], m["version"], m["codelist_name"], cid),
            )
        return dict(m)

    def set_review(
        self, entity_type: str, key: str, decision: str,
        note: str | None, author: str | None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO review (entity_type, entity_key, decision, note, author)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(entity_type, entity_key) DO UPDATE SET"
                " decision=excluded.decision, note=excluded.note,"
                " author=excluded.author, ts=datetime('now')",
                (entity_type, key, decision, note, author),
            )

    def get_review(self, entity_type: str, key: str) -> str | None:
        r = self.conn.execute(
            "SELECT decision FROM review WHERE entity_type=? AND entity_key=?",
            (entity_type, key),
        ).fetchone()
        return r[0] if r else None

    # ---------------------------------------------------------------- internals
    def _canonical_exists(self, sig_pairs: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM canonical WHERE sig_pairs = ?", (sig_pairs,)
        ).fetchone()
        return row is not None

    def _created_this_op(self, canonical_id: str, operation: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM canonical WHERE canonical_id = ? AND first_seen_op = ?",
            (canonical_id, operation),
        ).fetchone()
        return row is not None

    def _insert_canonical(
        self, cid: str, operation: str, rec: CodeListRecord
    ) -> None:
        self.conn.execute(
            "INSERT INTO canonical (canonical_id, sig_pairs, sig_values, sig_name,"
            " display_name, label, n_pairs, is_empty, first_seen_op,"
            " rep_urn, rep_ddi_id, rep_version, rep_name)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                cid,
                rec.sig_pairs,
                rec.sig_values,
                rec.sig_name,
                rec.name or rec.source_id or cid,
                rec.label,
                rec.n_pairs,
                int(rec.is_empty),
                operation,
                rec.urn,
                rec.ddi_id,
                rec.version,
                rec.name,
            ),
        )
        self.conn.executemany(
            "INSERT INTO canonical_pairs (canonical_id, ord, value, label)"
            " VALUES (?,?,?,?)",
            [
                (cid, i, c.value, c.label)
                for i, c in enumerate(rec.codes)
            ],
        )

    def _upsert_member(
        self, cid: str, operation: str, rec: CodeListRecord
    ) -> None:
        self.conn.execute(
            "INSERT INTO member (canonical_id, operation, ddi_id, version, urn,"
            " source_id, codelist_name, n_pairs, n_unresolved, sig_pairs, sig_values)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(operation, ddi_id, version) DO UPDATE SET"
            " canonical_id=excluded.canonical_id, sig_pairs=excluded.sig_pairs,"
            " sig_values=excluded.sig_values",
            (
                cid,
                operation,
                rec.ddi_id,
                rec.version,
                rec.urn,
                rec.source_id,
                rec.name,
                rec.n_pairs,
                rec.n_unresolved,
                rec.sig_pairs,
                rec.sig_values,
            ),
        )
