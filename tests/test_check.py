"""Service de contrôle scrubber-check : index, moteur, rapport, CLI et API."""

import json

import pytest
from fastapi.testclient import TestClient

from api_fixtures import ddi_file, make_client
from metadata_scrubber.check import api as check_api
from metadata_scrubber.check.cli import main as cli_main
from metadata_scrubber.check.index import ReferenceIndex, build_index, read_index, write_index
from metadata_scrubber.check.report import check_bytes, has_findings, to_csv, to_table

CONTRAT = [("CDI", "Contrat à durée indéterminée"), ("CDD", "Contrat à durée déterminée")]

# Fichier « nouvelle opération » : une liste par cas de figure
NEW_FILE = ddi_file({
    "sexe-23": ("N_sexe_23", [("1", "Homme"), ("2", "Femme")], None),                   # réutilisée
    "sexe-new": ("N_sexe", [("1", "Homme"), ("2", "Femme")], None),                    # doublon exact
    "region-new": ("N_region", [("11", "Île-de-France"), ("84", "Auvergne-Rhône-Alpes")], None),  # officielle
    "contrat-sub": ("N_contrat_court", [CONTRAT[0], ("APP", "Apprentissage")], None),  # inclusion
    "sexe-plus": ("N_sexe_plus", [("1", "Homme"), ("2", "Femme"), ("9", "Autre")], None),  # extension
    "derivee": ("N_contrat_25", CONTRAT + [("INT", "Intérim")], "contrat-24"),         # lignée
    "neuve-a": ("N_neuve_a", [("A", "Alpha"), ("B", "Bêta")], None),                   # nouvelle…
    "neuve-b": ("N_neuve_b", [("A", "Alpha"), ("B", "Bêta")], None),                   # …en double
})


@pytest.fixture(scope="module")
def index_doc():
    client, _ = make_client()
    return build_index(client, log=lambda _: None)


@pytest.fixture
def index(index_doc):
    return ReferenceIndex.from_doc(index_doc)


def _by_id(report):
    return {cl["id"]: cl for cl in report["codelists"]}


def test_build_index(index_doc, tmp_path):
    assert index_doc["scope"] == "toute la base"
    assert index_doc["skipped_operations"] == {"s2022": "pas de description DDI"}
    levels = {e["id"]: e["level"] for e in index_doc["codelists"]}
    assert levels["pays"] == "specifique"          # une seule série dans le faux serveur
    assert levels["insee-geo:regions"] == "officielle"
    pays = next(e for e in index_doc["codelists"] if e["id"] == "pays")
    assert pays["operations"] == ["s2023", "s2024"] and pays["vars_count"] == 2

    path = str(tmp_path / "idx.json.gz")
    write_index(index_doc, path)
    assert read_index(path)["counts"] == index_doc["counts"]


def test_check_statuses(index):
    report = check_bytes(NEW_FILE, "nouvelle.xml", index)
    cls = _by_id(report)

    assert cls["sexe-23"]["status"] == "reutilisee"
    assert cls["sexe-new"]["status"] == "doublon"
    assert {m["id"] for m in cls["sexe-new"]["matches"]} >= {"sexe-23", "sexe-24"}

    region = cls["region-new"]
    assert region["status"] == "doublon"
    assert region["matches"][0]["id"] == "insee-geo:regions"
    assert "nomenclature officielle" in region["action"]

    assert cls["contrat-sub"]["status"] == "inclusion"
    assert cls["contrat-sub"]["matches"][0]["detection"] == "inclusion"
    assert cls["contrat-sub"]["matches"][0]["diff"]["codes_en_moins"] == [["CDD", "Contrat à durée déterminée"]]

    plus = cls["sexe-plus"]
    assert plus["matches"][0]["detection"] == "extension"
    assert plus["matches"][0]["diff"]["codes_en_plus"] == [["9", "Autre"]]
    assert "Étend" in plus["action"]

    derivee = {m["id"]: m["detection"] for m in cls["derivee"]["matches"]}
    assert derivee["contrat-24"] == "lineage"

    assert cls["neuve-a"]["status"] == "nouvelle" and cls["neuve-a"]["matches"] == []
    internal = report["internal_duplicates"]
    assert [(d["a"]["id"], d["b"]["id"], d["detection"]) for d in internal
            if d["a"]["id"].startswith("neuve")] == [("neuve-a", "neuve-b", "exact")]

    assert report["summary"]["by_status"]["reutilisee"] == 1
    assert has_findings(report, "doublon") and not has_findings(report, "never")


def test_restrict_and_exclude(index):
    # Hors de la série et des opérations demandées, seules les officielles restent
    restricted = index.restrict(series=["s0000"])
    assert {cl.id for cl in restricted.codelists} == {cl.id for cl in index.codelists
                                                       if cl.id.startswith("insee-geo:")}
    # Re-contrôle de s2024 : ses listes propres sortent, la liste partagée reste
    excluded = index.restrict(exclude_operations=["s2024"])
    ids = {cl.id for cl in excluded.codelists}
    assert "sexe-24" not in ids and "pays" in ids and "sexe-23" in ids


