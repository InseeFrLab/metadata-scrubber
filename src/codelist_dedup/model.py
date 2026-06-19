"""Structures de données du pipeline de dédoublonnage des listes de codes."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CodeEntry:
    """Un code d'une liste : sa valeur + le libellé résolu de sa catégorie."""

    value: str
    category_id: str | None
    label: str | None = None  # résolu après la passe d'extraction

    @property
    def resolved(self) -> bool:
        return self.label is not None


@dataclass
class CodeListRecord:
    """Une liste de codes DDI extraite d'un fichier d'opération."""

    ddi_id: str
    urn: str
    version: str
    source_id: str  # r:UserID colectica:sourceId (identifiant lisible)
    name: str  # CodeListName (code technique, ex. L_DEP)
    label: str  # r:Label (libellé humain, ex. « Liste de codes DEPARTEMENTS »)
    description: str
    codes: list[CodeEntry] = field(default_factory=list)

    # signatures (remplies par signature.py)
    sig_pairs: str = ""
    sig_values: str = ""
    sig_name: str = ""

    @property
    def n_pairs(self) -> int:
        return len(self.codes)

    @property
    def n_unresolved(self) -> int:
        return sum(1 for c in self.codes if not c.resolved)

    @property
    def is_empty(self) -> bool:
        return len(self.codes) == 0
