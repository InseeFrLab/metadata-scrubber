"""Tests unitaires pour le pipeline de dédoublonnage."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

# =====================================================================
# normalize.py
# =====================================================================

from scrubber.normalize import normalize, signature_from_codes
from scrubber.namespace import detect_ddi_namespace


class TestNormalize:
    def test_normalize_strips_and_lower(self):
        assert normalize("  Hello World  ") == "hello world"

    def test_normalize_nfc(self):
        # Unicode décomposé → composé via NFC
        assert normalize("café") == normalize("caf\u00e9")

    def test_normalize_multiple_spaces(self):
        assert normalize("a   b  c") == "a b c"


class TestSignatureFromCodes:
    def test_exact_match(self):
        codes = [("1", "Un"), ("2", "Deux")]
        sig = signature_from_codes(codes)
        assert sig == tuple(sorted({("1", "un"), ("2", "deux")}))

    def test_order_independent(self):
        codes1 = [("1", "Un"), ("2", "Deux")]
        codes2 = [("2", "Deux"), ("1", "Un")]
        assert signature_from_codes(codes1) == signature_from_codes(codes2)

    def test_case_insensitive(self):
        codes1 = [("1", "Un"), ("2", "Deux")]
        codes2 = [("1", "un"), ("2", "deux")]
        assert signature_from_codes(codes1) == signature_from_codes(codes2)

    def test_duplicated_codes(self):
        codes = [("1", "Un"), ("1", "Un")]  # doublon
        sig = signature_from_codes(codes)
        assert len(sig) == 1  # dédupliqué


# =====================================================================
# funnel.py
# =====================================================================

from scrubber.funnel import detect_exact_duplicates, detect_fuzzy_duplicates
from scrubber.types import CodeList


class TestDetectExactDuplicates:
    def build_cl(self, name: str, codes: list[tuple[str, str]]) -> CodeList:
        cl = CodeList(id=f"cl-{name}", name=name, label="", codes=codes)
        cl.sig = signature_from_codes(codes)
        return cl

    def test_no_duplicates(self):
        cl1 = self.build_cl("a", [("1", "Un")])
        cl2 = self.build_cl("b", [("1", "One")])
        groups = detect_exact_duplicates([cl1, cl2])
        assert groups == {}

    def test_exact_duplicates(self):
        cl1 = self.build_cl("a", [("1", "Un"), ("2", "Deux")])
        cl2 = self.build_cl("b", [("2", "Deux"), ("1", "Un")])  # ordre inversé
        groups = detect_exact_duplicates([cl1, cl2])
        assert len(groups) == 1
        assert len(groups[tuple(groups.keys())[0]]) == 2

    def test_non_duplicates(self):
        cl1 = self.build_cl("a", [("1", "Un")])
        cl2 = self.build_cl("b", [("1", "One")])
        groups = detect_exact_duplicates([cl1, cl2])
        assert groups == {}


class TestDetectFuzzyDuplicates:
    def build_cl(self, name: str, codes: list[tuple[str, str]]) -> CodeList:
        cl = CodeList(id=f"cl-{name}", name=name, label="", codes=codes)
        cl.sig = signature_from_codes(codes)
        return cl

    def test_fuzzy_high_similarity(self):
        cl1 = self.build_cl("a", [("1", "Un"), ("2", "Deux"), ("3", "Trois")])
        cl2 = self.build_cl("b", [("1", "Un"), ("2", "Deux"), ("3", "Troi")])  # typo
        detected, all_pairs, by_score = detect_fuzzy_duplicates([cl1, cl2])
        assert len(detected) >= 1  # haute similarité

    def test_fuzzy_no_match(self):
        cl1 = self.build_cl("a", [("1", "Sexe")])
        cl2 = self.build_cl("b", [("1", "Code Postal")])
        detected, _, by_score = detect_fuzzy_duplicates([cl1, cl2])
        assert len(detected) == 0


# =====================================================================
# signals.py
# =====================================================================

from scrubber.signals import cross_check, find_usage_groups


class TestCrossCheck:
    def test_same_usage(self):
        a = CodeList(id="a", name="A", label="", codes=[], vars=["x", "y"])
        b = CodeList(id="b", name="B", label="", codes=[], vars=["x", "y"])
        sig = cross_check(a, b)
        assert sig.usage_type == "same"
        assert sig.shared_vars == ("x", "y")
        assert sig.same_usage is True

    def test_partial_usage(self):
        a = CodeList(id="a", name="A", label="", codes=[], vars=["x", "y"])
        b = CodeList(id="b", name="B", label="", codes=[], vars=["y", "z"])
        sig = cross_check(a, b)
        assert sig.usage_type == "partial"
        assert sig.shared_vars == ("y",)
        assert sig.only_a == ("x",)
        assert sig.only_b == ("z",)

    def test_disjoint_usage(self):
        a = CodeList(id="a", name="A", label="", codes=[], vars=["x"])
        b = CodeList(id="b", name="B", label="", codes=[], vars=["y"])
        sig = cross_check(a, b)
        assert sig.usage_type == "disjoint"
        assert sig.shared_vars == ()

    def test_shared_ratio(self):
        a = CodeList(id="a", name="A", label="", codes=[], vars=["x", "y"])
        b = CodeList(id="b", name="B", label="", codes=[], vars=["y", "z"])
        sig = cross_check(a, b)
        assert sig.shared_ratio == pytest.approx(1 / 3)


class TestFindUsageGroups:
    def test_groups_identical_sig(self):
        cl1 = CodeList(id="a", name="A", label="", codes=[], vars=["x"])
        cl1.var_sig = ("x",)
        cl2 = CodeList(id="b", name="B", label="", codes=[], vars=["x"])
        cl2.var_sig = ("x",)
        cl3 = CodeList(id="c", name="C", label="", codes=[], vars=["y"])
        cl3.var_sig = ("y",)

        groups = find_usage_groups([cl1, cl2, cl3])
        assert len(groups) == 1
        assert len(groups[("x",)]) == 2


# =====================================================================
# types.py
# =====================================================================

from scrubber.types import CandidateFusion


class TestCandidateFusion:
    def test_all_ids(self):
        master = CodeList(id="m", name="M", label="", codes=[])
        slave1 = CodeList(id="s1", name="S1", label="", codes=[])
        slave2 = CodeList(id="s2", name="S2", label="", codes=[])
        cand = CandidateFusion(
            fusion_id="f1",
            detection_type="exact",
            master_cl=master,
            slave_cls=[slave1, slave2],
            confidence=1.0,
        )
        assert cand.all_ids == ["m", "s1", "s2"]

    def test_shared_vars(self):
        master = CodeList(id="m", name="M", label="", codes=[], vars=["x", "y"])
        slave = CodeList(id="s", name="S", label="", codes=[], vars=["y", "z"])
        cand = CandidateFusion(
            fusion_id="f1",
            detection_type="exact",
            master_cl=master,
            slave_cls=[slave],
            confidence=1.0,
        )
        assert cand.shared_vars == {"y"}


# =====================================================================
# extraction from raw XML string
# =====================================================================

XML_SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<MetaDataDocument>
  <Fragment>
    <CodeList>
      <ID>cl-001</ID>
      <CodeListName>
        <String>N_statut</String>
      </CodeListName>
      <Code>
        <ID>cd-1</ID>
        <CategoryReference>
          <ID>cat-1</ID>
        </CategoryReference>
        <Value>1</Value>
      </Code>
      <Code>
        <ID>cd-2</ID>
        <CategoryReference>
          <ID>cat-2</ID>
        </CategoryReference>
        <Value>2</Value>
      </Code>
    </CodeList>
  </Fragment>
  <Fragment>
    <CodeList>
      <ID>cl-002</ID>
      <CodeListName>
        <String>N_statut_dup</String>
      </CodeListName>
      <Code>
        <ID>cd-3</ID>
        <CategoryReference>
          <ID>cat-1</ID>
        </CategoryReference>
        <Value>1</Value>
      </Code>
      <Code>
        <ID>cd-4</ID>
        <CategoryReference>
          <ID>cat-2</ID>
        </CategoryReference>
        <Value>2</Value>
      </Code>
    </CodeList>
  </Fragment>
  <Fragment>
    <Category>
      <ID>cat-1</ID>
      <Label>
        <Content>Actif</Content>
      </Label>
    </Category>
  </Fragment>
  <Fragment>
    <Category>
      <ID>cat-2</ID>
      <Label>
        <Content>Inactif</Content>
      </Label>
    </Category>
  </Fragment>
  <Fragment>
    <Variable>
      <ID>var-1</ID>
      <VariableName>
        <String>statut</String>
      </VariableName>
      <Label>
        <Content>Statut de individu</Content>
      </Label>
      <VariableRepresentation>
        <CodeRepresentation>
          <CodeListReference>
            <ID>cl-001</ID>
          </CodeListReference>
        </CodeRepresentation>
      </VariableRepresentation>
    </Variable>
  </Fragment>
</MetaDataDocument>
"""


