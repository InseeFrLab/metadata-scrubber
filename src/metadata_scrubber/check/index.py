"""index.py — Index de référence des CodeLists publiées dans l'API Métadonnées.

L'API n'offre ni recherche ni liste globale des CodeLists : on parcourt
séries → opérations → fichiers → CodeLists une fois (`build_index`), on stocke
le résultat (`reference_index.json.gz`, local ou S3), et chaque contrôle compare
un fichier à cet index chargé en mémoire (`ReferenceIndex`).

Les signatures ne sont pas stockées : elles sont recalculées au chargement avec
`funnel.prepare_signatures`, pour suivre toute évolution de la normalisation.
"""

from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable

from ..corpus import apply_provenance, build_corpus, resolve_operations
from ..extractor import extract_codelists, extract_variables, read_bytes
from ..funnel import prepare_signatures
from ..insee_api import InseeMetadataClient
from ..levels import COMMUNE, LEVEL_RANK, OFFICIELLE, SPECIFIQUE, suggest_level  # noqa: F401
from ..normalize import normalize
from ..quality import split_codes
from ..registry_io import write_bytes
from ..types import CodeList

INDEX_VERSION = 1
DEFAULT_INDEX_PATH = "reference_index.json.gz"


# Nomenclatures géographiques (COG) importées comme listes officielles :
# (chemin /geo/{kind}, nom de la liste)
GEO_LISTS = [
    ("regions", "Régions (COG)"),
    ("departements", "Départements (COG)"),
    ("communes", "Communes (COG)"),
    ("arrondissements", "Arrondissements (COG)"),
    ("cantons", "Cantons (COG)"),
    ("collectivitesDOutreMer", "Collectivités d'outre-mer (COG)"),
    ("pays", "Pays et territoires étrangers (COG)"),
    ("intercommunalites", "Intercommunalités (EPCI)"),
    ("zonesDEmploi2020", "Zones d'emploi 2020"),
    ("unitesUrbaines2020", "Unités urbaines 2020"),
    ("airesDAttractionDesVilles2020", "Aires d'attraction des villes 2020"),
    ("bassinsDeVie2022", "Bassins de vie 2022"),
    ("quartiersPrioritairesDeLaPolitiqueDeLaVille2024", "Quartiers prioritaires de la politique de la ville 2024"),
]


def default_index_path() -> str:
    return os.environ.get("SCRUBBER_CHECK_INDEX", DEFAULT_INDEX_PATH)


# ----------------------------------------------------------------------
# Construction
# ----------------------------------------------------------------------

def _entry(cl: CodeList, vars_count: int = 0) -> dict[str, Any]:
    return {
        "id": cl.id,
        "agency": cl.agency,
        "version": cl.version,
        "urn": cl.urn,
        "name": cl.name,
        "label": cl.label,
        "codes": [list(pair) for pair in cl.codes],
        "based_on": cl.based_on,
        "operations": sorted(cl.operations),
        "series": sorted(cl.series),
        "vars_count": vars_count,
        "origin": cl.origin,
        "level": suggest_level(cl),
    }


def geo_codelists(
    client: InseeMetadataClient, date: str | None = None, kinds: list[str] | None = None
) -> list[CodeList]:
    """Listes officielles du COG (code, intitulé) à la date donnée (défaut : aujourd'hui).

    `kinds` restreint aux chemins /geo/{kind} donnés (défaut : toutes, voir GEO_LISTS).
    """
    result = []
    for kind, name in GEO_LISTS:
        if kinds is not None and kind not in kinds:
            continue
        territories = client.geo_list(kind, date)
        if territories:
            result.append(CodeList(
                id=f"insee-geo:{kind}",
                name=name,
                label=name,
                codes=[(t["code"], t["intitule"]) for t in territories],
                origin="api-geo",
            ))
    return result


