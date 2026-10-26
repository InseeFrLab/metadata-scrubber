"""Codes d'échappement : détection « exact hors échappement » et « libellés »."""

import pytest

from metadata_scrubber.funnel import detect_business_duplicates, detect_label_duplicates
from metadata_scrubber.main import _build_candidates
from metadata_scrubber.normalize import normalize, signature_from_codes
from metadata_scrubber.quality import is_escape_code, split_codes
from metadata_scrubber.reporting.duplicates_registry import build_duplicates_registry
from metadata_scrubber.types import CodeList


def _cl(cl_id: str, codes: list[tuple[str, str]]) -> CodeList:
    """CodeList avec ses signatures calculées comme à l'étape 4 du pipeline."""
    cl = CodeList(id=cl_id, name=cl_id, label="", codes=codes)
    cl.sig = signature_from_codes(codes)
    business, _ = split_codes(codes)
    cl.sig_business = signature_from_codes(business)
    cl.sig_labels = tuple(sorted({normalize(lab) for _v, lab in business if lab}))
    return cl


# Configurations tirées de BPE.xml
ZONAGE = [("1", "Zone urbaine"), ("2", "Zone rurale"), ("3", "Zone mixte")]
ZONAGE_A = _cl("58fc5213", ZONAGE + [("x", "Non concerné par le zonage")])
ZONAGE_B = _cl("c5d62056", ZONAGE + [("_Z", "Sans objet")])
PRESENCE_01 = _cl("c61876ef", [("_Z", "Sans objet"), ("1", "Présence"), ("0", "Absence"),
                               ("_U", "Indéterminé")])
PRESENCE_OUI = _cl("16f2abd4", [("ind", "Indéterminé"), ("oui", "Présence"),
                                ("nc", "Non concerné"), ("non", "Absence")])
PRESENCE_SEULE = _cl("f5387063", [("0", "Absence"), ("1", "Présence")])
QUALITE_LONG = _cl("dccbfe0f", [("Acceptable", "Acceptable"), ("Bonne", "Bonne"),
                                ("Mauvaise", "Mauvaise")])
QUALITE_COURT = _cl("40bbd7a8", [("A", "Acceptable"), ("B", "Bonne"), ("M", "Mauvaise")])


class TestIsEscapeCode:
    @pytest.mark.parametrize("value,label", [
        ("_Z", "Sans objet"),
        ("_U", "Indéterminé"),
        ("x", "Non concerné par le zonage"),
        ("nr", "Non renseigné"),
        ("/", "Sans objet (individus qui ne sont pas nés à l'étranger)"),
        ("9", "Valeur manquante"),
    ])
    def test_escape(self, value, label):
        assert is_escape_code(value, label)

    @pytest.mark.parametrize("value,label", [
        ("1", "Contrat à durée indéterminée"),
        ("1", "Présence"),
        ("A101", "Type d'équipements hors champ de la culture"),
        ("x", ""),
    ])
    def test_business(self, value, label):
        assert not is_escape_code(value, label)


class TestBusinessDuplicates:
    def test_same_business_codes_different_escape_codes(self):
        groups = detect_business_duplicates([ZONAGE_A, ZONAGE_B])
        assert [[cl.id for cl in g] for g in groups] == [["58fc5213", "c5d62056"]]

    def test_list_without_escape_codes_joins_the_group(self):
        groups = detect_business_duplicates([PRESENCE_01, PRESENCE_SEULE])
        assert [[cl.id for cl in g] for g in groups] == [["c61876ef", "f5387063"]]

    def test_single_business_code_lists_are_not_grouped(self):
        a = _cl("a", [("C", "Cohérent"), ("1", "Non réponse")])
        b = _cl("b", [("C", "Cohérent"), ("/", "Sans objet")])
        assert detect_business_duplicates([a, b]) == []

    def test_exact_duplicates_are_not_reported_again(self):
        twin = _cl("twin", ZONAGE_A.codes)
        assert detect_business_duplicates([ZONAGE_A, twin]) == []


class TestLabelDuplicates:
    def test_same_labels_different_values(self):
        groups = detect_label_duplicates([PRESENCE_01, PRESENCE_OUI, QUALITE_LONG, QUALITE_COURT])
        assert sorted([cl.id for cl in g] for g in groups) == [
            ["c61876ef", "16f2abd4"], ["dccbfe0f", "40bbd7a8"],
        ]

    def test_single_label_lists_are_not_grouped(self):
        a = _cl("a", [("cf", "Voir dictionnaire des nomenclatures")])
        b = _cl("b", [("Cf", "Voir dictionnaire des nomenclatures"), ("9", "Non renseigné")])
        c = _cl("c", [("x", "Voir dictionnaire des nomenclatures")])
        assert detect_label_duplicates([a, c]) == []
        assert detect_label_duplicates([a, b]) == []


def test_candidates_and_registry():
    codelists = [ZONAGE_A, ZONAGE_B, QUALITE_LONG, QUALITE_COURT]
    cands = _build_candidates(
        {}, [], codelists, {},
        business_groups=detect_business_duplicates(codelists),
        label_groups=detect_label_duplicates(codelists),
    )
    assert {(c.detection_type, c.confidence) for c in cands} == {
        ("exact_business", 0.98), ("labels", 0.95),
    }

    reg = build_duplicates_registry(cands, codelists)
    dup = reg["58fc5213"]["duplicates"][0]
    assert dup["detection_types"] == ["exact_business"]
    assert reg["58fc5213"]["escape_codes"] == [["x", "Non concerné par le zonage"]]
    assert dup["escape_codes"] == [["_Z", "Sans objet"]]
    assert reg["dccbfe0f"]["duplicates"][0]["detection_types"] == ["labels"]