class TestExtractFromXML:
    def test_parse_xml(self):
        from scrubber.extractor import parse_xml
        objects = parse_xml(XML_SAMPLE)
        assert len(objects) == 5  # 2 CodeList + 2 Category + 1 Variable

    def test_extract_categories(self):
        from scrubber.extractor import parse_xml, extract_categories
        objects = parse_xml(XML_SAMPLE)
        cats = extract_categories(objects)
        assert cats["cat-1"] == "Actif"
        assert cats["cat-2"] == "Inactif"

    def test_extract_codelists(self):
        from scrubber.extractor import parse_xml, extract_codelists
        objects = parse_xml(XML_SAMPLE)
        cls = extract_codelists(objects)
        assert len(cls) == 2

        cl1 = next(c for c in cls if c.id == "cl-001")
        assert cl1.name == "N_statut"
        assert len(cl1.codes) == 2
        assert ("1", "Actif") in cl1.codes
        assert ("2", "Inactif") in cl1.codes

    def test_exact_detection_on_sample(self):
        from scrubber.extractor import parse_xml, extract_codelists
        from scrubber.funnel import detect_exact_duplicates
        from scrubber.normalize import signature_from_codes
        objects = parse_xml(XML_SAMPLE)
        codelists = extract_codelists(objects)
        for cl in codelists:
            cl.sig = signature_from_codes(cl.codes)

        groups = detect_exact_duplicates(codelists)
        # cl-001 et cl-002 ont exactement les mêmes codes → doivent être groupés
        assert len(groups) == 1

    def test_full_extract(self):

        result = full_extract(XML_SAMPLE)
        assert result["cl_count"] == 2
        assert result["var_count"] == 1
        assert result["category_count"] == 2


