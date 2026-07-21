"""models.py — Pydantic schemas pour les requêtes et réponses API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ============================================================================
# Pipeline
# ============================================================================

class PipelineRequest(BaseModel):
    """Requête pour lancer un pipeline."""
    xml_source: str = Field(..., description="Chemin ou URL S3 du fichier DDI.")
    output_base: str = Field(
        "s3://projet-metadonnees-rmes/scrubber_output",
        description="Répertoire de sortie (S3 ou local).",
    )
    run_llm: bool = Field(True, description="Inclure les phases sémantiques (LLM).")
    verbose: bool = Field(False, description="Mode verbeux.")
    registry_path: Optional[str] = Field(
        None, description="Registre nettoyé (cleaned_codelists.json) à injecter dans la détection."
    )


class AddToCleanedRequest(BaseModel):
    """Requête d'ajout manuel d'une CodeList au registre nettoyé."""
    cl_id: str = Field(..., description="Id de la CodeList (parent du registre des doublons).")


class PipelineResponse(BaseModel):
    """Réponse lors du lancement d'un pipeline."""
    job_id: str
    status: str = "pending"
    message: str = "Pipeline lancé avec succès."


class PipelineStatus(BaseModel):
    """État d'un pipeline."""
    job_id: str
    status: str  # "pending" | "running" | "success" | "error"
    progress: float  # 0.0 à 1.0
    phase: Optional[int] = None
    phase_label: Optional[str] = None
    current_log: Optional[str] = None
    logs: list[str] = Field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    error_message: Optional[str] = None
    duration_seconds: Optional[float] = None


# ============================================================================
# Registry / Validation
# ============================================================================

class DecisionUpdate(BaseModel):
    """Mise à jour de la decision d'un duplicate."""
    cl_id: str
    dup_id: str
    decision: str = Field(..., pattern="^(approve|reject|pending)$")


class BulkDecision(BaseModel):
    """Action globale sur les decisions."""
    action: str = Field(..., pattern="^(approve|reject|pending)$")
    criteria: str = Field(..., pattern="^(exact|high-confidence|all)$")


class RegistryStats(BaseModel):
    """Statistiques globales du registre."""
    total_code_lists: int
    total_duplicates: int
    approved: int
    rejected: int
    pending: int
    by_detection_type: dict[str, int]


class CodeListResult(BaseModel):
    """une CodeList avec ses duplicates."""
    id: str
    name: str
    label: Optional[str] = None
    codes_count: int = 0
    vars_count: int = 0
    vars: list[str] = Field(default_factory=list)
    cat_ids_count: int = 0
    duplicates_count: int = 0
    duplicates: list[dict[str, Any]] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)


class CodelistFilter(BaseModel):
    """Paramètres de filtrage pour les CodeLists."""
    decision_filter: list[str] = ["approve", "reject", "pending"]
    search: Optional[str] = None
    sort_by: str = "duplicates_count"  # "duplicates_count" | "name"
    page: int = 0
    page_size: int = 50


class FilteredResult(BaseModel):
    """Résultat de filtrage des CodeLists."""
    items: list[CodeListResult]
    total: int
    page: int
    page_size: int
    total_pages: int
