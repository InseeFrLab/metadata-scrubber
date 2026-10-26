"""Changement de liste maître : liens du groupe, swap, persistance et sync du registre nettoyé."""

import copy
import json
import os

import pytest

os.environ.setdefault("SCRUBBER_AUTH_PASSWORD", "testpw")

from fastapi.testclient import TestClient  # noqa: E402

import metadata_scrubber.ui.server as server  # noqa: E402
from metadata_scrubber.cleaned_registry import empty_cleaned_doc, sync_cleaned_registry  # noqa: E402
from metadata_scrubber.decisions import empty_decisions_doc, update_decisions  # noqa: E402
from metadata_scrubber.normalize import signature_from_codes  # noqa: E402
from metadata_scrubber.reporting.duplicates_registry import (  # noqa: E402
    build_duplicates_registry,
    detection_against_parent,
)
from metadata_scrubber.types import CandidateFusion, CodeList  # noqa: E402
from metadata_scrubber.ui.services.registry_service import (  # noqa: E402
    SwapParentError,
    swap_parent,
)

AUTH = {"Authorization": "Basic dTp0ZXN0cHc="}  # u:testpw


def _cl(cl_id: str, origin: str = "xml") -> CodeList:
    cl = CodeList(id=cl_id, name=cl_id.upper(), label="", codes=[("1", cl_id)], origin=origin)
    cl.sig = signature_from_codes(cl.codes)
    return cl


def _candidate(kind, master, slave, conf):
    return CandidateFusion(
        fusion_id=f"{kind}-{master.id}-{slave.id}",
        detection_type=kind,
        master_cl=master,
        slave_cls=[slave],
        confidence=conf,
    )


def _chain_registry(**kwargs):
    """a ~ b (exact), b ~ c (flou) ; pas de paire a ~ c. Maître par défaut : a."""
    a, b, c = _cl("a"), _cl("b"), _cl("c")
    cands = [_candidate("exact", a, b, 1.0), _candidate("fuzzy", b, c, 0.93)]
    return build_duplicates_registry(cands, [a, b, c], **kwargs)


class TestLinks:
    def test_every_member_lists_its_pairs(self):
        reg = _chain_registry()
        assert [lk["id"] for lk in reg["a"]["links"]] == ["b"]
        b, c = reg["a"]["duplicates"]
        assert [lk["id"] for lk in b["links"]] == ["a", "c"]
        assert c["links"] == [{"id": "b", "detection_types": ["fuzzy"], "confidence": 0.93}]

    def test_detection_against_parent(self):
        links = [
            {"id": "b", "detection_types": ["exact"], "confidence": 1.0},
            {"id": "c", "detection_types": ["fuzzy"], "confidence": 0.93},
        ]
        assert detection_against_parent(links, "c") == (["fuzzy"], 0.93, [])
        assert detection_against_parent(links, "z") == (["exact", "fuzzy"], 1.0, ["b", "c"])

    def test_preferred_parent_is_chosen(self):
        reg = _chain_registry(preferred_parents={"c"})
        assert list(reg) == ["c"]
        assert reg["c"]["master_chosen"] is True
        assert {d["id"] for d in reg["c"]["duplicates"]} == {"a", "b"}

    def test_registry_entry_beats_preferred_parent(self):
        r, x = _cl("r", origin="registry"), _cl("x")
        reg = build_duplicates_registry(
            [_candidate("exact", r, x, 1.0)], [r, x], preferred_parents={"x"}
        )
        assert list(reg) == ["r"]


class TestSwapParent:
    def test_swap_recomputes_badges_and_keeps_decisions(self):
        reg = _chain_registry()
        reg["a"]["duplicates"][0]["decision"] = "reject"  # b rejeté vis-à-vis de a

        entry = swap_parent(reg, "a", "c")

        assert list(reg) == ["c"]
        assert entry["master_chosen"] is True and entry["previous_parent"] == "a"
        dups = {d["id"]: d for d in entry["duplicates"]}
        # a n'a pas de paire avec c : lien indirect via b
        assert dups["a"]["detection_types"] == ["exact"] and dups["a"]["via"] == ["b"]
        assert dups["a"]["decision"] == "pending"
        # b ~ c : paire directe floue, décision conservée
        assert dups["b"]["detection_types"] == ["fuzzy"] and "via" not in dups["b"]
        assert dups["b"]["confidence"] == 0.93
        assert dups["b"]["decision"] == "reject"

    def test_swap_keeps_position(self):
        reg = {"first": {"id": "first", "duplicates": []}, **_chain_registry(),
               "last": {"id": "last", "duplicates": []}}
        swap_parent(reg, "a", "b")
        assert list(reg) == ["first", "b", "last"]

    def test_swap_back_restores_the_group(self):
        reg = _chain_registry()
        original = copy.deepcopy(reg["a"]["duplicates"])
        swap_parent(reg, "a", "c")
        entry = swap_parent(reg, "c", "a")
        assert {d["id"]: d["detection_types"] for d in entry["duplicates"]} == {
            d["id"]: d["detection_types"] for d in original
        }

    def test_registry_master_cannot_be_swapped(self):
        r, x = _cl("r", origin="registry"), _cl("x")
        reg = build_duplicates_registry([_candidate("exact", r, x, 1.0)], [r, x])
        with pytest.raises(SwapParentError):
            swap_parent(reg, "r", "x")

    def test_old_registry_without_links_is_refused(self):
        reg = {"a": {"id": "a", "duplicates": [{"id": "b", "decision": "pending"}]}}
        with pytest.raises(SwapParentError):
            swap_parent(reg, "a", "b")

    def test_unknown_ids(self):
        reg = _chain_registry()
        with pytest.raises(KeyError):
            swap_parent(reg, "zzz", "b")
        with pytest.raises(KeyError):
            swap_parent(reg, "a", "zzz")


