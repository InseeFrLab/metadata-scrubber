"""Matcher phase 2 — rapprochement sémantique.

L'implémentation effective vit dans ``codelist_dedup.semantic`` (pipeline
embeddings → candidats par cosinus → juge LLM, exécuté en passe séparée sur le
registre cumulé via ``codelist-dedup semantic``). Ce module conserve l'interface
``Matcher`` pour homogénéité avec ``matchers/exact.py``.
"""

from __future__ import annotations

from ..model import CodeListRecord
from . import MatchDecision


class SemanticMatcher:
    """Adaptateur fin au-dessus de ``codelist_dedup.semantic``.

    La phase 2 fonctionne en lot (embeddings de tout le registre, recherche de
    voisins, juge LLM borné) plutôt qu'enregistrement par enregistrement ; voir
    ``semantic.run_semantic``. Cette classe expose seulement le protocole.
    """

    def __init__(self, registry: object) -> None:
        self.registry = registry

    def find_candidates(self, record: CodeListRecord) -> list[str]:  # pragma: no cover
        raise NotImplementedError(
            "La phase 2 s'exécute en lot : utiliser `codelist-dedup semantic`."
        )

    def decide(self, record: CodeListRecord, candidate_id: str) -> MatchDecision:  # pragma: no cover
        raise NotImplementedError(
            "La phase 2 s'exécute en lot : utiliser `codelist-dedup semantic`."
        )
