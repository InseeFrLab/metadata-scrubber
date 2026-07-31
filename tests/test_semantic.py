"""Tests unitaires pour la détection sémantique."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from metadata_scrubber.semantic import (
    _TRUNCATE_CL,
    _CandidatePair,
    _VarRecord,
    _cl_texts,
    _embed_texts,
    detect_semantic_codelists,
    detect_semantic_via_variables,
    llm_judge,
    pairs_to_candidates,
    run_semantic_detection,
)
from metadata_scrubber.types import CodeList


class TestEmbedTexts:
    """Tests de la fonction _embed_texts."""

    def test_embed_texts_shape(self):
        """Retourne un tableau numpy avec la bonne forme."""
        client = MagicMock()
        # Simuler 3 textes → embeddings de dimension 10
        mock_embedding = MagicMock()
        mock_embedding.embedding = [0.1] * 10
        client.embeddings.create.return_value = MagicMock(data=[mock_embedding] * 3)

        vecs = _embed_texts(client, ["text1", "text2", "text3"])

        assert isinstance(vecs, np.ndarray)
        assert vecs.shape == (3, 10)
        # Vérifier que les vecteurs sont normalisés (norme ≈ 1)
        norms = np.linalg.norm(vecs, axis=1)
        np.testing.assert_allclose(norms, 1.0, atol=1e-6)

    def test_embed_texts_batch(self):
        """Gère correctement les batches."""
        client = MagicMock()
        # Un appel unique avec 3 embeddings (pas de batching, mais teste la logique)
        mock_emb = MagicMock()
        mock_emb.embedding = [0.5, 0.5]
        client.embeddings.create.return_value = MagicMock(data=[mock_emb] * 3)

        vecs = _embed_texts(client, ["a", "b", "c"])

        assert vecs.shape == (3, 2)


class TestClTexts:
    """Tests de _cl_texts (troncature avant embedding)."""

    def test_truncates_large_codelist(self, capsys):
        """Une CodeList au texte trop long est tronquée à _TRUNCATE_CL caractères."""
        codes = [(str(i), f"Libellé numéro {i}") for i in range(3000)]
        cl = CodeList(id="cl-huge", name="HUGE", label="Grosse liste", codes=codes)

        texts = _cl_texts([cl])

        assert len(texts) == 1
        assert len(texts[0]) == _TRUNCATE_CL
        captured = capsys.readouterr()
        assert "tronquée" in captured.out
        assert "HUGE" in captured.out

    def test_does_not_truncate_small_codelist(self, capsys):
        """Une CodeList au texte court n'est pas signalée comme tronquée."""
        cl = CodeList(id="cl-small", name="SMALL", label="Petite liste", codes=[("1", "Un")])

        texts = _cl_texts([cl])

        assert len(texts) == 1
        assert len(texts[0]) < _TRUNCATE_CL
        captured = capsys.readouterr()
        assert captured.out == ""


class TestDetectSemanticCodelists:
    """Tests de detect_semantic_codelists."""

    def test_returns_empty_when_no_codelists(self):
        """Retourne une liste vide si aucune CodeList."""
        client_mock = MagicMock()
        mock_emb = MagicMock()
        mock_emb.embedding = [1.0, 0.0]
        client_mock.embeddings.create.return_value = MagicMock(data=[mock_emb])
        client_mock.chat.completions.create.return_value = MagicMock(
            choices=[
                MagicMock(
                    message=MagicMock(
                        content='{"meme_concept": true, "confiance": 0.9, "raison": "test"}'
                    )
                )
            ]
        )

        with patch("metadata_scrubber.semantic._get_openai_client", return_value=client_mock):
            codelists = [
                CodeList(id="cl-1", name="A", label="", codes=[("1", "Un")]),
            ]
            pairs = detect_semantic_codelists(codelists, threshold=0.5)
            # Une seule CodeList → pas de paires possibles
            assert pairs == []

    def test_returns_empty_when_below_threshold(self):
        """Retourne une liste vide si toutes les similitudes sont sous le seuil."""
        client_mock = MagicMock()
        # Embeddings orthogonaux (cosinus ≈ 0)
        mock_emb_a = MagicMock()
        mock_emb_a.embedding = [1.0, 0.0]
        mock_emb_b = MagicMock()
        mock_emb_b.embedding = [0.0, 1.0]

        client_mock.embeddings.create.return_value = MagicMock(data=[mock_emb_a, mock_emb_b])

        with patch("metadata_scrubber.semantic._get_openai_client", return_value=client_mock):
            codelists = [
                CodeList(id="cl-a", name="Liste A", label="Label A", codes=[("1", "Un")]),
                CodeList(id="cl-b", name="Liste B", label="Label B", codes=[("2", "Deux")]),
            ]
            pairs = detect_semantic_codelists(codelists, threshold=0.9)
            assert pairs == []


