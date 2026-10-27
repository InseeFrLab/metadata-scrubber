"""Sauvegarde automatique des décisions, registres par chemin, décisions en lot, liste des runs."""

import json
import os

import pytest

os.environ.setdefault("SCRUBBER_AUTH_PASSWORD", "testpw")

from fastapi.testclient import TestClient  # noqa: E402

import metadata_scrubber.ui.server as server  # noqa: E402
from metadata_scrubber.decisions import reapply_decisions  # noqa: E402

AUTH = {"Authorization": "Basic dTp0ZXN0cHc="}  # u:testpw


def _registry(dup_decision="pending", **dup_extra):
    return {
        "a": {
            "id": "a", "name": "A", "codes": [["1", "x"]],
            "duplicates": [{"id": "b", "decision": dup_decision, **dup_extra},
                           {"id": "c", "decision": "pending"}],
        },
        "d": {"id": "d", "name": "D", "codes": [["2", "y"]], "duplicates": []},
    }


def _write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "_registries", {})
    monkeypatch.setattr(server, "_registry_path", None)
    run_dir = tmp_path / "out" / "20261022-140509"
    reg_file = run_dir / "codelist_duplicates.json"
    _write(reg_file, _registry())
    return TestClient(server.app, headers=AUTH), tmp_path, str(reg_file)


def _decisions(tmp_path):
    return json.loads((tmp_path / "out" / "decisions.json").read_text(encoding="utf-8"))


def test_decision_is_saved_and_reloaded(env):
    c, tmp_path, path = env
    assert c.get("/api/registry", params={"path": path}).status_code == 200

    r = c.patch("/api/registry/decision", params={"path": path},
                json={"cl_id": "a", "dup_id": "b", "decision": "approve"})
    assert r.status_code == 200
    assert r.json()["saved"] is True
    assert _decisions(tmp_path)["pairs"]["a|b"]["decision"] == "approve"

    # Le registre sur disque n'a pas changé : le rechargement relit decisions.json
    server._registries.clear()
    reloaded = c.get("/api/registry", params={"path": path}).json()
    assert reloaded["a"]["duplicates"][0]["decision"] == "approve"
    assert "decision_carried" not in reloaded["a"]["duplicates"][0]


def test_dismiss_is_saved(env):
    c, tmp_path, path = env
    c.get("/api/registry", params={"path": path})
    r = c.post("/api/registry/dismiss", params={"path": path}, json={"cl_ids": ["d"]})
    assert r.json()["saved"] is True
    assert list(_decisions(tmp_path)["dismissed"]) == ["d"]


def test_registries_are_kept_per_path(env, tmp_path):
    c, _, path = env
    other = tmp_path / "out" / "20261023-091200" / "codelist_duplicates.json"
    _write(other, {"z": {"id": "z", "name": "Z", "duplicates": [{"id": "y"}]}})

    c.get("/api/registry", params={"path": path})
    c.get("/api/registry", params={"path": str(other)})

    r = c.patch("/api/registry/decision", params={"path": path},
                json={"cl_id": "a", "dup_id": "b", "decision": "reject"})
    assert r.status_code == 200
    assert server._registries[path]["a"]["duplicates"][0]["decision"] == "reject"
    assert "a" not in server._registries[str(other)]


def test_batch_decisions_and_undo(env):
    c, tmp_path, path = env
    c.get("/api/registry", params={"path": path})

    r = c.post("/api/registry/decisions", params={"path": path}, json={"items": [
        {"cl_id": "a", "dup_id": "b", "decision": "approve"},
        {"cl_id": "a", "dup_id": "c", "decision": "approve"},
        {"cl_id": "a", "dup_id": "zzz", "decision": "approve"},
    ]})
    body = r.json()
    assert body["count"] == 2 and body["saved"] is True
    assert body["not_found"] == [{"cl_id": "a", "dup_id": "zzz"}]
    assert {p["dup_id"]: p["decision"] for p in body["previous"]} == {"b": "pending", "c": "pending"}

    r = c.post("/api/registry/decisions", params={"path": path}, json={"items": body["previous"]})
    assert r.json()["count"] == 2
    dups = server._registries[path]["a"]["duplicates"]
    assert [d["decision"] for d in dups] == ["pending", "pending"]
    assert _decisions(tmp_path)["pairs"] == {}


def test_save_failure_is_reported(env, monkeypatch):
    c, _, path = env
    c.get("/api/registry", params={"path": path})

    real_write = server.write_registry
    s3_down = True

    def flaky_write(doc, p):
        if s3_down:
            raise OSError("S3 indisponible")
        real_write(doc, p)

    monkeypatch.setattr(server, "write_registry", flaky_write)
    r = c.patch("/api/registry/decision", params={"path": path},
                json={"cl_id": "a", "dup_id": "b", "decision": "approve"})
    assert r.status_code == 200
    assert r.json()["saved"] is False and "S3 indisponible" in r.json()["save_error"]
    # La décision reste en mémoire ; un nouvel essai l'enregistre
    s3_down = False
    assert c.post("/api/registry/persist", params={"path": path}).json()["saved"] is True


def test_reapply_resets_carried_decision_set_back_to_pending():
    registry = _registry("approve", decision_carried=True)
    # La décision reprise a été remise en attente : elle n'est plus dans decisions.json
    assert reapply_decisions(registry, {"pairs": {}, "dismissed": {}}) == 1
    dup = registry["a"]["duplicates"][0]
    assert dup["decision"] == "pending" and "decision_carried" not in dup


def test_list_runs(env, tmp_path):
    c, _, _ = env
    root = tmp_path / "out"
    _write(root / "20261023-091200" / "run_summary.json",
           {"status": "success", "xml_source": "s3://b/BTS.xml", "counts": {"candidates": 3}})
    (root / "pas-un-run").mkdir()

    r = c.get("/api/runs", params={"root": str(root)})
    runs = r.json()["runs"]
    assert [run["run_id"] for run in runs] == ["20261023-091200", "20261022-140509"]
    assert runs[0]["status"] == "success" and runs[0]["counts"] == {"candidates": 3}
    assert runs[0]["registry_path"].endswith("20261023-091200/codelist_duplicates.json")
    assert runs[1]["status"] == "unknown"

    assert c.get("/api/runs", params={"root": str(tmp_path / "absent")}).json()["runs"] == []
