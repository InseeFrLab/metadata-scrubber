"""Tests du registre des CodeLists nettoyées (scrubber.cleaned_registry)."""

import json

from scrubber.cleaned_registry import (
    add_entry_from_parent,
    cleaned_registry_path,
    empty_cleaned_doc,
    load_cleaned_codelists,
    migrate_cleaned_doc,
    sync_cleaned_registry,
    validate_cleaned_doc,
)
from scrubber.funnel import detect_exact_duplicates
from scrubber.normalize import signature_from_codes
from scrubber.reporting.duplicates_registry import build_duplicates_registry
from scrubber.types import CandidateFusion, CodeList


def _registry(decision="approve", dup_id="dup-1"):
    """Registre des doublons minimal : un parent, un doublon."""
    return {
        "parent-1": {
            "id": "parent-1",
            "name": "SEXE",
            "label": "Sexe",
            "codes": [["1", "Homme"], ["2", "Femme"]],
            "codes_count": 2,
            "vars": ["SEXE"],
            "duplicates": [
                {
                    "id": dup_id,
                    "name": "SEXE_2",
                    "detection_types": ["exact"],
                    "confidence": 1.0,
                    "decision": decision,
                }
            ],
        }
    }


# ────────────────────────────────────────────────────────────────────────
# sync_cleaned_registry
# ────────────────────────────────────────────────────────────────────────


def test_sync_cree_entree_avec_replaces():
    cleaned, stats = sync_cleaned_registry(_registry("approve"), None, "reg.json")
    assert stats["entries_created"] == 1
    entry = cleaned["codelists"]["parent-1"]
    assert entry["name"] == "SEXE"
    assert entry["codes"] == [["1", "Homme"], ["2", "Femme"]]
    assert [r["id"] for r in entry["replaces"]] == ["dup-1"]
    assert entry["replaces"][0]["detection_types"] == ["exact"]
    assert entry["first_added_at"] == entry["updated_at"]
    assert cleaned["version"] == 2


def test_sync_pas_dentree_sans_approbation():
    cleaned, stats = sync_cleaned_registry(_registry("pending"), None, "reg.json")
    assert cleaned["codelists"] == {}
    assert stats["entries_created"] == 0


def test_sync_append_sans_ecraser_les_editions():
    cleaned, _ = sync_cleaned_registry(_registry("approve", "dup-1"), None, "reg.json")
    # Éditions manuelles : rename + code ajouté
    entry = cleaned["codelists"]["parent-1"]
    entry["name"] = "SEXE_RENOMME"
    entry["codes"].append(["9", "Non renseigné"])

    # Second doublon approuvé sur le même parent
    reg = _registry("approve", "dup-1")
    reg["parent-1"]["duplicates"].append(
        {
            "id": "dup-2",
            "name": "SEXE_3",
            "detection_types": ["fuzzy"],
            "confidence": 0.97,
            "decision": "approve",
        }
    )
    cleaned, stats = sync_cleaned_registry(reg, cleaned, "reg.json")
    entry = cleaned["codelists"]["parent-1"]
    assert stats["replaces_added"] == 1
    assert {r["id"] for r in entry["replaces"]} == {"dup-1", "dup-2"}
    # Les éditions manuelles sont préservées
    assert entry["name"] == "SEXE_RENOMME"
    assert ["9", "Non renseigné"] in entry["codes"]


def test_sync_retrait_a_la_deapprobation_et_suppression_si_vide():
    cleaned, _ = sync_cleaned_registry(_registry("approve"), None, "reg.json")
    assert "parent-1" in cleaned["codelists"]

    cleaned, stats = sync_cleaned_registry(_registry("pending"), cleaned, "reg.json")
    assert stats["replaces_removed"] == 1
    assert stats["entries_deleted"] == 1
    assert "parent-1" not in cleaned["codelists"]


def test_sync_preserve_les_replaces_etrangers_au_registre_courant():
    cleaned, _ = sync_cleaned_registry(_registry("approve", "dup-1"), None, "reg.json")
    # Id issu d'un autre run (absent des duplicates du registre courant)
    cleaned["codelists"]["parent-1"]["replaces"].append(
        {"id": "ancien-dup", "name": "VIEUX", "detection_types": [], "confidence": 0}
    )
    # Dé-approbation de dup-1 : ancien-dup doit survivre → entrée conservée
    cleaned, stats = sync_cleaned_registry(_registry("pending", "dup-1"), cleaned, "reg.json")
    entry = cleaned["codelists"]["parent-1"]
    assert [r["id"] for r in entry["replaces"]] == ["ancien-dup"]
    assert stats["entries_deleted"] == 0


