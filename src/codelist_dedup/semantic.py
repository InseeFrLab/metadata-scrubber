"""Phase 2 — rapprochement sémantique : embeddings + juge LLM.

Pipeline (sur le registre cumulé, après ingestion) :

1. **Embeddings** : chaque canon (nom + ``valeur: libellé``) est vectorisé via
   l'endpoint OpenAI-compatible (modèle ``qwen3-embedding-8b``). Mis en cache
   dans la table ``embedding`` → idempotent.
2. **Candidats** : voisins les plus proches par **cosinus** (≥ ``min_cosine``,
   top-k), à bas coût — capte les synonymes que la phase 1 (exact/lexical) rate.
3. **Juge LLM** : sur les meilleurs candidats seulement, tranche « même concept ? »
   → confiance + justification. Mis en cache dans ``semantic_verdict``, borné par
   ``max_judgements``. Juge par défaut ``gemma4-26b-moe`` (rapide, ~0,6 s/appel) ;
   ``qwen3-6-35b-moe`` (modèle « thinking », ~20 s/appel) disponible via
   ``--chat-model`` pour un arbitrage plus fin.

Phase 2 **propose** uniquement : rien n'est fusionné.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

import numpy as np
from tqdm import tqdm

from .registry import Registry

EMBED_MODEL = os.environ.get("CLD_EMBED_MODEL", "qwen3-embedding-8b")
# Juge par défaut : gemma4 (~30× plus rapide que qwen3, JSON propre, non « thinking »).
# qwen3-6-35b-moe reste dispo via --chat-model pour un arbitrage plus fin (mais lent).
CHAT_MODEL = os.environ.get("CLD_JUDGE_MODEL", "gemma4-26b-moe")

_EMBED_MAX_CHARS = 8000
_JUDGE_SAMPLE = 40  # paires (valeur, libellé) montrées au LLM par liste


def get_client():
    from openai import OpenAI

    return OpenAI(
        base_url=os.environ["OPENAI_BASE_URL"],
        api_key=os.environ["OPENAI_API_KEY"],
    )


def embedding_text(
    name: str, label: str, pairs: list[tuple[str, str | None]]
) -> str:
    lines = [x for x in (name, label) if x]
    for value, code_label in pairs:
        lines.append(f"{value}: {code_label or ''}")
    return "\n".join(lines)[:_EMBED_MAX_CHARS]


# ----------------------------------------------------------------- embeddings
def embed_missing(reg: Registry, client, model: str, batch: int = 64) -> int:
    """Vectorise les canons sans embedding (pour ``model``). Retourne le nombre calculé."""
    todo = reg.missing_embedding_ids(model)
    with tqdm(total=len(todo), desc="Embeddings", unit="canon", disable=not todo) as bar:
        for start in range(0, len(todo), batch):
            chunk = todo[start : start + batch]
            texts = []
            for cid in chunk:
                name, label, pairs = reg.canonical_content(cid)
                texts.append(embedding_text(name, label, pairs))
            resp = client.embeddings.create(model=model, input=texts)
            for cid, item in zip(chunk, resp.data):
                vec = np.asarray(item.embedding, dtype=np.float32)
                reg.store_embedding(cid, model, vec.shape[0], vec.tobytes())
            bar.update(len(chunk))
    return len(todo)


# ------------------------------------------------------------------ candidats
@dataclass
class Candidate:
    canonical_a: str
    canonical_b: str
    cosine: float


def candidate_pairs(
    reg: Registry, model: str, min_cosine: float = 0.85, top_k: int = 5
) -> list[Candidate]:
    """Paires de canons proches par cosinus (≥ min_cosine, top-k voisins par canon)."""
    rows = reg.load_embeddings(model)
    if len(rows) < 2:
        return []
    ids = [r[0] for r in rows]
    mat = np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat = mat / norms
    sims = mat @ mat.T  # cosinus (vecteurs normés)

    seen: set[tuple[str, str]] = set()
    out: list[Candidate] = []
    for i in range(len(ids)):
        order = np.argsort(-sims[i])
        kept = 0
        for j in order:
            if j == i:
                continue
            c = float(sims[i, j])
            if c < min_cosine or kept >= top_k:
                break
            a, b = sorted((ids[i], ids[j]))
            if (a, b) not in seen:
                seen.add((a, b))
                out.append(Candidate(a, b, c))
            kept += 1
    out.sort(key=lambda x: x.cosine, reverse=True)
    return out


# --------------------------------------------------------------------- juge
_JUDGE_SYS = (
    "Tu es un expert des métadonnées statistiques (listes de codes DDI). On te "
    "donne deux listes de codes (nom + paires valeur:libellé). Détermine si elles "
    "décrivent le MÊME concept / la même nomenclature (au-delà des différences de "
    "codage ou de formulation). Réponds STRICTEMENT par un objet JSON :\n"
    '{"meme_concept": true|false, "confiance": 0.0-1.0, "raison": "courte explication"}'
)


def _format_list(name: str, label: str, pairs: list[tuple[str, str | None]]) -> str:
    sample = pairs[:_JUDGE_SAMPLE]
    body = "\n".join(f"  {v}: {l or ''}" for v, l in sample)
    more = "" if len(pairs) <= _JUDGE_SAMPLE else f"\n  … (+{len(pairs) - _JUDGE_SAMPLE} codes)"
    header = f"Nom: {name}"
    if label:
        header += f"\nLibellé: {label}"
    return f"{header}\n{body}{more}"


def _parse_verdict(content: str | None) -> tuple[bool | None, float | None, str | None]:
    if not content:
        return None, None, "réponse vide ou tronquée"
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if not m:
        return None, None, "JSON introuvable"
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None, None, "JSON invalide"
    return (
        bool(d.get("meme_concept")) if "meme_concept" in d else None,
        d.get("confiance"),
        d.get("raison"),
    )


def judge_pairs(
    reg: Registry, client, chat_model: str, candidates: list[Candidate],
    max_judgements: int = 50,
) -> int:
    """Juge les candidats non encore évalués (borné). Retourne le nombre d'appels LLM."""
    pending = [
        c
        for c in candidates
        if not reg.has_verdict(c.canonical_a, c.canonical_b, chat_model)
    ]
    to_judge = pending[:max_judgements]
    skipped = len(pending) - len(to_judge)

    for cand in tqdm(to_judge, desc="Juge LLM", unit="paire", disable=not to_judge):
        name_a, label_a, pairs_a = reg.canonical_content(cand.canonical_a)
        name_b, label_b, pairs_b = reg.canonical_content(cand.canonical_b)
        prompt = (
            f"Liste A:\n{_format_list(name_a, label_a, pairs_a)}\n\n"
            f"Liste B:\n{_format_list(name_b, label_b, pairs_b)}"
        )
        try:
            resp = client.chat.completions.create(
                model=chat_model,
                messages=[
                    {"role": "system", "content": _JUDGE_SYS},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=2000,
                temperature=0,
            )
            same, conf, reason = _parse_verdict(resp.choices[0].message.content)
        except Exception as exc:  # robustesse : on note l'échec, on continue
            same, conf, reason = None, None, f"erreur LLM: {exc}"
        reg.store_verdict(
            cand.canonical_a, cand.canonical_b, chat_model, cand.cosine,
            same, conf, reason,
        )
    if skipped:
        print(
            f"  ⚠️ {skipped} candidats non jugés (plafond --max-judgements={max_judgements})",
            file=sys.stderr,
        )
    return len(to_judge)


# ------------------------------------------------------------- orchestration
def run_semantic(
    reg: Registry, *, embed_model: str = EMBED_MODEL, chat_model: str = CHAT_MODEL,
    min_cosine: float = 0.85, top_k: int = 5, max_judgements: int = 50,
    use_llm: bool = True,
) -> list[Candidate]:
    client = get_client()
    n_emb = embed_missing(reg, client, embed_model)
    print(f"Embeddings : {n_emb} nouveaux calculés (modèle {embed_model}).", file=sys.stderr)
    candidates = candidate_pairs(reg, embed_model, min_cosine=min_cosine, top_k=top_k)
    print(f"Candidats (cosinus ≥ {min_cosine}) : {len(candidates)}.", file=sys.stderr)
    if use_llm and candidates:
        n = judge_pairs(reg, client, chat_model, candidates, max_judgements=max_judgements)
        print(f"Juge LLM : {n} paires évaluées (modèle {chat_model}).", file=sys.stderr)
    return candidates