class TestDetectSemanticViaVariables:
    """Tests de detect_semantic_via_variables."""

    def test_returns_empty_when_insufficient_records(self):
        """Retourne une liste vide si < 2 records."""
        client_mock = MagicMock()
        mock_emb = MagicMock()
        mock_emb.embedding = [0.7, 0.7]
        client_mock.embeddings.create.return_value = MagicMock(data=[mock_emb])

        with patch("metadata_scrubber.semantic._get_openai_client", return_value=client_mock):
            codelists = [
                CodeList(id="cl-1", name="A", label="", codes=[("1", "Un")]),
            ]
            var_records = [
                _VarRecord(
                    var_name="var1",
                    var_label="Label 1",
                    cl_id="cl-1",
                    cl_name="A",
                    text="var1 Label 1",
                )
            ]
            pairs = detect_semantic_via_variables(codelists, var_records)
            assert pairs == []

    def test_returns_empty_when_same_codelist(self):
        """Retourne une liste vide si les variables pointent vers la même CL."""
        client_mock = MagicMock()
        mock_emb_a = MagicMock()
        mock_emb_a.embedding = [0.9, 0.1]
        mock_emb_b = MagicMock()
        mock_emb_b.embedding = [0.8, 0.2]

        client_mock.embeddings.create.return_value = MagicMock(data=[mock_emb_a, mock_emb_b])

        with patch("metadata_scrubber.semantic._get_openai_client", return_value=client_mock):
            codelists = [
                CodeList(id="cl-1", name="A", label="", codes=[("1", "Un")]),
            ]
            var_records = [
                _VarRecord(
                    var_name="var1",
                    var_label="Label 1",
                    cl_id="cl-1",
                    cl_name="A",
                    text="var1 Label 1",
                ),
                _VarRecord(
                    var_name="var2",
                    var_label="Label 2",
                    cl_id="cl-1",
                    cl_name="A",
                    text="var2 Label 2",
                ),
            ]
            pairs = detect_semantic_via_variables(codelists, var_records, threshold=0.5)
            assert pairs == []  # même CodeList


class TestLlmJudge:
    """Tests de llm_judge."""

    def test_parses_json_response(self):
        """Parse correctement une réponse JSON du LLM."""
        client_mock = MagicMock()
        json_response = '{"meme_concept": true, "confiance": 0.95, "raison": "Mêmes codes"}'
        client_mock.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content=json_response))]
        )

        cl_a = CodeList(id="cl-1", name="Liste A", label="Label A", codes=[("1", "Un")])
        cl_b = CodeList(id="cl-2", name="Liste B", label="Label B", codes=[("2", "Deux")])

        result = llm_judge(client_mock, cl_a, cl_b)

        assert result.meme_concept is True
        assert result.confiance == pytest.approx(0.95)
        assert result.raison == "Mêmes codes"

    def test_handles_non_json_response(self):
        """Retourne un fallback si la réponse n'est pas du JSON."""
        client_mock = MagicMock()
        client_mock.chat.completions.create.return_value = MagicMock(
            choices=[MagicMock(message=MagicMock(content="Je ne sais pas..."))]
        )

        cl_a = CodeList(id="cl-1", name="Liste A", label="Label A", codes=[])
        cl_b = CodeList(id="cl-2", name="Liste B", label="Label B", codes=[])

        result = llm_judge(client_mock, cl_a, cl_b)

        assert result.meme_concept is False
        assert result.confiance == pytest.approx(0.0)
        assert result.raison == "Réponse non parseable"

    def test_handles_exception(self):
        """Gère les erreurs du client OpenAI."""
        client_mock = MagicMock()
        client_mock.chat.completions.create.side_effect = Exception("Network error")

        cl_a = CodeList(id="cl-1", name="Liste A", label="Label A", codes=[])
        cl_b = CodeList(id="cl-2", name="Liste B", label="Label B", codes=[])

        result = llm_judge(client_mock, cl_a, cl_b)

        assert result.meme_concept is False
        assert result.raison == "Erreur: Network error"


