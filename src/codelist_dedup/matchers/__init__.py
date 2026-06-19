"""Stratégies de rapprochement (le « joint » phase 1 / phase 2).

Phase 1 : ``ExactMatcher`` — la fusion exacte est réalisée directement dans
``Registry.fold_in`` (lookup sur ``sig_pairs``). Ce protocole formalise le point
d'extension pour la phase 2 (``SemanticMatcher`` : embeddings + LLM).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..model import CodeListRecord


@dataclass
class MatchDecision:
    canonical_id: str | None  # None = aucun rapprochement
    confidence: float
    rationale: str


class Matcher(Protocol):
    def find_candidates(self, record: CodeListRecord) -> list[str]:
        """Identifiants canoniques candidats pour ``record``."""
        ...

    def decide(self, record: CodeListRecord, candidate_id: str) -> MatchDecision:
        """Tranche si ``record`` correspond au candidat."""
        ...