def test_sync_preserve_entree_manuelle_sans_replaces():
    """Une entrée ajoutée à la main (replaces vide) survit aux syncs."""
    cleaned = empty_cleaned_doc()
    parent = {
        "id": "solo-1", "name": "SOLO", "label": "", "codes": [["1", "Un"]],
        "codes_count": 1, "vars": [], "duplicates": [],
    }
    assert add_entry_from_parent(cleaned, parent) is True
    # Second ajout → refus
    assert add_entry_from_parent(cleaned, parent) is False

    # Sync avec le parent présent (sans doublon) : l'entrée survit
    registry = {"solo-1": parent}
    cleaned, stats = sync_cleaned_registry(registry, cleaned, "reg.json")
    assert "solo-1" in cleaned["codelists"]
    assert stats["entries_deleted"] == 0

    # Sync avec un registre où le parent n'apparaît pas : survit aussi
    cleaned, stats = sync_cleaned_registry(_registry("pending"), cleaned, "reg.json")
    assert "solo-1" in cleaned["codelists"]
    assert cleaned["codelists"]["solo-1"]["replaces"] == []


def test_add_entry_from_parent_contenu():
    cleaned = empty_cleaned_doc()
    parent = {
        "id": "a", "name": "N", "label": "L",
        "codes": [["1", "Un"], ["2", "Deux"]], "codes_count": 2, "vars": ["V"],
    }
    assert add_entry_from_parent(cleaned, parent)
    e = cleaned["codelists"]["a"]
    assert e["name"] == "N" and e["codes_count"] == 2 and e["replaces"] == []
    assert e["first_added_at"] == e["updated_at"]


def test_build_registry_inclut_les_listes_sans_doublon():
    solo = _cl("solo-1", "SOLO")
    reg_cl = _cl("reg-1", "A", origin="registry")
    xml_cl = _cl("xml-1", "B")
    candidates = [
        CandidateFusion(
            fusion_id="exact-test", detection_type="exact",
            master_cl=reg_cl, slave_cls=[xml_cl], confidence=1.0, evidence={},
        )
    ]
    registry = build_duplicates_registry(candidates, [reg_cl, xml_cl, solo])
    assert "solo-1" in registry
    assert registry["solo-1"]["duplicates"] == []
    assert registry["solo-1"]["origin"] == "xml"


def test_migration_merged_duplicates_vers_replaces():
    doc = {
        "generated_at": "x",
        "codelists": {
            "a": {"id": "a", "merged_duplicates": [{"id": "b"}]},
        },
    }
    doc = migrate_cleaned_doc(doc)
    assert doc["version"] == 2
    assert doc["codelists"]["a"]["replaces"] == [{"id": "b"}]
    assert "merged_duplicates" not in doc["codelists"]["a"]


def test_cleaned_registry_path():
    assert cleaned_registry_path("audit/reg.json") == "audit/cleaned_codelists.json"
    assert (
        cleaned_registry_path("s3://bucket/dir/reg.json")
        == "s3://bucket/dir/cleaned_codelists.json"
    )
    assert cleaned_registry_path("reg.json") == "cleaned_codelists.json"


# ────────────────────────────────────────────────────────────────────────
# validate_cleaned_doc
# ────────────────────────────────────────────────────────────────────────


def test_validate_doc_valide():
    doc = empty_cleaned_doc()
    doc["codelists"]["a"] = {
        "id": "a",
        "name": "N",
        "label": "",
        "codes": [["1", "Un"]],
        "replaces": [{"id": "b"}],
    }
    assert validate_cleaned_doc(doc) == []
    assert doc["codelists"]["a"]["codes_count"] == 1


def test_validate_doc_invalide():
    assert validate_cleaned_doc("pas un dict")
    doc = {"codelists": {"a": {"id": "", "codes": [["", "vide"]], "replaces": [{}]}}}
    errors = validate_cleaned_doc(doc)
    assert any(".id" in e for e in errors)
    assert any("codes[0]" in e for e in errors)
    assert any("replaces" in e for e in errors)


def test_validate_id_doit_egaler_la_cle():
    doc = {"codelists": {"a": {"id": "autre", "name": "", "label": "", "codes": [], "replaces": []}}}
    errors = validate_cleaned_doc(doc)
    assert any("doit être égal" in e for e in errors)


# ────────────────────────────────────────────────────────────────────────
# load_cleaned_codelists
# ────────────────────────────────────────────────────────────────────────


def test_load_cleaned_codelists(tmp_path):
    doc = empty_cleaned_doc()
    doc["codelists"]["a"] = {
        "id": "a",
        "name": "SEXE",
        "label": "Sexe",
        "codes": [["1", "Homme"], ["2", "Femme"]],
        "vars": ["SEXE"],
        "replaces": [{"id": "b"}, {"id": "c"}],
    }
    path = tmp_path / "cleaned_codelists.json"
    path.write_text(json.dumps(doc), encoding="utf-8")

    cls, replaced = load_cleaned_codelists(str(path))
    assert len(cls) == 1
    cl = cls[0]
    assert cl.origin == "registry"
    assert cl.codes == [("1", "Homme"), ("2", "Femme")]
    assert cl.vars == ["SEXE"]
    assert replaced == {"b", "c"}


