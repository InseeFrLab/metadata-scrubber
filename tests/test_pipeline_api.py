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
    assert any(w["phase"] == "api" for w in summary["warnings"])
    # Le cache des objets versionnés est à la racine des sorties, partagé entre les runs
    assert (tmp_path / "out" / "api_cache" / "codelist" / "fr.insee" / "pays" / "1.xml").exists()


def test_describe_api_scope():
    assert describe_api_scope({"all_operations": True}) == "api:toutes"
    assert describe_api_scope({"series": ["s1998"], "operations": ["s2219"]}) == "api:s1998,s2219"
    assert describe_api_scope(None) == ""
