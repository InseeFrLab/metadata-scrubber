"""Décisions conservées entre les runs (decisions.json) et listes écartées."""

import json
import os

import pytest

os.environ.setdefault("SCRUBBER_AUTH_PASSWORD", "testpw")

from fastapi.testclient import TestClient  # noqa: E402

import metadata_scrubber.ui.server as server  # noqa: E402
from metadata_scrubber.cleaned_registry import add_entries_from_parents, empty_cleaned_doc  # noqa: E402
from metadata_scrubber.decisions import (  # noqa: E402
    apply_decisions,
    decisions_path,
    empty_decisions_doc,
    pair_key,
    update_decisions,
)

AUTH = {"Authorization": "Basic dTp0ZXN0cHc="}  # u:testpw


def _registry(decision_b="pending", decision_c="pending", dismissed_d=False):
    return {
        "a": {
            "id": "a",
            "duplicates": [
                {"id": "b", "decision": decision_b},
                {"id": "c", "decision": decision_c},
            ],
        },
        "d": {"id": "d", "duplicates": [], **({"dismissed": True} if dismissed_d else {})},
    }


class TestDecisionsDoc:
    def test_pair_key_is_symmetric(self):
        assert pair_key("a", "b") == pair_key("b", "a") == "a|b"

    def test_path_is_at_output_root(self):
        assert decisions_path("s3://b/out/20261022-140509/codelist_duplicates.json") == (
            "s3://b/out/decisions.json"
        )
        assert decisions_path("out/codelist_duplicates.json") == "out/decisions.json"

    def test_update_keeps_approve_and_reject_only(self):
        doc = empty_decisions_doc()
        update_decisions(doc, _registry("approve", "reject", dismissed_d=True))
        assert doc["pairs"]["a|b"]["decision"] == "approve"
        assert doc["pairs"]["a|c"]["decision"] == "reject"
        assert list(doc["dismissed"]) == ["d"]

    def test_pending_and_undismiss_remove_entries(self):
        doc = empty_decisions_doc()
        update_decisions(doc, _registry("approve", "reject", dismissed_d=True))
        update_decisions(doc, _registry("pending", "reject"))
        assert list(doc["pairs"]) == ["a|c"]
        assert doc["dismissed"] == {}

    def test_pairs_from_other_sources_are_kept(self):
        doc = empty_decisions_doc()
        doc["pairs"]["x|y"] = {"decision": "reject", "updated_at": "t"}
        update_decisions(doc, _registry("approve"))
        assert doc["pairs"]["x|y"]["decision"] == "reject"

    def test_unchanged_decision_keeps_its_date(self):
        doc = empty_decisions_doc()
        doc["pairs"]["a|b"] = {"decision": "approve", "updated_at": "t0"}
        update_decisions(doc, _registry("approve"))
        assert doc["pairs"]["a|b"]["updated_at"] == "t0"


class TestApplyDecisions:
    def test_decisions_and_dismissed_are_carried(self):
        doc = empty_decisions_doc()
        # Paire enregistrée dans l'autre sens (c maître lors d'un run précédent)
        doc["pairs"][pair_key("c", "a")] = {"decision": "reject"}
        doc["dismissed"]["d"] = {}

        registry = _registry()
        assert apply_decisions(registry, doc) == 2

        b, c = registry["a"]["duplicates"]
        assert b["decision"] == "pending" and "decision_carried" not in b
        assert c["decision"] == "reject" and c["decision_carried"] is True
        assert registry["d"]["dismissed"] is True

    def test_batch_add_all_skips_dismissed(self):
        cleaned = empty_cleaned_doc()
        registry = {
            "d": {"id": "d", "codes": [], "duplicates": [], "dismissed": True},
            "e": {"id": "e", "codes": [], "duplicates": []},
        }
        result = add_entries_from_parents(cleaned, registry)
        assert result["added"] == ["e"]


XML_TWO_IDENTICAL = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataDocument>
  <Fragment><CodeList><ID>cl-001</ID><CodeListName><String>A</String></CodeListName>
    <Code><Value>1</Value></Code><Code><Value>2</Value></Code></CodeList></Fragment>
  <Fragment><CodeList><ID>cl-002</ID><CodeListName><String>B</String></CodeListName>
    <Code><Value>1</Value></Code><Code><Value>2</Value></Code></CodeList></Fragment>
  <Fragment><CodeList><ID>cl-003</ID><CodeListName><String>C</String></CodeListName>
    <Code><Value>9</Value></Code></CodeList></Fragment>
</MetaDataDocument>
"""


def test_pipeline_reapplies_saved_decisions(tmp_path):
    from metadata_scrubber import main

    xml = tmp_path / "in.xml"
    xml.write_text(XML_TWO_IDENTICAL, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "decisions.json").write_text(json.dumps({
        "version": 1,
        "pairs": {"cl-001|cl-002": {"decision": "reject"}},
        "dismissed": {"cl-003": {}},
    }), encoding="utf-8")

    result = main.run_pipeline(xml_source=str(xml), audit_dir=str(out), run_llm=False)

    run_dir = out / result["run_id"]
    registry = json.loads((run_dir / "codelist_duplicates.json").read_text(encoding="utf-8"))
    dup = registry["cl-001"]["duplicates"][0]
    assert dup["id"] == "cl-002"
    assert dup["decision"] == "reject" and dup["decision_carried"] is True
    assert registry["cl-003"]["dismissed"] is True

    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    assert summary["counts"]["decisions_carried"] == 2


@pytest.fixture
def client(tmp_path, monkeypatch):
    run_dir = tmp_path / "out" / "20261022-140509"
    run_dir.mkdir(parents=True)
    registry = {
        "a": {"id": "a", "name": "A", "codes": [["1", "x"]],
              "duplicates": [{"id": "b", "decision": "reject"}]},
        "c": {"id": "c", "name": "C", "codes": [["2", "y"]], "duplicates": []},
    }
    path = str(run_dir / "codelist_duplicates.json")
    monkeypatch.setattr(server, "_registries", {path: registry})
    monkeypatch.setattr(server, "_registry_path", path)
    return TestClient(server.app, headers=AUTH), tmp_path, registry


def test_dismiss_route_and_save_writes_decisions(client):
    c, tmp_path, registry = client

    r = c.post("/api/registry/dismiss", json={"cl_ids": ["c", "a", "zzz"]})
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == ["c"]
    assert body["skipped_with_duplicates"] == ["a"]
    assert body["not_found"] == ["zzz"]
    assert registry["c"]["dismissed"] is True

    r = c.post("/api/registry/save")
    assert r.status_code == 200
    assert r.json()["decisions_path"] == str(tmp_path / "out" / "decisions.json")
    saved = json.loads((tmp_path / "out" / "decisions.json").read_text(encoding="utf-8"))
    assert saved["pairs"]["a|b"]["decision"] == "reject"
    assert list(saved["dismissed"]) == ["c"]

    r = c.post("/api/registry/dismiss", json={"cl_ids": ["c"], "dismissed": False})
    assert r.json()["updated"] == ["c"]
    assert "dismissed" not in registry["c"]
