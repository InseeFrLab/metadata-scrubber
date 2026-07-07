"""Détection sémantique de doublons — embeddings + juge LLM.

Pipeline inspiré de exploration/explore_standalone.py :
  1. Phase directe   : embeddings des CodeLists (nom + label + codes)
  2. Phase inverse    : embeddings des Variables → paires de CodeLists candidates
  3. Juge LLM         : validation finale sur les paires à haut score
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from itertools import combinations
from typing import Any

import numpy as np
from openai import OpenAI

from .normalize import concat_text
from .types import CodeList, CandidateFusion

logger = logging.getLogger(__name__)

# ---------- Configurations par défaut ----------

_EMBEDDING_MODEL: str = "qwen3-embedding-8b"  # modèle d'embeddings
_JUDGE_MODEL: str = "gemma4-26b-moe"  # modèle "juge"
_EMBEDDING_BATCH: int = 128  # batch size pour les embeddings
_SIM_CL_THRESHOLD: float = 0.90  # similitude CodeList → candidate
_SIM_VAR_THRESHOLD: float = 0.92  # similitude Variable → candidate

# Textes tronqués à ces longueurs pour ne pas saturer le token limit
_TRUNCATE_CL: int = 8000
_TRUNCATE_VAR: int = 4000


# ================================================================
# 1. Embeddings
# ================================================================


def _embed_texts(
    client: OpenAI,
    texts: list[str],
    *,
    model: str = _EMBEDDING_MODEL,
    batch_size: int = _EMBEDDING_BATCH,
    verbose: bool = False,
) -> np.ndarray:
    """
    Calcule les embeddings d'une liste de textes par batch.

    Args:
        texts: Listes de textes à embedded.
        model: Modèle d'embedding.
        batch_size: Taille des batchs.
        verbose: Si True, affiche les détails des appels API.

    Returns:
        Tableau numpy de forme (n_textes, dimension) normalisé en cosinus.
    """
    if verbose:
        print(f"  [embeddings] modèle={model}, {len(texts)} textes")

    vecs: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        chunk = texts[i : i + batch_size]
        t0 = time.perf_counter()
        resp = client.embeddings.create(model=model, input=chunk)
        elapsed = time.perf_counter() - t0
        if verbose:
            print(
                f"  [embeddings]   batch {i // batch_size + 1}/"
                f"{(len(texts) + batch_size - 1) // batch_size}: "
                f"{len(chunk)} textes → {elapsed:.2f}s"
            )
        vecs.extend(d.embedding for d in resp.data)

    M = np.array(vecs, dtype="float32")
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0] = 1.0  # éviter div by zero
    M /= norms
    return M


def _get_openai_client() -> OpenAI:
    """Retourne un client OpenAI via les variables d'environnement."""
    base = os.environ.get("OPENAI_BASE_URL")
    key = os.environ.get("OPENAI_API_KEY")
    if not base or not key:
        raise RuntimeError(
            "Les variables OPENAI_BASE_URL et OPENAI_API_KEY "
            "sont requises pour la détection sémantique."
        )
    return OpenAI(base_url=base, api_key=key)


# ================================================================
# 2. Phase 1 — Embeddings directs des CodeLists
# ================================================================


def _cl_texts(codelists: list[CodeList]) -> list[str]:
    """Pour chaque CodeList, un texte concaténé pour l'embedding."""
    return [concat_text(cl.name, cl.label, cl.codes) for cl in codelists]


@dataclass
class _CandidatePair:
    """Paire candidate avec ses métadonnées."""

    cl_a: CodeList
    cl_b: CodeList
    score: float
    phase: str  # "direct" ou "variable"


