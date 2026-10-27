"""corpus.py — Assemblage des objets DDI d'opérations téléchargées depuis l'API Insee.

Le corpus réunit, pour un ensemble d'opérations, les Variables de leurs fichiers
et les CodeLists (codes, catégories) que ces variables référencent, sous la
forme attendue par `extractor` (liste d'objets DDI). Il garde la provenance de
chaque CodeList : opérations et séries qui l'utilisent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from lxml import etree

from .extractor import _local, _text_of
from .insee_api import DdiRef, InseeMetadataClient, code_list_reference
from .types import CodeList


@dataclass
class Corpus:
    """Objets DDI d'un ensemble d'opérations et provenance de leurs CodeLists."""
    objects: list[etree._Element] = field(default_factory=list)
    # cl_id → {"operations": {...}, "series": {...}}
    provenance: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    operations: dict[str, dict] = field(default_factory=dict)  # op_id → infos et volumes
    skipped: dict[str, str] = field(default_factory=dict)      # op_id → raison
    restricted: list[str] = field(default_factory=list)        # objets non diffusés (401/403)

    def describe(self) -> str:
        """Résumé d'une ligne (journal, run_summary)."""
        n_cl = len(self.provenance)
        text = (
            f"{len(self.operations)} opération(s) avec DDI, {len(self.skipped)} sans, "
            f"{n_cl} CodeList(s) référencée(s)"
        )
        if self.restricted:
            text += f", {len(self.restricted)} objet(s) non diffusé(s) ignoré(s)"
        return text


def resolve_operations(
    client: InseeMetadataClient,
    operations: list[str] | None = None,
    series: list[str] | None = None,
    all_operations: bool = False,
) -> dict[str, str]:
    """Opérations à charger, avec leur série : {op_id: serie_id}.

    Une série demandée apporte toutes ses opérations, sauf si des opérations de
    cette série sont aussi demandées : seules celles-ci sont gardées. Une
    opération d'une autre série s'ajoute (sa série est demandée à l'API).
    `all_operations` parcourt toutes les séries de l'API, sans restriction.
    """
    result: dict[str, str] = {}
    wanted, restricted = set(operations or []), set(series or [])
    serie_ids = list(series or [])
    if all_operations:
        serie_ids += [s["id"] for s in client.list_series()]
    for serie_id in dict.fromkeys(serie_ids):
        info = client.serie(serie_id)
        if info is None:
            raise ValueError(f"Série inconnue de l'API : {serie_id}")
        ops = [op["id"] for op in info["operations"]]
        if serie_id in restricted:
            ops = [op for op in ops if op in wanted] or ops
        for op_id in ops:
            result.setdefault(op_id, serie_id)
    for op_id in operations or []:
        if op_id in result:
            continue
        info = client.operation(op_id)
        if info is None:
            raise ValueError(f"Opération inconnue de l'API : {op_id}")
        result[op_id] = info["serie"]["id"]
    return result


def _object_key(obj: etree._Element) -> tuple[str, str, str, str]:
    return (_local(obj), _text_of(obj, "Agency"), _text_of(obj, "ID"), _text_of(obj, "Version"))


def build_corpus(
    client: InseeMetadataClient,
    operations: dict[str, str],
    log: Callable[[str], None] = print,
) -> Corpus:
    """Télécharge les fichiers et CodeLists des opérations ({op_id: serie_id}).

    Une opération sans description DDI (404) ou dont le StudyUnit ne référence
    aucun fichier est ignorée et notée dans `skipped`. Chaque CodeList n'est
    téléchargée qu'une fois, même si plusieurs opérations la partagent.
    """
    corpus = Corpus()
    seen: set[tuple[str, str, str, str]] = set()
    codelist_refs: dict[DdiRef, None] = {}

    def add(objs: list[etree._Element]) -> None:
        for obj in objs:
            if not isinstance(obj.tag, str):
                continue
            key = _object_key(obj)
            if key not in seen:
                seen.add(key)
                corpus.objects.append(obj)

    for i, (op_id, serie_id) in enumerate(operations.items(), 1):
        files = client.operation_files(op_id)
        if not files:
            corpus.skipped[op_id] = "pas de description DDI" if files is None else "aucun fichier"
            continue
        n_vars = 0
        for file_ref in files:
            variables = [o for o in client.file_fragments(file_ref) if _local(o) == "Variable"]
            n_vars += len(variables)
            add(variables)
            for var in variables:
                ref = code_list_reference(var)
                if ref is None:
                    continue
                codelist_refs[ref] = None
                prov = corpus.provenance.setdefault(ref.id, {"operations": set(), "series": set()})
                prov["operations"].add(op_id)
                if serie_id:
                    prov["series"].add(serie_id)
        corpus.operations[op_id] = {"serie": serie_id, "files": len(files), "variables": n_vars}
        log(f"  [api] {i}/{len(operations)} {op_id} : {len(files)} fichier(s), {n_vars} variable(s)")

    for ref in codelist_refs:
        add(client.codelist_fragments(ref))
    corpus.restricted = list(client.restricted)
    log(
        f"  [api] {corpus.describe()} · {client.calls} appel(s) réseau, "
        f"{client.cache_hits} objet(s) lus en cache"
    )
    return corpus


def apply_provenance(codelists: list[CodeList], corpus: Corpus) -> None:
    """Renseigne `operations` et `series` des CodeLists depuis le corpus."""
    for cl in codelists:
        prov = corpus.provenance.get(cl.id)
        if prov:
            cl.operations = set(prov["operations"])
            cl.series = set(prov["series"])