def build_index(
    client: InseeMetadataClient,
    *,
    series: list[str] | None = None,
    operations: list[str] | None = None,
    with_geo: bool = True,
    geo_date: str | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Construit l'index : toute la base par défaut, ou les séries / opérations données."""
    all_operations = not series and not operations
    ops = resolve_operations(client, operations=operations, series=series, all_operations=all_operations)
    log(f"[index] {len(ops)} opération(s) à parcourir.")
    corpus = build_corpus(client, ops, log=log)

    codelists = extract_codelists(corpus.objects)
    apply_provenance(codelists, corpus)
    vars_count: dict[str, int] = defaultdict(int)
    for var in extract_variables(corpus.objects):
        if var.cl_id:
            vars_count[var.cl_id] += 1
    entries = [_entry(cl, vars_count[cl.id]) for cl in codelists]
    if with_geo:
        geo = geo_codelists(client, geo_date)
        entries += [_entry(cl) for cl in geo]
        log(f"[index] {len(geo)} liste(s) officielle(s) du COG.")

    levels = defaultdict(int)
    for e in entries:
        levels[e["level"]] += 1
    return {
        "version": INDEX_VERSION,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "api_base_url": client.base_url,
        "scope": "toute la base" if all_operations else ", ".join([*(series or []), *(operations or [])]),
        "operations": corpus.operations,
        "skipped_operations": corpus.skipped,
        "restricted_objects": corpus.restricted,
        "counts": {
            "operations": len(corpus.operations),
            "operations_without_ddi": len(corpus.skipped),
            "restricted_objects": len(corpus.restricted),
            "codelists": len(entries),
            "by_level": dict(levels),
        },
        "codelists": entries,
    }


def write_index(doc: dict[str, Any], path: str) -> None:
    """Écrit l'index (JSON compressé si le chemin finit par .gz), local ou S3."""
    data = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    write_bytes(gzip.compress(data) if path.endswith(".gz") else data, path)


def read_index(path: str) -> dict[str, Any]:
    raw = read_bytes(path)
    if path.endswith(".gz"):
        raw = gzip.decompress(raw)
    doc = json.loads(raw)
    if doc.get("version") != INDEX_VERSION:
        raise ValueError(f"Version d'index non prise en charge : {doc.get('version')} ({path})")
    return doc


def index_metadata(doc: dict[str, Any]) -> dict[str, Any]:
    """En-tête de l'index (sans les listes)."""
    return {k: v for k, v in doc.items() if k != "codelists"}


# ----------------------------------------------------------------------
# Index en mémoire
# ----------------------------------------------------------------------

def _codelist_from_entry(e: dict[str, Any]) -> CodeList:
    cl = CodeList(
        id=e["id"],
        name=e.get("name", ""),
        label=e.get("label", ""),
        codes=[(str(p[0]), str(p[1]) if len(p) > 1 else "") for p in e.get("codes", [])],
        agency=e.get("agency", ""),
        version=e.get("version", ""),
        based_on=list(e.get("based_on", [])),
        operations=set(e.get("operations", [])),
        series=set(e.get("series", [])),
        origin=e.get("origin", "api"),
    )
    cl.vars = [""] * int(e.get("vars_count", 0))  # seul le nombre est connu
    return cl


class ReferenceIndex:
    """CodeLists de référence et index inversés pour des recherches en O(1)."""

    def __init__(self, codelists: list[CodeList], levels: dict[str, str], meta: dict[str, Any]):
        self.codelists = codelists
        self.levels = levels
        self.meta = meta
        prepare_signatures(codelists)

        self.by_id: dict[str, CodeList] = {}
        self.by_sig: dict[tuple, list[CodeList]] = defaultdict(list)
        self.by_business: dict[tuple, list[CodeList]] = defaultdict(list)
        self.by_labels: dict[tuple, list[CodeList]] = defaultdict(list)
        self.by_pair: dict[tuple[str, str], list[CodeList]] = defaultdict(list)
        self.by_value: dict[str, list[CodeList]] = defaultdict(list)
        self.by_label: dict[str, list[CodeList]] = defaultdict(list)
        self.business_pairs: dict[str, frozenset[tuple[str, str]]] = {}

        for cl in codelists:
            self.by_id[cl.id] = cl
            self.by_sig[cl.sig].append(cl)
            if cl.sig_business:
                self.by_business[cl.sig_business].append(cl)
            if cl.sig_labels:
                self.by_labels[cl.sig_labels].append(cl)
            business, _ = split_codes(cl.codes)
            pairs = frozenset((normalize(v), normalize(lab)) for v, lab in business)
            self.business_pairs[cl.id] = pairs
            for pair in pairs:
                self.by_pair[pair].append(cl)
            for value, label in pairs:
                self.by_value[value].append(cl)
                if label:
                    self.by_label[label].append(cl)

    @classmethod
    def from_doc(cls, doc: dict[str, Any]) -> "ReferenceIndex":
        codelists = [_codelist_from_entry(e) for e in doc.get("codelists", [])]
        levels = {e["id"]: e.get("level", "") for e in doc.get("codelists", [])}
        return cls(codelists, levels, index_metadata(doc))

    @classmethod
    def load(cls, path: str) -> "ReferenceIndex":
        return cls.from_doc(read_index(path))

    def level(self, cl: CodeList) -> str:
        return self.levels.get(cl.id, "")

    def restrict(
        self,
        series: list[str] | None = None,
        operations: list[str] | None = None,
        exclude_operations: list[str] | None = None,
    ) -> "ReferenceIndex":
        """Sous-index : listes des séries / opérations données (les officielles restent).

        `exclude_operations` retire les listes utilisées *uniquement* par ces
        opérations (re-contrôle d'une opération déjà publiée).
        """
        if not (series or operations or exclude_operations):
            return self
        series_set, ops_set = set(series or []), set(operations or [])
        excluded = set(exclude_operations or [])

        def keep(cl: CodeList) -> bool:
            if self.level(cl) == OFFICIELLE:
                return True
            if excluded and cl.operations and cl.operations <= excluded:
                return False
            if series_set or ops_set:
                return bool(cl.series & series_set or cl.operations & ops_set)
            return True

        kept = [cl for cl in self.codelists if keep(cl)]
        meta = {**self.meta, "restricted_to": {
            "series": sorted(series_set), "operations": sorted(ops_set),
            "excluded_operations": sorted(excluded),
        }}
        return ReferenceIndex(kept, self.levels, meta)