def detect_semantic_codelists(
    codelists: list[CodeList],
    *,
    threshold: float | None = None,
    verbose: bool = False,
) -> list[_CandidatePair]:
    """
    Phase 1 — Détection sémantique directe des CodeLists.

    Compose le texte de chaque CodeList (nom + label + codes) et calcule
    la similarité cosinus entre tous les paires via des embeddings.

    Args:
        codelists: Liste complète de CodeList avec signatures calculées.
        threshold: Seuil de similitude cosinus. Par défaut _SIM_CL_THRESHOLD.
        verbose: Si True, affiche les détails des appels API.

    Returns:
        Liste de paires candidates (score ≥ threshold) triée par score décroissant.
    """
    threshold = threshold or _SIM_CL_THRESHOLD

    texts = _cl_texts(codelists)
    client = _get_openai_client()
    vecs = _embed_texts(client, texts, verbose=verbose)

    sims = vecs @ vecs.T  # cosinus (vecteurs déjà normalisés)

    pairs: list[_CandidatePair] = []
    n = len(codelists)
    for i, j in combinations(range(n), 2):
        score = float(sims[i, j])
        if score >= threshold:
            pairs.append(
                _CandidatePair(
                    cl_a=codelists[i],
                    cl_b=codelists[j],
                    score=round(score, 4),
                    phase="direct",
                )
            )

    pairs.sort(key=lambda p: p.score, reverse=True)
    logger.info("Semantic CL: %d paires candidates >= %.2f", len(pairs), threshold)
    return pairs


# ================================================================
# 3. Phase 2 — Embeddings indirects via Variables
# ================================================================


@dataclass
class _VarRecord:
    """Enregistrement d'une variable pour l'embedding sémantique."""

    var_name: str
    var_label: str
    cl_id: str
    cl_name: str
    text: str


def detect_semantic_via_variables(
    codelists: list[CodeList],
    var_records: list[_VarRecord],
    *,
    threshold: float | None = None,
    verbose: bool = False,
) -> list[_CandidatePair]:
    """
    Phase 2 — Détection sémantique indirecte via les Variables.

    Idée inversée : au lieu de comparer les CodeLists directement, on compare
    les variables qui les utilisent. Deux variables sémantiquement proches mais
    pointant vers des CodeLists différentes signalent un doublon potentiel.

    Args:
        codelists: Liste complète de CodeList avec signatures calculées.
        var_records: Liste d'enregistrements (VariableRef enrichi).
        threshold: Seuil de similitude cosinus. Par défaut _SIM_VAR_THRESHOLD.
        verbose: Si True, affiche les détails des appels API.

    Returns:
        Liste de paires candidates (score ≥ threshold) triée par score.
    """
    threshold = threshold or _SIM_VAR_THRESHOLD
    cl_index = {cl.id: cl for cl in codelists}

    # Filtrer les variables sans cl_id valide
    valid: list[_VarRecord] = [r for r in var_records if r.cl_id in cl_index]

    if len(valid) < 2:
        logger.info("Semantic var: < 2 variable records, skip.")
        return []

    # Embeddings
    texts = [r.text[:_TRUNCATE_VAR] for r in valid]
    client = _get_openai_client()
    vecs = _embed_texts(client, texts, verbose=verbose)

    sims = vecs @ vecs.T
    pairs: list[_CandidatePair] = []
    # Track best per (cl_a_id, cl_b_id) canonical pair
    best_score: dict[tuple[str, str], float] = {}

    n = len(valid)
    for i, j in combinations(range(n), 2):
        score = float(sims[i, j])
        if score < threshold:
            continue

        ra, rb = valid[i], valid[j]
        if ra.cl_id == rb.cl_id:
            continue  # même CodeList → pas d'intérêt

        # Canonical key (ordre stable)
        key = (min(ra.cl_id, rb.cl_id), max(ra.cl_id, rb.cl_id))
        if score > best_score.get(key, 0.0):
            best_score[key] = score

    # Convert best scores to _CandidatePair
    for (id_a, id_b), score in sorted(best_score.items(), key=lambda x: x[1], reverse=True):
        if id_a in cl_index and id_b in cl_index:
            pairs.append(
                _CandidatePair(
                    cl_a=cl_index[id_a],
                    cl_b=cl_index[id_b],
                    score=round(score, 4),
                    phase="variable",
                )
            )

    logger.info(
        "Semantic var: %d candidate CL pairs from %d variable pairs >= %.2f",
        len(pairs),
        len(best_score),
        threshold,
    )
    return pairs


