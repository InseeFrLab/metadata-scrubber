"""Pipeline complet sur une source API (faux serveur) : provenance, lignée, avertissements."""

import json

import api_fixtures
import metadata_scrubber.insee_api as insee_api
from metadata_scrubber.main import describe_api_scope, run_pipeline


def test_pipeline_from_api_serie(tmp_path, monkeypatch):
    real_client = insee_api.InseeMetadataClient

    def fake_client(cache_dir=None, **_):
        client, _fake = api_fixtures.make_client(cache_dir=cache_dir)
        assert isinstance(client, real_client)
        return client

    monkeypatch.setattr(insee_api, "InseeMetadataClient", fake_client)

    result = run_pipeline(
        audit_dir=str(tmp_path / "out"), run_llm=False, api_scope={"series": ["s1998"]},
    )

    registry = json.loads(open(result["output_files"]["codelist_duplicates.json"]).read())
    dups = {cl_id: {d["id"]: d["detection_types"] for d in entry["duplicates"]}
            for cl_id, entry in registry.items()}
    assert dups["sexe-23"] == {"sexe-24": ["exact"]}
    # Copie modifiée (un code en plus) : trouvée par la filiation BasedOnObject
    assert "lineage" in dups["contrat-23"]["contrat-24"]
    assert registry["pays"]["operations"] == ["s2023", "s2024"]
    assert registry["pays"]["series"] == ["s1998"]
    assert registry["pays"]["urn"] == "urn:ddi:fr.insee:pays:1"

    summary = json.loads(open(result["output_files"]["run_summary.json"]).read())
    assert summary["status"] == "success"
    assert summary["xml_source"] == "api:s1998"
    assert summary["api"]["skipped_operations"] == {"s2022": "pas de description DDI"}
    api_warning = next(w["reason"] for w in summary["warnings"] if w["phase"] == "api")
    assert "1 opération(s) sur 3 ignorée(s)" in api_warning
    assert "les 2 autres ont été analysées" in api_warning
    assert "description DDI" in api_warning and "s2022" in api_warning
    # Le cache des objets versionnés est à la racine des sorties, partagé entre les runs
    assert (tmp_path / "out" / "api_cache" / "codelist" / "fr.insee" / "pays" / "1.xml").exists()


def test_describe_api_scope():
    assert describe_api_scope({"all_operations": True}) == "api:toutes"
    assert describe_api_scope({"series": ["s1998"], "operations": ["s2219"]}) == "api:s1998,s2219"
    assert describe_api_scope(None) == ""


def test_warning_names_large_codelists():
    from metadata_scrubber.main import _name_list
    from metadata_scrubber.types import CodeList

    big = [CodeList(id=f"cl{i}", name=f"Liste {i}", label="", codes=[(str(c), "x") for c in range(6000)])
           for i in range(12)]
    text = _name_list(big)
    assert text.startswith("Liste 0 (6\u202f000 codes), Liste 1")
    assert text.endswith("… et 2 autres")