class TestPairsToCandidates:
    """Tests de pairs_to_candidates."""

    def test_conversion_basic(self):
        """Convertit correctement une paire en CandidateFusion."""
        cl_a = CodeList(
            id="cl-12345678",
            name="Liste A",
            label="Label A",
            codes=[("1", "Un"), ("2", "Deux")],
        )
        cl_b = CodeList(
            id="cl-87654321",
            name="Liste B",
            label="Label B",
            codes=[("1", "Uno"), ("2", "Dos")],
        )

        pairs = [
            _CandidatePair(
                cl_a=cl_a,
                cl_b=cl_b,
                score=0.92,
                phase="direct",
            )
        ]

        results = pairs_to_candidates(pairs)

        assert len(results) == 1
        cand = results[0]
        assert cand.detection_type == "semantic_list"
        assert cand.confidence == pytest.approx(0.92)
        # ID tronqué à 8 caractères
        assert cand.fusion_id == "semantic-s-cl-12345-cl-87654"
        assert cand.master_cl is cl_a
        assert cand.slave_cls == [cl_b]
        assert "cosine_score" in cand.evidence

    def test_conversion_with_judge(self):
        """Utilise le juge LLM pour la confiance."""
        cl_a = CodeList(id="m", name="M", label="", codes=[])
        cl_b = CodeList(id="s", name="S", label="", codes=[])

        pairs = [
            _CandidatePair(
                cl_a=cl_a,
                cl_b=cl_b,
                score=0.95,
                phase="direct",
            )
        ]

        judge_results = [
            MagicMock(
                meme_concept=True,
                confiance=0.85,
                raison="Concept similaire",
            )
        ]

        results = pairs_to_candidates(pairs, judge_results)

        assert results[0].confidence == pytest.approx(0.85)
        assert results[0].evidence["judge_meme_concept"] is True

    def test_conversion_semantic_var(self):
        """Déduit le type correct pour 'variable'."""
        cl_a = CodeList(
            id="cl-12345678",
            name="Liste A",
            label="Label A",
            codes=[("1", "Un")],
        )
        cl_b = CodeList(
            id="cl-87654321",
            name="Liste B",
            label="Label B",
            codes=[("2", "Deux")],
        )

        pairs = [
            _CandidatePair(
                cl_a=cl_a,
                cl_b=cl_b,
                score=0.92,
                phase="variable",
            )
        ]

        results = pairs_to_candidates(pairs)

        assert results[0].detection_type == "semantic_var"


class TestRunSemanticDetection:
    """Tests de run_semantic_detection."""

    def test_returns_empty_when_no_llm_and_no_var_records(self):
        """Retourne des listes vides si run_llm=False et pas de var_records."""
        client_mock = MagicMock()
        mock_emb = MagicMock()
        mock_emb.embedding = [1.0, 0.0]
        client_mock.embeddings.create.return_value = MagicMock(data=[mock_emb])

        with patch("metadata_scrubber.semantic._get_openai_client", return_value=client_mock):
            codelists = [
                CodeList(id="cl-1", name="A", label="", codes=[("1", "Un")]),
            ]
            pairs, judge_results = run_semantic_detection(
                codelists,
                run_llm=False,
            )
            assert pairs == []
            assert judge_results == []