# ================================================================
# 4. Juge LLM
# ================================================================


_JUDGE_SYS_PROMPT: str = (
    "Deux listes de codes décrivent-elles le MÊME concept métier ? "
    'Réponds STRICTEMENT en JSON {"meme_concept": bool, "confiance": 0..1, "raison": "..."}.'
)


@dataclass
class _JudgeResult:
    """Résultat du juge LLM."""

    meme_concept: bool
    confiance: float
    raison: str


def llm_judge(
    client: OpenAI,
    cl_a: CodeList,
    cl_b: CodeList,
    *,
    model: str | None = None,
    max_tokens: int = 400,
    verbose: bool = False,
) -> _JudgeResult:
    """
    Juge LLM : vérifie si deux CodeLists décrivent le même concept.

    Args:
        client: Client OpenAI.
        cl_a: Première CodeList.
        cl_b: Deuxième CodeList.
        model: Modèle LLM à utiliser. Par défaut _JUDGE_MODEL.
        max_tokens: Token max de réponse.
        verbose: Si True, affiche les détails de l'appel (prompt, réponse, timing).

    Returns:
        _JudgeResult avec meme_concept (bool), confiance (0-1), raison (str).
    """
    model = model or _JUDGE_MODEL

    prompt = (
        f"CodeList A ({cl_a.name or cl_a.id[:12]}): "
        f"{cl_a.label}\n"
        f"Codes: {cl_a.codes[:10]}\n\n"
        f"CodeList B ({cl_b.name or cl_b.id[:12]}): "
        f"{cl_b.label}\n"
        f"Codes: {cl_b.codes[:10]}"
    )

    if verbose:
        cl_name_a = cl_a.name or cl_a.id[:12]
        cl_name_b = cl_b.name or cl_b.id[:12]
        print(f"\n  [LLM judge] {cl_name_a} ({cl_a.label}) ↔ {cl_name_b} ({cl_b.label})")
        print(f"    modèle={model}")
        display_prompt = prompt[:1000] + "..." if len(prompt) > 1000 else prompt
        print(f"    prompt={display_prompt}")

    try:
        t0 = time.perf_counter()
        out = (
            client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _JUDGE_SYS_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=max_tokens,
            )
            .choices[0]
            .message.content
            or ""
        )
        elapsed = time.perf_counter() - t0
        if verbose:
            print(f"    temps={elapsed:.2f}s")
            print(f"    réponse brute={out!r}")
    except Exception as exc:
        if verbose:
            print(f"    ERREUR: {exc}")
        logger.warning("LLM judge failed for %s <-> %s: %s", cl_a.id, cl_b.id, exc)
        return _JudgeResult(meme_concept=False, confiance=0.0, raison=f"Erreur: {exc}")

    # Parse JSON - chercher le premier objet JSON dans la réponse
    m = re.search(r"\{.*\}", out, re.DOTALL)
    if m:
        import json

        try:
            data = json.loads(m.group(0))
            result = _JudgeResult(
                meme_concept=bool(data.get("meme_concept", False)),
                confiance=float(data.get("confiance", 0.0)),
                raison=str(data.get("raison", "")),
            )
            if verbose:
                print(
                    f"    parse OK → meme_concept={result.meme_concept}, "
                    f"confiance={result.confiance:.2f}, raison={result.raison}"
                )
            return result
        except (ValueError, TypeError, KeyError):
            if verbose:
                print(
                    f"    parse ÉCHEC — JSON non conforme, fallback."
                )
            pass

    # Fallback : ne pas conclure
    if verbose:
        print(f"    parse ÉCHEC — fallback")
    return _JudgeResult(meme_concept=False, confiance=0.0, raison="Réponse non parseable")


