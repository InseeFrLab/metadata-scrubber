"""metadata-scrubber — dédoublonnage des listes de codes DDI."""

from __future__ import annotations

from .extractor import extract_codelists, extract_variables, full_extract
from .funnel import detect_exact_duplicates, detect_fuzzy_duplicates
from .normalize import concat_text, normalize, signature_from_codes
from .semantic import (
    _VarRecord,
    detect_semantic_codelists,
    detect_semantic_via_variables,
    llm_judge,
    pairs_to_candidates,
    run_semantic_detection,
)
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
