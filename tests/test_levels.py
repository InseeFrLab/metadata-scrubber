"""Niveaux de CodeLists, choix de la maître, nomenclatures officielles (phase B)."""

import json

import api_fixtures
import metadata_scrubber.insee_api as insee_api
from api_fixtures import ddi_file
from metadata_scrubber.cleaned_registry import (
    load_cleaned_codelists,
    migrate_cleaned_doc,
    sync_cleaned_registry,
    validate_cleaned_doc,
)
from metadata_scrubber.levels import master_rank, suggest_level, suggest_official
from metadata_scrubber.main import run_pipeline, seed_officielles
from metadata_scrubber.types import CodeList


def _cl(cl_id, **kw):
    return CodeList(id=cl_id, name=kw.pop("name", cl_id), label="", **kw)


def test_suggest_level():
    assert suggest_level(_cl("a", series={"s1", "s2"})) == "commune"
    assert suggest_level(_cl("b", series={"s1"})) == "specifique"
    assert suggest_level(_cl("c")) == ""
    assert suggest_level(_cl("d", origin="api-geo")) == "officielle"
    assert suggest_level(_cl("e", series={"s1", "s2"}, level="officielle")) == "officielle"


def test_master_rank_orders_masters():
    xml_one_serie = _cl("xml1", series={"s1"}, vars=["v"])
    xml_two_series = _cl("xml2", series={"s1", "s2"})
    xml_many_vars = _cl("xml3", series={"s1"}, vars=["v1", "v2", "v3"])
    registry_plain = _cl("reg", origin="registry")
    registry_official = _cl("off", origin="registry", level="officielle")
    lists = [xml_one_serie, xml_two_series, xml_many_vars, registry_plain, registry_official]

    ordered = sorted(lists, key=lambda cl: master_rank(cl))
    assert [cl.id for cl in ordered] == ["off", "reg", "xml2", "xml3", "xml1"]
    # La maître choisie par l'expert passe devant à niveau égal
    ordered = sorted(lists, key=lambda cl: master_rank(cl, {"xml1"}))
    assert [cl.id for cl in ordered][2:] == ["xml1", "xml2", "xml3"]


def test_suggest_official():
    officials = [_cl("r", name="Régions (COG)"), _cl("d", name="Départements (COG)"),
                 _cl("p", name="Pays et territoires étrangers (COG)")]
    assert suggest_official(_cl("x", name="N-region"), officials).id == "r"
    assert suggest_official(_cl("x", name="N_Départements"), officials).id == "d"
    assert suggest_official(_cl("x", name="N-pays_naissance"), officials).id == "p"
    assert suggest_official(_cl("x", name="N-uu_23"), officials) is None


def test_migrate_v2_to_v3_and_validate():
    doc = migrate_cleaned_doc({"version": 2, "codelists": {"a": {"id": "a", "name": "A", "codes": []}}})
    assert doc["version"] == 3
    entry = doc["codelists"]["a"]
    assert entry["level"] == "" and entry["operations"] == [] and entry["replaces"] == []
    entry["level"] = "nationale"
    assert any("level" in e for e in validate_cleaned_doc(doc))


def test_sync_creates_v3_entries_with_provenance():
    registry = {"a": {"id": "a", "name": "A", "codes": [["1", "x"]], "urn": "urn:ddi:fr.insee:a:2",
                      "agency": "fr.insee", "version": "2", "operations": ["s1", "s2"],
                      "series": ["S1", "S2"],
                      "duplicates": [{"id": "b", "name": "B", "decision": "approve"}]}}
    cleaned, _ = sync_cleaned_registry(registry, None, "reg.json")
    entry = cleaned["codelists"]["a"]
    assert (entry["source"], entry["level"], entry["level_suggested"]) == ("validation", "", "commune")
    assert entry["urn"] == "urn:ddi:fr.insee:a:2" and entry["series"] == ["S1", "S2"]


def _fake_client(monkeypatch):
    monkeypatch.setattr(insee_api, "InseeMetadataClient",
                        lambda cache_dir=None, **_: api_fixtures.make_client(cache_dir)[0])


def test_seed_officielles(tmp_path, monkeypatch):
    _fake_client(monkeypatch)
    path = str(tmp_path / "cleaned_codelists.json")

    result = seed_officielles(path, geo=["regions"], codelists=["fr.insee/pays"])
    assert sorted(result["added"]) == ["insee-geo:regions", "pays"]
    doc = json.loads(open(path, encoding="utf-8").read())
    regions = doc["codelists"]["insee-geo:regions"]
    assert (regions["level"], regions["source"]) == ("officielle", "api-geo")
    assert regions["codes"][0] == ["11", "Île-de-France"]
    assert doc["codelists"]["pays"]["source"] == "api-structures"

    # Relance : rien de nouveau ; l'entrée rechargée porte son niveau
    assert seed_officielles(path, geo=["regions"]) == {"added": [], "updated": []}
    cls, _ = load_cleaned_codelists(path)
    assert {cl.id: cl.level for cl in cls}["insee-geo:regions"] == "officielle"


def test_pipeline_official_is_master_and_placeholder_is_linked(tmp_path, monkeypatch):
    _fake_client(monkeypatch)
    out = tmp_path / "out"
    cleaned_path = str(out / "cleaned_codelists.json")
    seed_officielles(cleaned_path, geo=["regions"])

    xml = tmp_path / "op.xml"
    xml.write_bytes(ddi_file({
        # Copie locale de la nomenclature des régions : l'officielle doit être maître
        "reg-copie": ("N_regions_23", [("11", "Île-de-France"), ("84", "Auvergne-Rhône-Alpes")], None),
        # Renvoi « Voir dictionnaire » : rattaché à la nomenclature officielle
        "reg-renvoi": ("N-region", [("cf", "Voir dictionnaire des nomenclatures")], None),
    }))
    result = run_pipeline(str(xml), audit_dir=str(out), run_llm=False, registry_path=cleaned_path)
    registry = json.loads(open(result["output_files"]["codelist_duplicates.json"]).read())

    assert [d["id"] for d in registry["insee-geo:regions"]["duplicates"]] == ["reg-copie"]
    assert registry["insee-geo:regions"]["level"] == "officielle"
    assert registry["reg-renvoi"]["suggested_official"] == {
        "id": "insee-geo:regions", "name": "Régions (COG)",
    }