# ================================================================
# 5. Utilitaires de haut niveau
# ================================================================


def run_semantic_detection(
    codelists: list[CodeList],
    var_records: list[_VarRecord] | None = None,
    *,
    run_llm: bool = True,
    cl_threshold: float | None = None,
    var_threshold: float | None = None,
    max_llm_pairs: int = 20,
    verbose: bool = False,
) -> tuple[list[_CandidatePair], list[_JudgeResult]]:
    """
    Pipeline sémantique complet : phases 1 + 2 + juge LLM.

    Args:
        codelists: Liste complète de CodeList.
        var_records: Enregistrements de variables (optionnel, requis pour phase 2).
        run_llm: Si True, exécute le juge LLM sur les meilleures paires.
        cl_threshold: Seuil pour la phase directe.
        var_threshold: Seuil pour la phase inverse.
        max_llm_pairs: Nombre maximal de paires à soumettre au juge LLM.
        verbose: Si True, affiche les détails des appels LLM/embeddings.

    Returns:
        Tuple (pairs, judge_results).
    """
    client = _get_openai_client()
    all_pairs: list[_CandidatePair] = []

    # Phase 1 - embeddings directs
    pairs_cl = detect_semantic_codelists(codelists, threshold=cl_threshold, verbose=verbose)
    all_pairs.extend(pairs_cl)

    # Phase 2 - embeddings via variables
    if var_records:
        pairs_var = detect_semantic_via_variables(
            codelists,
            var_records,
            threshold=var_threshold,
            verbose=verbose,
        )
        all_pairs.extend(pairs_var)

    if not all_pairs:
        return [], []

    judge_results: list[_JudgeResult] = []

    if run_llm:
        pairs_sorted = sorted(all_pairs, key=lambda p: p.score, reverse=True)
        for pair in pairs_sorted[:max_llm_pairs]:
            result = llm_judge(client, pair.cl_a, pair.cl_b, verbose=verbose)
            judge_results.append(result)

    return all_pairs, judge_results


def pairs_to_candidates(
    pairs: list[_CandidatePair],
    judge_results: list[_JudgeResult] | None = None,
) -> list[CandidateFusion]:
    """
    Convertit la liste de paires candidates en objets CandidateFusion.

    Args:
        pairs: Paires issues de detect_semantic_codelists ou detect_semantic_via_variables.
        judge_results: Resultats du juge LLM (optionnel). Si fourni, doivent être
                       alignés avec :len(pairs): (same order).

    Returns:
        Liste de CandidateFusion avec type "semantic_list" ou "semantic_var".
    """
    results: list[CandidateFusion] = []
    judge_map: dict[tuple[str, str], _JudgeResult] = {}

    if judge_results:
        # On associe les résultats par ordre - chaque paire a un index dans l'ordre d'origine
        for i, p in enumerate(pairs):
            key = (p.cl_a.id, p.cl_b.id)
            if i < len(judge_results):
                judge_map[key] = judge_results[i]

    for i, pair in enumerate(pairs):
        det_type = "semantic_list" if pair.phase == "direct" else "semantic_var"
        key = (pair.cl_a.id, pair.cl_b.id)

        judge = judge_map.get(key)
        if judge:
            confidence = judge.confiance
            evidence: dict[str, Any] = {
                "cosine_score": pair.score,
                "judge_meme_concept": judge.meme_concept,
                "judge_raison": judge.raison,
                "phase": pair.phase,
            }
        else:
            confidence = pair.score
            evidence = {"cosine_score": pair.score, "phase": pair.phase}

        results.append(
            CandidateFusion(
                fusion_id=f"semantic-{det_type[0]}-{pair.cl_a.id[:8]}-{pair.cl_b.id[:8]}",
                detection_type=det_type,
                master_cl=pair.cl_a,
                slave_cls=[pair.cl_b],
                confidence=round(confidence, 4),
                evidence=evidence,
            )
        )

    return results
