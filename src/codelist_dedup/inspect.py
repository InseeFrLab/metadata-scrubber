"""Inspection d'un fichier source DDI : comptes par type + export parquet.

Outil de diagnostic indépendant du registre : parse un fichier source (local ou
``s3://``) et produit deux DataFrames exportables en parquet —
``objects`` (1 ligne par objet, tous types) et ``codes`` (1 ligne par code des
CodeList, libellé de catégorie résolu).
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import BinaryIO

import pandas as pd

from .extract import extract_all

_OBJECT_COLUMNS = [
    "type", "id", "urn", "version", "source_id", "name", "label",
    "description", "n_codes",
]
_CODE_COLUMNS = [
    "codelist_id", "codelist_name", "codelist_label", "value", "code_label",
    "category_id",
]


def objects_dataframe(objects: list[dict]) -> pd.DataFrame:
    """1 ligne par objet DDI (tous types)."""
    rows = [
        {
            "type": o["type"],
            "id": o["id"],
            "urn": o["urn"],
            "version": o["version"],
            "source_id": o["source_id"],
            "name": o["name"],
            "label": o["label"],
            "description": o["description"],
            "n_codes": len(o["codes"]) if o["type"] == "CodeList" else None,
        }
        for o in objects
    ]
    return pd.DataFrame(rows, columns=_OBJECT_COLUMNS)


def codes_dataframe(objects: list[dict]) -> pd.DataFrame:
    """1 ligne par code des CodeList (libellés de catégorie résolus)."""
    rows = [
        {
            "codelist_id": o["id"],
            "codelist_name": o["name"],
            "codelist_label": o["label"],
            "value": c["value"],
            "code_label": c.get("code_label"),
            "category_id": c["category_id"],
        }
        for o in objects
        if o["type"] == "CodeList"
        for c in o["codes"]
    ]
    return pd.DataFrame(rows, columns=_CODE_COLUMNS)


def run_inspect(
    stream: BinaryIO, operation: str, out_dir: Path,
    write_parquet: bool = True,
) -> tuple[Counter, pd.DataFrame, pd.DataFrame, list[Path]]:
    """Parse le flux, construit les DataFrames, écrit les parquet (si demandé)."""
    objects, counts = extract_all(stream)
    df_objects = objects_dataframe(objects)
    df_codes = codes_dataframe(objects)

    paths: list[Path] = []
    if write_parquet:
        out_dir.mkdir(parents=True, exist_ok=True)
        p_obj = out_dir / f"objects_{operation}.parquet"
        p_codes = out_dir / f"codes_{operation}.parquet"
        df_objects.to_parquet(p_obj, index=False)
        df_codes.to_parquet(p_codes, index=False)
        paths = [p_obj, p_codes]

    return counts, df_objects, df_codes, paths
