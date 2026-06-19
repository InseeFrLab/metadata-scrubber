"""Matcher phase 1 : égalité exacte sur ``sig_pairs``.

Documente la stratégie ; la fusion effective est faite dans
``Registry.fold_in`` pour rester dans une seule transaction par opération.
"""

from __future__ import annotations

from ..model import CodeListRecord
from ..registry import Registry, canonical_id_for
from . import MatchDecision


class ExactMatcher:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry

    def find_candidates(self, record: CodeListRecord) -> list[str]:
        if self.registry._canonical_exists(record.sig_pairs):
            return [canonical_id_for(record.sig_pairs)]
        return []

    def decide(self, record: CodeListRecord, candidate_id: str) -> MatchDecision:
        return MatchDecision(
            canonical_id=candidate_id,
            confidence=1.0,
            rationale="Ensemble (valeur, libellé) identique.",
        )