# =====================================================================
# Reporting — duplicates_registry
# =====================================================================

from scrubber.extractor import full_extract


class TestDuplicatesRegistry:
    def test_empty_candidates(self):
        from scrubber.funnel import detect_exact_duplicates, detect_fuzzy_duplicates
        from scrubber.normalize import signature_from_codes
        from scrubber.reporting.duplicates_registry import (
            build_duplicates_registry,
            write_duplicates_registry,
        )

        # Sans candidats, le registry est vide
        registry = build_duplicates_registry([], [])
        assert registry == {}

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "codelist_duplicates.json")
            path = write_duplicates_registry([], [], output_path)
            assert path == output_path

            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            assert data == {}

    def test_simple_duplicate_pair(self):
        from scrubber.funnel import detect_exact_duplicates, detect_fuzzy_duplicates
        from scrubber.normalize import signature_from_codes
        from scrubber.reporting.duplicates_registry import (
            build_duplicates_registry,
            write_duplicates_registry,
        )
        from scrubber.types import CandidateFusion

        # Extraire les CodeLists
        result = full_extract(XML_SAMPLE)
        codelists = result["codelists"]
        for cl in codelists:
            cl.sig = signature_from_codes(cl.codes)

        # Pipeline funnel exact
        exact_groups = detect_exact_duplicates(codelists)
        assert len(exact_groups) == 1  # cl-001 et cl-002 dupliqués

        # Pipeline funnel fuzzy
        detected, pairs, by_score = detect_fuzzy_duplicates(codelists)
        assert isinstance(detected, list)

        # Construire des candidats manuellement pour tester le registry
        group = list(exact_groups.values())[0]
        master = group[0]
        slaves = group[1:]
        candidates = [
            CandidateFusion(
                fusion_id=f"exact-{master.id[:8]}",
                detection_type="exact",
                master_cl=master,
                slave_cls=slaves,
                confidence=1.0,
                evidence={"codes_count": len(master.codes)},
            )
        ]
        assert len(candidates) == 1
        assert candidates[0].detection_type == "exact"

        # Build registry depuis les candidats — chaque paire n'apparaît qu'une
        # fois : cl-001 (premier dans le document) est parent, cl-002 n'a pas
        # d'entrée top-level.
        registry = build_duplicates_registry(candidates, codelists)
        assert "cl-001" in registry
        assert "cl-002" not in registry
        assert [d["id"] for d in registry["cl-001"]["duplicates"]] == ["cl-002"]

        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "codelist_duplicates.json")
            path = write_duplicates_registry(candidates, codelists, output_path)
            assert path == output_path

            with open(path, encoding="utf-8") as f:
                data = json.load(f)

            assert "cl-001" in data
            assert "cl-002" not in data


# =====================================================================
# namespace.py
# =====================================================================


class TestNamespaceDetection:
    """Tests d'identification des namespaces DDI dans les CodeLists."""

    def test_has_ddi_33_namespace(self):
        """Une CL avec un namespace DDI-3.x doit etre detectee."""
        ns33_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <MetaDataDocument xmlns:="ddi:reusable:3_3">
            <Fragment>
                <Group>
                    <GroupID>G-1</GroupID>
                    <GroupTitle><String>Nom Groupe</String></GroupTitle>
                </Group>
            </Fragment>
        </MetaDataDocument>
        """
        res = detect_ddi_namespace(ns33_xml)
        assert res == "3.3"

    def test_has_ddi_6_namespace(self):
        """Une CL avec un namespace DDI-6.x doit etre detectee."""
        ns6_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <MetaDataDocument>
            <Fragment>
                <Group xmlns="ddi:code:6_0">
                    <GroupID>G-2</GroupID>
                </Group>
            </Fragment>
        </MetaDataDocument>
        """
        res = detect_ddi_namespace(ns6_xml)
        assert res == "6.0"

    def test_no_namespace(self):
        """Un XML sans namespace valide retourne None."""
        no_ns_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <MetaDataDocument>
            <Fragment>
                <CodeList>
                    <ID>cl-01</ID>
                </CodeList>
            </Fragment>
        </MetaDataDocument>
        """
        res = detect_ddi_namespace(no_ns_xml)
        assert res is None