def test_report_formats(index):
    report = check_bytes(NEW_FILE, "nouvelle.xml", index)
    csv_text = to_csv(report)
    assert csv_text.splitlines()[0].startswith("liste_id;liste_nom")
    assert "insee-geo:regions" in csv_text
    table = to_table(report)
    assert "Doublons internes au fichier" in table and "N_region" in table


def test_cli(index_doc, tmp_path, capsys):
    idx = str(tmp_path / "idx.json.gz")
    write_index(index_doc, idx)
    xml = tmp_path / "nouvelle.xml"
    xml.write_bytes(NEW_FILE)

    with pytest.raises(SystemExit) as exc:
        cli_main([str(xml), "--index", idx, "--format", "json"])
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["summary"]["codelists"] == 8

    with pytest.raises(SystemExit) as exc:
        cli_main([str(xml), "--index", idx, "--fail-on", "never", "--output", str(tmp_path / "r.csv"),
                  "--format", "csv"])
    assert exc.value.code == 0
    assert (tmp_path / "r.csv").read_text(encoding="utf-8").startswith("liste_id")

    with pytest.raises(SystemExit) as exc:
        cli_main([str(tmp_path / "absent.xml"), "--index", idx])
    assert exc.value.code == 2

    with pytest.raises(SystemExit) as exc:
        cli_main(["index", "info", "--index", idx])
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)["counts"]["operations"] == 2


@pytest.fixture
def api_client(index_doc, tmp_path, monkeypatch):
    idx = str(tmp_path / "idx.json.gz")
    write_index(index_doc, idx)
    monkeypatch.setenv("SCRUBBER_CHECK_INDEX", idx)
    monkeypatch.setattr(check_api, "_holder", check_api._IndexHolder())
    return TestClient(check_api.app)


def test_api_check(api_client):
    r = api_client.post("/v1/check", files={"file": ("nouvelle.xml", NEW_FILE, "application/xml")})
    assert r.status_code == 200
    assert _by_id(r.json())["region-new"]["status"] == "doublon"

    # Corps XML brut, restriction et format CSV
    r = api_client.post("/v1/check?serie=s0000&format=csv", content=NEW_FILE,
                        headers={"Content-Type": "application/xml"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "sexe-23" in r.text

    assert api_client.get("/v1/index").json()["counts"]["operations"] == 2
    assert api_client.get("/health").json()["index_loaded"] is True


def test_api_rejects_bad_input(api_client, monkeypatch, tmp_path):
    assert api_client.post("/v1/check", content=b"<pas du ddi",
                           headers={"Content-Type": "application/xml"}).status_code == 422
    assert api_client.post("/v1/check", content=b"  ",
                           headers={"Content-Type": "application/xml"}).status_code == 422

    monkeypatch.setenv("SCRUBBER_CHECK_MAX_MB", "0.001")   # ~1 Ko
    r = api_client.post("/v1/check", files={"file": ("gros.xml", NEW_FILE, "application/xml")})
    assert r.status_code == 413

    # Entité externe : jamais résolue
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    monkeypatch.setenv("SCRUBBER_CHECK_MAX_MB", "50")
    xxe = (f'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY xxe SYSTEM "file://{secret}">]>'
           '<FragmentInstance><Fragment><CodeList><ID>a</ID><CodeListName><String>&xxe;</String>'
           '</CodeListName></CodeList></Fragment></FragmentInstance>').encode()
    r = api_client.post("/v1/check", content=xxe, headers={"Content-Type": "application/xml"})
    assert "SECRET" not in r.text


def test_api_auth(api_client, monkeypatch):
    monkeypatch.setenv("SCRUBBER_CHECK_AUTH_PASSWORD", "pw")
    assert api_client.get("/v1/index").status_code == 401
    assert api_client.get("/v1/index", auth=("x", "pw")).status_code == 200
    assert api_client.get("/health").status_code == 200


def test_api_without_index(monkeypatch, tmp_path):
    monkeypatch.setenv("SCRUBBER_CHECK_INDEX", str(tmp_path / "absent.json.gz"))
    monkeypatch.setattr(check_api, "_holder", check_api._IndexHolder())
    assert TestClient(check_api.app).get("/v1/index").status_code == 503


def test_web_precheck_route(index_doc, tmp_path, monkeypatch):
    # Mot de passe lu à l'import du module d'authentification de l'appli
    monkeypatch.setenv("SCRUBBER_AUTH_PASSWORD", "testpw")
    import metadata_scrubber.ui.server as server

    idx = str(tmp_path / "idx.json.gz")
    write_index(index_doc, idx)
    monkeypatch.setattr(server, "_precheck_holder", None)
    client = TestClient(server.app, headers={"Authorization": "Basic dTp0ZXN0cHc="})
    r = client.post(f"/api/precheck?index={idx}", files={"file": ("n.xml", NEW_FILE, "application/xml")})
    assert r.status_code == 200
    assert _by_id(r.json())["region-new"]["status"] == "doublon"
    r = client.post(f"/api/precheck?index={tmp_path}/absent.gz", files={"file": ("n.xml", NEW_FILE)})
    assert r.status_code == 503
