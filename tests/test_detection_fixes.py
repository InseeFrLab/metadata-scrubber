"""Tests des correctifs de détection (score symétrique, confiance, badges, `used`)."""

from __future__ import annotations

import pytest

from metadata_scrubber.funnel import _code_similarity
from metadata_scrubber.main import MAX_NON_EXACT_CONFIDENCE, _build_candidates
from metadata_scrubber.normalize import signature_from_codes
from metadata_scrubber.reporting.duplicates_registry import build_duplicates_registry
from metadata_scrubber.types import CandidateFusion, CodeList


def _cl(cl_id: str, n: int, extra: int = 0, name: str = "") -> CodeList:
    """Liste de n codes communs (c0..cn-1) + `extra` codes propres à cette liste."""
    codes = [(f"c{i}", f"Libelle numero {i}") for i in range(n)]
    codes += [(f"{cl_id}-x{i}", f"Specifique {cl_id} {i}") for i in range(extra)]
    cl = CodeList(id=cl_id, name=name or cl_id, label="", codes=codes)
    cl.sig = signature_from_codes(cl.codes)
    return cl


class TestSymmetricSimilarity:
    def test_identical_lists(self):
        assert _code_similarity(_cl("a", 10), _cl("b", 10)) == 1.0

    def test_symmetric(self):
        small, big = _cl("a", 13), _cl("b", 13, extra=9)
        assert _code_similarity(small, big) == _code_similarity(big, small)

    def test_subset_is_no_longer_100_percent(self):
        small, big = _cl("a", 13), _cl("b", 13, extra=9)
        sim = _code_similarity(small, big)
        assert sim == pytest.approx(13 / 22)
        assert sim < 0.75  # sous min_code_sim : paire écartée quel que soit l'ordre

    def test_few_extra_codes_stay_close(self):
        a, b = _cl("a", 85), _cl("b", 85, extra=3)
        assert _code_similarity(a, b) == pytest.approx(85 / 88)


class TestLargeLists:
    """Listes de plus de 100 codes (ex. L_TYPEQU de la BPE : 229 ⊂ 232)."""

    def test_large_lists_are_compared(self):
        from metadata_scrubber.funnel import detect_fuzzy_duplicates

        a, b = _cl("a", 229, name="L_TYPEQU_2024_DIFF"), _cl("b", 229, extra=3, name="L_TYPEQU_2024")
        detected, _, _, excluded = detect_fuzzy_duplicates([a, b])
        assert excluded == []
        assert len(detected) == 1
        assert detected[0]["code_similarity"] == pytest.approx(229 / 232, abs=1e-4)

    def test_same_value_with_different_label_is_matched_by_label(self):
        a = CodeList(id="a", name="a", label="", codes=[("1", "Oui"), ("2", "Non")])
        b = CodeList(id="b", name="b", label="", codes=[("1", "Oui"), ("2", "Sans objet")])
        assert _code_similarity(a, b) == pytest.approx(0.5)

    def test_remaining_codes_still_use_pairwise_search(self):
        # Valeurs proches mais non identiques : appariées par la 2e passe
        a = CodeList(id="a", name="a", label="", codes=[("A0101", "Gendarmerie"), ("A0102", "Police")])
        b = CodeList(id="b", name="b", label="", codes=[("A0101 ", "Gendarmerie"), ("A0102 ", "Police")])
        assert _code_similarity(a, b) == 1.0

    def test_pairwise_search_is_skipped_on_large_remainders(self):
        a = CodeList(id="a", name="a", label="", codes=[(f"val{i:04d}", f"L{i}") for i in range(150)])
        b = CodeList(id="b", name="b", label="", codes=[(f"val{i:04d} ", f"L{i}") for i in range(150)])
        assert _code_similarity(a, b) == 0.0
        assert _code_similarity(a, b, max_pairwise=200) == 1.0

    def test_size_ratio_prefilter(self):
        from metadata_scrubber.funnel import detect_fuzzy_duplicates

        # 13 codes contre 22 : ratio 0.59 < min_code_sim, paire écartée sans calcul
        detected, nearby, _, _ = detect_fuzzy_duplicates([_cl("a", 13), _cl("b", 13, extra=9)])
        assert detected == [] and nearby == {}

    def test_large_pair_is_fast(self):
        import time

        a, b = _cl("a", 300), _cl("b", 290, extra=10)
        start = time.perf_counter()
        _code_similarity(a, b)
        assert time.perf_counter() - start < 1.0


class TestBuildCandidates:
    def _fuzzy_pair(self, a, b, score):
        return {"a": a, "b": b, "score": score, "a_codes": len(a.codes), "b_codes": len(b.codes)}

    def test_fuzzy_confidence_is_capped_below_exact(self):
        a, b = _cl("aaaaaaaa", 5), _cl("bbbbbbbb", 5)
        cands = _build_candidates({}, [self._fuzzy_pair(a, b, 1.0)], [a, b], {})
        assert cands[0].confidence == MAX_NON_EXACT_CONFIDENCE

    def test_exact_keeps_confidence_one(self):
        a, b = _cl("aaaaaaaa", 5), _cl("bbbbbbbb", 5)
        cands = _build_candidates({a.sig: [a, b]}, [], [a, b], {})
        assert cands[0].confidence == 1.0

    def test_fuzzy_slave_is_not_reported_again_by_usage(self):
        a, b = _cl("aaaaaaaa", 5), _cl("bbbbbbbb", 5, extra=1)
        cands = _build_candidates(
            {}, [self._fuzzy_pair(a, b, 0.93)], [a, b], {("v1",): [a, b]}
        )
        assert [c.detection_type for c in cands] == ["fuzzy"]

    def test_fuzzy_chain_is_kept(self):
        a, b, c = _cl("aaaaaaaa", 5), _cl("bbbbbbbb", 5), _cl("cccccccc", 5)
        pairs = [self._fuzzy_pair(a, b, 0.95), self._fuzzy_pair(b, c, 0.94)]
        cands = _build_candidates({}, pairs, [a, b, c], {})
        assert len(cands) == 2


def _candidate(kind, master, slave, conf):
    return CandidateFusion(
        fusion_id=f"{kind}-{master.id}-{slave.id}",
        detection_type=kind,
        master_cl=master,
        slave_cls=[slave],
        confidence=conf,
    )


class TestHonestBadges:
    def test_direct_pair_with_parent_wins(self):
        parent, sat = _cl("parent", 3), _cl("sat", 3)
        other = _cl("other", 3)
        # sat est exact avec « other » (satellite) mais flou avec le parent
        cands = [
            _candidate("fuzzy", parent, sat, 0.95),
            _candidate("exact", other, sat, 1.0),
            _candidate("fuzzy", parent, other, 0.93),
        ]
        reg = build_duplicates_registry(cands, [parent, sat, other])
        dup = next(d for d in reg["parent"]["duplicates"] if d["id"] == "sat")
        assert dup["detection_types"] == ["fuzzy"]
        assert dup["confidence"] == 0.95
        assert "via" not in dup

    def test_indirect_link_reports_via(self):
        parent, mid, far = _cl("parent", 3), _cl("mid", 3), _cl("far", 3)
        cands = [
            _candidate("exact", parent, mid, 1.0),
            _candidate("fuzzy", mid, far, 0.94),
        ]
        reg = build_duplicates_registry(cands, [parent, mid, far])
        dup = next(d for d in reg["parent"]["duplicates"] if d["id"] == "far")
        assert dup["detection_types"] == ["fuzzy"]
        assert dup["via"] == ["mid"]
