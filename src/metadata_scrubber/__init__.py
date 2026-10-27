"""metadata-scrubber — dédoublonnage des listes de codes DDI."""

from __future__ import annotations

from .extractor import extract_codelists, extract_variables, full_extract
from .funnel import detect_exact_duplicates, detect_fuzzy_duplicates
from .normalize import concat_text, normalize, signature_from_codes
from .signals import compute_usage_signatures, find_usage_groups
from .types import (
    CandidateFusion,
    CodeList,
    ExtractionResult,
    VariableRef,
)

__all__ = [
    "detect_semantic_codelists",
    "detect_semantic_via_variables",
    "llm_judge",
    "pairs_to_candidates",
    "run_semantic_detection",
    "_VarRecord",
    "extract_codelists",
    "extract_variables",
    "full_extract",
    "detect_exact_duplicates",
    "detect_fuzzy_duplicates",
    "compute_usage_signatures",
    "find_usage_groups",
    "concat_text",
    "normalize",
    "signature_from_codes",
    "CandidateFusion",
    "CodeList",
    "ExtractionResult",
    "VariableRef",
]

# La détection sémantique dépend d'openai et numpy, absents de l'image légère du
# service de contrôle (scrubber-check) : import à la première utilisation.
_SEMANTIC_EXPORTS = frozenset({
    "_VarRecord",
    "detect_semantic_codelists",
    "detect_semantic_via_variables",
    "llm_judge",
    "pairs_to_candidates",
    "run_semantic_detection",
})


def __getattr__(name: str):
    if name in _SEMANTIC_EXPORTS:
        from . import semantic

        return getattr(semantic, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
