"""Faux serveur de l'API Métadonnées (httpx.MockTransport) pour les tests, sans réseau.

Deux millésimes d'une série : s2023 (BTS 2023) et s2024 (BTS 2024).
- CL `sexe-23` (2023) et `sexe-24` (2024, BasedOn `sexe-23`, contenu identique) ;
- CL `contrat-23` (2023) et `contrat-24` (2024, BasedOn `contrat-23`, un code en plus) ;
- CL `pays` partagée par les deux millésimes (même identifiant) ;
- l'opération s2022 n'a pas de description DDI (404).
"""

from __future__ import annotations

import json

import httpx

from metadata_scrubber.insee_api import InseeMetadataClient

BASE = "https://api.test/metadonnees/V2"
NS = 'xmlns="ddi:logicalproduct:3_3" xmlns:r="ddi:reusable:3_3"'

CODELISTS = {
    "sexe-23": ("N_sexe_23", [("1", "Homme"), ("2", "Femme")], None),
    "sexe-24": ("N_sexe_24", [("1", "Homme"), ("2", "Femme")], "sexe-23"),
    "contrat-23": ("N_contrat_23", [("CDI", "Contrat à durée indéterminée"),
                                   ("CDD", "Contrat à durée déterminée")], None),
    "contrat-24": ("N_contrat_24", [("CDI", "Contrat à durée indéterminée"),
                                   ("CDD", "Contrat à durée déterminée"),
                                   ("APP", "Apprentissage")], "contrat-23"),
    "pays": ("N_pays", [("FR", "France"), ("DE", "Allemagne")], None),
}
OPERATIONS = {
    "s2023": {"serie": "s1998", "files": {"pi-23": ["sexe-23", "contrat-23", "pays"]}},
    "s2024": {"serie": "s1998", "files": {"pi-24": ["sexe-24", "contrat-24", "pays"]}},
}


def _ref(tag: str, obj_id: str, version: str = "1") -> str:
    return (f"<r:{tag}><r:Agency>fr.insee</r:Agency><r:ID>{obj_id}</r:ID>"
            f"<r:Version>{version}</r:Version></r:{tag}>")


def _fragments(*objects: str) -> str:
    frags = "".join(f'<Fragment xmlns="ddi:instance:3_3">{o}</Fragment>' for o in objects)
    return f'<ddi:FragmentInstance xmlns:ddi="ddi:instance:3_3">{frags}</ddi:FragmentInstance>'


def study_unit_xml(op_id: str) -> str:
    refs = "".join(_ref("PhysicalInstanceReference", pi) for pi in OPERATIONS[op_id]["files"])
    return _fragments(f'<StudyUnit xmlns="ddi:studyunit:3_3" xmlns:r="ddi:reusable:3_3">'
                      f'<r:ID>su-{op_id}</r:ID>{refs}</StudyUnit>')


def file_xml(pi_id: str) -> str:
    cls = next(f[pi_id] for op in OPERATIONS.values() for f in [op["files"]] if pi_id in f)
    variables = [
        f'<Variable {NS}><r:Agency>fr.insee</r:Agency><r:ID>var-{pi_id}-{cl}</r:ID>'
        f'<r:Version>1</r:Version><VariableName><r:String>{cl.split("-")[0]}</r:String></VariableName>'
        f'<VariableRepresentation><r:CodeRepresentation>{_ref("CodeListReference", cl)}'
        f'</r:CodeRepresentation></VariableRepresentation></Variable>'
        for cl in cls
    ]
    return _fragments(*variables)


def codelist_xml(cl_id: str) -> str:
    name, codes, based_on = CODELISTS[cl_id]
    based = (f"<r:BasedOnObject>{_ref('BasedOnReference', based_on)}</r:BasedOnObject>"
             if based_on else "")
    code_els = "".join(
        f'<Code><r:ID>{cl_id}-c{i}</r:ID>{_ref("CategoryReference", f"cat-{cl_id}-{i}")}'
        f'<r:Value>{v}</r:Value></Code>'
        for i, (v, _) in enumerate(codes)
    )
    categories = [
        f'<Category {NS}><r:Agency>fr.insee</r:Agency><r:ID>cat-{cl_id}-{i}</r:ID>'
        f'<r:Version>1</r:Version><r:Label><r:Content>{label}</r:Content></r:Label></Category>'
        for i, (_, label) in enumerate(codes)
    ]
    codelist = (f'<CodeList {NS}><r:Agency>fr.insee</r:Agency><r:ID>{cl_id}</r:ID>'
                f'<r:Version>1</r:Version>{based}<CodeListName><r:String>{name}</r:String>'
                f'</CodeListName>{code_els}</CodeList>')
    return _fragments(codelist, *categories)


def _labels(text: str) -> list[dict]:
    return [{"contenu": text, "langue": "fr"}]


class FakeApi:
    """Répond comme l'API Métadonnées ; `requests` garde les chemins appelés."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/metadonnees/V2/")
        self.requests.append(path)
        parts = path.split("/")
        if path == "operations/series":
            return httpx.Response(200, json=[{"seriesId": "s1998", "label": _labels("BTS")}])
        if parts[:2] == ["operations", "serie"] and parts[2] == "s1998":
            return httpx.Response(200, json={
                "seriesId": "s1998", "label": _labels("Base Tous salariés"),
                "famille": {"id": "s12", "label": _labels("Emploi")},
                "operations": [{"id": op, "label": _labels(op)} for op in ["s2022", *OPERATIONS]],
            })
        if parts[:2] == ["operations", "operation"] and parts[2] in {"s2022", *OPERATIONS}:
            return httpx.Response(200, json={
                "id": parts[2], "label": _labels(parts[2]),
                "serie": {"id": "s1998", "label": _labels("BTS")},
            })
        if parts[:2] == ["structures", "operation"] and parts[2] in OPERATIONS:
            return httpx.Response(200, text=study_unit_xml(parts[2]))
        if parts[:2] == ["structures", "codelist"] and parts[3] == "restreinte":
            return httpx.Response(401)
        if parts[:2] == ["structures", "fichier"]:
            return httpx.Response(200, text=file_xml(parts[3]))
        if parts[:2] == ["structures", "codelist"] and parts[3] in CODELISTS:
            return httpx.Response(200, text=codelist_xml(parts[3]))
        if parts[0] == "geo":
            return httpx.Response(200, content=json.dumps(
                [{"code": "11", "intitule": "Île-de-France"}, {"code": "84", "intitule": "Auvergne-Rhône-Alpes"}]
            ))
        return httpx.Response(404)


def make_client(cache_dir: str | None = None) -> tuple[InseeMetadataClient, FakeApi]:
    fake = FakeApi()
    http = httpx.Client(transport=httpx.MockTransport(fake))
    return InseeMetadataClient(base_url=BASE, cache_dir=cache_dir, http=http), fake


def ddi_file(codelists: dict[str, tuple[str, list[tuple[str, str]], str | None]]) -> bytes:
    """Fichier DDI contenant les CodeLists données ({id: (nom, codes, based_on)})."""
    saved = dict(CODELISTS)
    CODELISTS.update(codelists)
    try:
        bodies = [codelist_xml(cl_id) for cl_id in codelists]
    finally:
        CODELISTS.clear()
        CODELISTS.update(saved)
    inner = "".join(b.split(">", 1)[1].rsplit("</ddi:FragmentInstance>", 1)[0] for b in bodies)
    return f'<ddi:FragmentInstance xmlns:ddi="ddi:instance:3_3">{inner}</ddi:FragmentInstance>'.encode()