class TestPersistence:
    def test_chosen_master_is_saved_in_decisions(self):
        reg = _chain_registry()
        swap_parent(reg, "a", "c")
        doc = empty_decisions_doc()
        doc["masters"]["a"] = {}  # ancienne maître choisie lors d'un run précédent
        update_decisions(doc, reg)
        assert list(doc["masters"]) == ["c"]

    def test_cleaned_entry_of_old_parent_drops_the_group(self):
        reg = _chain_registry()
        reg["a"]["duplicates"][0]["decision"] = "approve"
        cleaned, _ = sync_cleaned_registry(reg, empty_cleaned_doc(), "x")
        cleaned["codelists"]["a"]["replaces"].append({"id": "autre"})  # autre run

        swap_parent(reg, "a", "c")
        for d in reg["c"]["duplicates"]:
            d["decision"] = "approve" if d["id"] == "b" else d["decision"]
        cleaned, _ = sync_cleaned_registry(reg, cleaned, "x")

        assert [r["id"] for r in cleaned["codelists"]["a"]["replaces"]] == ["autre"]
        assert [r["id"] for r in cleaned["codelists"]["c"]["replaces"]] == ["b"]

    def test_old_parent_entry_is_deleted_when_emptied(self):
        reg = _chain_registry()
        reg["a"]["duplicates"][0]["decision"] = "approve"
        cleaned, _ = sync_cleaned_registry(reg, empty_cleaned_doc(), "x")
        swap_parent(reg, "a", "c")
        cleaned, stats = sync_cleaned_registry(reg, cleaned, "x")
        assert "a" not in cleaned["codelists"]
        assert stats["entries_deleted"] == 1


XML_TWO_IDENTICAL = """<?xml version="1.0" encoding="UTF-8"?>
<MetaDataDocument>
  <Fragment><CodeList><ID>cl-001</ID><CodeListName><String>A</String></CodeListName>
    <Code><Value>1</Value></Code></CodeList></Fragment>
  <Fragment><CodeList><ID>cl-002</ID><CodeListName><String>B</String></CodeListName>
    <Code><Value>1</Value></Code></CodeList></Fragment>
</MetaDataDocument>
"""


def test_pipeline_keeps_chosen_master(tmp_path):
    from metadata_scrubber import main

    xml = tmp_path / "in.xml"
    xml.write_text(XML_TWO_IDENTICAL, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    (out / "decisions.json").write_text(
        json.dumps({"version": 1, "pairs": {}, "dismissed": {}, "masters": {"cl-002": {}}}),
        encoding="utf-8",
    )

    result = main.run_pipeline(xml_source=str(xml), audit_dir=str(out), run_llm=False)

    registry = json.loads(
        (out / result["run_id"] / "codelist_duplicates.json").read_text(encoding="utf-8")
    )
    assert list(registry) == ["cl-002"]
    assert registry["cl-002"]["master_chosen"] is True
    assert registry["cl-002"]["duplicates"][0]["id"] == "cl-001"


@pytest.fixture
def client(tmp_path, monkeypatch):
    registry = _chain_registry()
    monkeypatch.setattr(server, "_registry_state", registry)
    monkeypatch.setattr(server, "_registry_path", str(tmp_path / "codelist_duplicates.json"))
    return TestClient(server.app, headers=AUTH), registry


def test_swap_route(client):
    c, registry = client
    r = c.post("/api/registry/swap-parent", json={"parent_id": "a", "new_parent_id": "c"})
    assert r.status_code == 200
    assert r.json()["entry"]["id"] == "c"
    assert list(registry) == ["c"]

    assert c.post(
        "/api/registry/swap-parent", json={"parent_id": "a", "new_parent_id": "c"}
    ).status_code == 404


def test_swap_route_refuses_registry_master(tmp_path, monkeypatch):
    r, x = _cl("r", origin="registry"), _cl("x")
    registry = build_duplicates_registry([_candidate("exact", r, x, 1.0)], [r, x])
    monkeypatch.setattr(server, "_registry_state", registry)
    c = TestClient(server.app, headers=AUTH)
    resp = c.post("/api/registry/swap-parent", json={"parent_id": "r", "new_parent_id": "x"})
    assert resp.status_code == 409
