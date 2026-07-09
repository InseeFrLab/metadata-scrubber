"""Typage partagé du schéma de données DDI et des candidats de fusion."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CodeList:
    """Représentation d'une CodeList DDI avec ses codes résolus."""
    id: str
    name: str
    label: str
    codes: list[tuple[str, str]] = field(default_factory=list)  # (valeur, libellé)
    sig: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # signature de contenu
    vars: list[str] = field(default_factory=list)               # noms de variables référentes
    var_ids: set[str] = field(default_factory=set)              # IDs des variables référentes
    cat_ids: set[str] = field(default_factory=set)              # IDs des catégories présentes
    var_sig: tuple[str, ...] = field(default_factory=tuple)     # signature d'usage (trié)
    origin: str = "xml"                                         # "xml" ou "registry"


@dataclass
class VariableRef:
    """Variable ou RepresentedVariable pointant vers une CodeList."""
    id: str
    name: str
    label: str
    cl_id: str | None = None  # ID de la CodeList référencée (peut être None)


@dataclass
class CandidateFusion:
    """Candidat de fusion entre une CodeList master et plusieurs slaves."""
    fusion_id: str
    detection_type: str  # "exact", "fuzzy", "semantic_list", "semantic_var", "usage"
    master_cl: CodeList
    slave_cls: list[CodeList]
    confidence: float  # 0.0 – 1.0
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def all_ids(self) -> list[str]:
        return [self.master_cl.id] + [c.id for c in self.slave_cls]

    @property
    def shared_vars(self) -> set[str]:
        """Variables communes à toutes les listes du groupe."""
        if not self.slave_cls:
            return set(self.master_cl.vars)
        sets = [set(cl.vars) for cl in [self.master_cl] + self.slave_cls]
        return set.intersection(*sets) if sets else set()


@dataclass
class ExtractionResult:
    """Résultat complet de l'extraction et de la détection."""
    codelists: list[CodeList]
    variables: list[VariableRef]
    candidates: list[CandidateFusion]
    stats: dict[str, int] = field(default_factory=dict)
