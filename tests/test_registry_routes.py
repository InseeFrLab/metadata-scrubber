"""Routes de retrait du registre nettoyé et de résumé de run (client de test FastAPI)."""

import json
import os

import pytest

os.environ.setdefault("SCRUBBER_AUTH_PASSWORD", "testpw")

from fastapi.testclient import TestClient  # noqa: E402

import metadata_scrubber.ui.server as server  # noqa: E402

AUTH = {"Authorization": "Basic dTp0ZXN0cHc="}  # u:testpw


@pytest.fixture
def client(tmp_path, monkeypatch):
    run_dir = tmp_path / "out" / "20261022-140509"
    run_dir.mkdir(parents=True)
    registry = {
        "a": {"id": "a", "name": "A", "codes": [["1", "x"]], "duplicates": []},
        "b": {"id": "b", "name": "B", "codes": [["1", "x"]], "duplicates": []},
        # Faux doublon : c2 rejeté, c3 en attente
        "c": {"id": "c", "name": "C", "codes": [["1", "x"]], "duplicates": [
            {"id": "c2", "name": "C2", "codes": [["1", "y"]], "series": ["s1161"], "decision": "reject"},
            {"id": "c3", "name": "C3", "codes": [["1", "x"]], "decision": "pending"},
        ]},
    }
    path = str(run_dir / "codelist_duplicates.json")
    monkeypatch.setattr(server, "_registries", {path: registry})
    monkeypatch.setattr(server, "_registry_path", path)
    return TestClient(server.app, headers=AUTH), tmp_path, run_dir


def test_cleaned_registry_lives_at_output_root(client):
    c, tmp_path, run_dir = client
    r = c.post("/api/registry/add-to-cleaned/batch", json={"cl_ids": ["a", "b"]})
    assert r.status_code == 200
    assert r.json()["path"] == str(tmp_path / "out" / "cleaned_codelists.json")
    assert not (run_dir / "cleaned_codelists.json").exists()


def test_remove_from_cleaned(client):
    c, tmp_path, _ = client
    c.post("/api/registry/add-to-cleaned/batch", json={"cl_ids": ["a", "b"]})

    r = c.post("/api/registry/remove-from-cleaned", json={"cl_ids": ["a", "zzz"]})

    assert r.status_code == 200
    body = r.json()
    assert body["removed"] == ["a"] and body["not_present"] == ["zzz"]
    saved = json.loads((tmp_path / "out" / "cleaned_codelists.json").read_text(encoding="utf-8"))
    assert list(saved["codelists"]) == ["b"]


def test_remove_without_cleaned_registry_is_404(client):
    c, _, _ = client
    assert c.post("/api/registry/remove-from-cleaned", json={"cl_ids": ["a"]}).status_code == 404


def test_run_summary_route(client):
    c, _, run_dir = client
    assert c.get("/api/registry/run-summary").status_code == 404

    summary = {"status": "success", "warnings": [{"phase": "semantic", "status": "skipped", "reason": "x"}]}
    (run_dir / "run_summary.json").write_text(json.dumps(summary), encoding="utf-8")
    r = c.get("/api/registry/run-summary")
    assert r.status_code == 200
    assert r.json()["warnings"][0]["phase"] == "semantic"


def test_false_duplicate_keeps_both_lists(client):
    c, tmp_path, _ = client
    assert c.post("/api/registry/add-to-cleaned", json={"cl_id": "c2"}).status_code == 200
    assert c.post("/api/registry/add-to-cleaned", json={"cl_id": "c"}).status_code == 200
    r = c.post("/api/registry/add-to-cleaned", json={"cl_id": "c3"})
    assert r.status_code == 409 and "rejeté" in r.json()["detail"]

    saved = json.loads((tmp_path / "out" / "cleaned_codelists.json").read_text(encoding="utf-8"))
    assert set(saved["codelists"]) == {"c", "c2"}
    assert saved["codelists"]["c2"]["codes"] == [["1", "y"]]
    assert saved["codelists"]["c2"]["level_suggested"] == "specifique"