def test_load_cleaned_codelists_absent():
    assert load_cleaned_codelists("/nexiste/pas.json") == ([], set())


def test_load_cleaned_codelists_legacy(tmp_path):
    doc = {"codelists": {"a": {"id": "a", "name": "N", "codes": [], "merged_duplicates": [{"id": "b"}]}}}
    path = tmp_path / "old.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    cls, replaced = load_cleaned_codelists(str(path))
    assert cls[0].origin == "registry"
    assert replaced == {"b"}


# ────────────────────────────────────────────────────────────────────────
# Priorité de détection + sérialisation origin
# ────────────────────────────────────────────────────────────────────────


def _cl(id_, name, origin="xml"):
    codes = [("1", "Homme"), ("2", "Femme")]
    cl = CodeList(id=id_, name=name, label="", codes=codes, origin=origin)
    cl.sig = signature_from_codes(cl.codes)
    return cl


def test_prepend_registre_devient_master():
    reg_cl = _cl("reg-1", "SEXE_CANONIQUE", origin="registry")
    xml_cl = _cl("xml-1", "SEXE_XML")
    groups = detect_exact_duplicates([reg_cl, xml_cl])
    assert len(groups) == 1
    group = next(iter(groups.values()))
    assert group[0].id == "reg-1"  # l'entrée registre est master


def _fusion(master, slaves, det="exact", conf=1.0):
    return CandidateFusion(
        fusion_id=f"{det}-test",
        detection_type=det,
        master_cl=master,
        slave_cls=slaves,
        confidence=conf,
        evidence={},
    )


def test_paire_listee_une_seule_fois():
    """Si B est doublon de A, A ne réapparaît pas sous B (pas d'entrée symétrique)."""
    a = _cl("a-1", "LISTE_A")
    b = _cl("b-1", "LISTE_B")
    registry = build_duplicates_registry([_fusion(a, [b])], [a, b])
    assert "a-1" in registry
    assert [d["id"] for d in registry["a-1"]["duplicates"]] == ["b-1"]
    # B n'a pas d'entrée top-level (ni parent ni « sans doublon »)
    assert "b-1" not in registry


def test_groupe_rattache_au_parent_unique():
    """Groupe A-B-C (paires A↔B et B↔C) → une seule entrée, parent A, dups B et C."""
    a = _cl("a-1", "LISTE_A")
    b = _cl("b-1", "LISTE_B")
    c = _cl("c-1", "LISTE_C")
    candidates = [_fusion(a, [b]), _fusion(b, [c], det="fuzzy", conf=0.9)]
    registry = build_duplicates_registry(candidates, [a, b, c])
    assert set(registry.keys()) == {"a-1"}
    assert {d["id"] for d in registry["a-1"]["duplicates"]} == {"b-1", "c-1"}
    # C porte les types de SA détection (fuzzy via B)
    c_dup = next(d for d in registry["a-1"]["duplicates"] if d["id"] == "c-1")
    assert c_dup["detection_types"] == ["fuzzy"]


def test_fusion_des_deux_sens_dune_paire():
    """Détections en sens opposés (fuzzy A→B, semantic B→A) fusionnées sous A."""
    a = _cl("a-1", "LISTE_A")
    b = _cl("b-1", "LISTE_B")
    candidates = [
        _fusion(a, [b], det="fuzzy", conf=0.91),
        _fusion(b, [a], det="semantic_list", conf=0.97),
    ]
    registry = build_duplicates_registry(candidates, [a, b])
    assert set(registry.keys()) == {"a-1"}
    dup = registry["a-1"]["duplicates"][0]
    assert dup["id"] == "b-1"
    assert dup["detection_types"] == ["fuzzy", "semantic_list"]
    assert dup["confidence"] == 0.97


def test_origin_serialise_dans_le_registre_des_doublons():
    reg_cl = _cl("reg-1", "SEXE_CANONIQUE", origin="registry")
    xml_cl = _cl("xml-1", "SEXE_XML")
    candidates = [
        CandidateFusion(
            fusion_id="exact-test",
            detection_type="exact",
            master_cl=reg_cl,
            slave_cls=[xml_cl],
            confidence=1.0,
            evidence={},
        )
    ]
    registry = build_duplicates_registry(candidates, [reg_cl, xml_cl])
    parent = registry["reg-1"]
    assert parent["origin"] == "registry"
    assert parent["duplicates"][0]["origin"] == "xml"
