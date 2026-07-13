"""Pydantic request schemas and shared Training Lab constants."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class EvidenceLabel(str, Enum):
    MEASURED = "MEASURED"
    SLICED = "SLICED"
    AI_JUDGED = "AI-JUDGED"
    USER_RATED = "USER-RATED"
    PHYSICALLY_VERIFIED = "PHYSICALLY VERIFIED"
    UNVERIFIED = "UNVERIFIED"


class ScoreCategory(str, Enum):
    PRINTABILITY = "printability"
    FUNCTION = "function"
    ADHERENCE = "prompt_spec_adherence"
    STRUCTURAL = "structural_quality"
    EXPERIENCE = "user_experience_ergonomics"
    SIMPLICITY = "simplicity_efficiency"


class RunMode(str, Enum):
    EVOLVE_EXISTING = "evolve_existing"
    CREATE_FROM_SPEC = "create_from_spec"


class TrainingProvenanceInput(BaseModel):
    """Explicit provenance fields accepted by the dataset-v2 consent gate."""

    status: str = Field(
        default="unknown", pattern=r"^(unknown|self-created|verified|licensed|rejected)$"
    )
    source: str = Field(default="", max_length=500)
    source_revision: str = Field(default="", max_length=500)
    license: str = Field(default="", max_length=200)
    license_rights: str = Field(
        default="not_reviewed",
        pattern=r"^(not_reviewed|owned|licensed_for_training|public_domain)$",
    )


class PhysicalFailureClass(str, Enum):
    ADHESION = "adhesion"
    SUPPORT_FAILURE = "support_failure"
    FUSED_FIT = "fused_fit"
    LOOSE_FIT = "loose_fit"
    DIMENSIONAL_ERROR = "dimensional_error"
    WEAK_FEATURE = "weak_feature"
    WARPING = "warping"
    SURFACE_QUALITY = "surface_quality"
    PRINTER_MATERIAL_FAILURE = "printer_material_failure"
    OTHER = "other"


class ModelFormat(str, Enum):
    """Model source formats understood by the additive Training Lab API."""

    OPENSCAD_LEGACY = "openscad-legacy"
    CADQUERY_V1 = "cadquery-v1"


class ModelPart(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    export_role: str = Field(pattern=r"^(printable|assembly|reference|fit_cutout|negative)$")
    transform: dict[str, list[float]] = Field(default_factory=dict)
    step_artifact: str | None = Field(default=None, max_length=128)
    stl_artifact: str | None = Field(default=None, max_length=128)


class ModelEnvelope(BaseModel):
    """Format-neutral candidate fields; legacy aliases are response-only."""

    model_format: ModelFormat
    source: str | None
    source_available: bool = False
    parameters: dict[str, Any] = Field(default_factory=dict)
    parts: list[ModelPart] = Field(default_factory=list)
    artifact_id: str | None = None


CATEGORY_MAXIMA = {
    ScoreCategory.PRINTABILITY.value: 25.0,
    ScoreCategory.FUNCTION.value: 25.0,
    ScoreCategory.ADHERENCE.value: 20.0,
    ScoreCategory.STRUCTURAL.value: 10.0,
    ScoreCategory.EXPERIENCE.value: 10.0,
    ScoreCategory.SIMPLICITY.value: 10.0,
}


class EvidenceInput(BaseModel):
    category: ScoreCategory
    criterion: str = Field(min_length=1, max_length=160)
    points_awarded: float = Field(default=0, ge=0)
    points_possible: float = Field(default=0, ge=0)
    label: EvidenceLabel
    source: str = Field(default="", max_length=120)
    summary: str = Field(default="", max_length=2000)
    confidence: float = Field(default=0, ge=0, le=1)
    measured_value: Any | None = None
    critical: bool = False


class MutationInput(BaseModel):
    mutation_type: str = Field(min_length=1, max_length=120)
    parameter: str | None = Field(default=None, max_length=120)
    original_value: Any | None = None
    mutated_value: Any | None = None
    expected_benefit: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=1000)


class RunLimits(BaseModel):
    variants_per_generation: int = Field(default=2, ge=2, le=2)
    # ``maximum_generations`` is retained for old clients. New clients use the
    # clearer ``maximum_iterations`` name; the engine normalizes either field.
    maximum_iterations: int | None = Field(default=None, ge=1, le=50)
    maximum_generations: int | None = Field(default=None, ge=1, le=50)
    target_reward_score: float | None = Field(default=None, ge=0, le=100)
    maximum_runtime_seconds: int = Field(default=1200, ge=10, le=604800)
    maximum_estimated_cost: float = Field(default=10.0, ge=0, le=100000)
    maximum_backend_calls: int = Field(default=10, ge=2, le=1000)
    repeated_generation_failure_limit: int = Field(default=3, ge=1, le=20)
    no_improvement_limit: int = Field(default=2, ge=1, le=20)
    mutation_strength: float = Field(default=0.25, ge=0, le=1)
    exploration_rate: float = Field(default=0.15, ge=0, le=1)
    benchmark_mode: bool = False
    physical_validation_required: bool = False
    random_seed: int | None = None


class CreateRunRequest(BaseModel):
    # Defaulting to evolve_existing preserves the old request contract. The
    # engine performs the mode-dependent cross-field validation so this stays
    # compatible with both Pydantic v1 and v2 deployments.
    run_mode: RunMode = RunMode.EVOLVE_EXISTING
    source_model_id: str | None = Field(default=None, max_length=64)
    source_prompt: str = Field(default="", max_length=50000)
    validated_spec: str = Field(min_length=1, max_length=100000)
    printer_profile: dict[str, Any]
    material_profile: dict[str, Any] = Field(default_factory=dict)
    locked_constraints: list[dict[str, Any]] = Field(default_factory=list)
    attached_reference_roles: list[dict[str, Any]] = Field(default_factory=list)
    export_exclusions: list[str] = Field(default_factory=list)
    active_backend: str = Field(default="", max_length=160)
    limits: RunLimits = Field(default_factory=RunLimits)
    initial_mutations: list[MutationInput] = Field(default_factory=list)
    part_family: str = Field(default="", max_length=160)
    training_consent: bool = False
    training_consent_decision: str = Field(
        default="not_reviewed", pattern=r"^(not_reviewed|approved|declined)$"
    )
    training_consent_reviewer: str = Field(default="", max_length=160)
    training_consent_reviewed_at: str | None = Field(default=None, max_length=80)
    provenance_status: str = Field(
        default="unknown", pattern=r"^(unknown|self-created|verified|licensed|rejected)$"
    )
    data_provenance: TrainingProvenanceInput = Field(default_factory=TrainingProvenanceInput)
    auto_start: bool = False


class MemoryRuleInput(BaseModel):
    category: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    scope: dict[str, Any]
    trigger_conditions: str = Field(default="", max_length=2000)
    recommendation: str = Field(min_length=1, max_length=2000)
    notes: str = Field(default="", max_length=4000)


class MemoryObservationInput(BaseModel):
    success: bool
    source_model_id: str | None = Field(default=None, max_length=64)
    source_candidate_id: str | None = Field(default=None, max_length=64)
    physical: bool = False
    major_regression: bool = False
    note: str = Field(default="", max_length=2000)


class MemoryReviewInput(BaseModel):
    action: str = Field(pattern=r"^(approve_for_testing|dispute|deprecate|reject|reset_confidence)$")
    note: str = Field(default="", max_length=2000)


class PhysicalValidationInput(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=64)
    artifact_checksum: str = Field(
        pattern=r"^(?:sha256:)?[0-9a-fA-F]{64}$",
        description="Checksum of the exact candidate artifact that was printed",
    )
    artifact_name: str | None = Field(default=None, max_length=128)
    printed_successfully: bool
    printer_profile: dict[str, Any]
    material: str = Field(min_length=1, max_length=120)
    nozzle: float = Field(gt=0, le=10)
    layer_height: float = Field(gt=0, le=10)
    slicer_profile: str = Field(default="", max_length=240)
    results: dict[str, Any] = Field(default_factory=dict)
    measurements: dict[str, float] = Field(default_factory=dict)
    failure_classes: list[PhysicalFailureClass] = Field(default_factory=list)
    failure_notes: str = Field(default="", max_length=5000)
    photo_name: str | None = Field(default=None, max_length=240)
    user_rating: float | None = Field(default=None, ge=0, le=10)
    recommended_change: str = Field(default="", max_length=2000)


class CalibrationInput(BaseModel):
    calibration_type: str = Field(min_length=1, max_length=120)
    printer_profile: dict[str, Any]
    material: str = Field(min_length=1, max_length=120)
    nozzle: float = Field(gt=0, le=10)
    layer_height: float = Field(gt=0, le=10)
    slicer_profile: str | None = Field(default=None, max_length=240)
    tested_values: list[Any] = Field(default_factory=list)
    physical_results: list[dict[str, Any]] = Field(default_factory=list)
    recommended_value: Any | None = None
    status: str = Field(default="hypothesis", pattern=r"^(hypothesis|provisional|validated|deprecated)$")


class BenchmarkInput(BaseModel):
    benchmark_id: str = Field(min_length=1, max_length=64)
    category: str = Field(min_length=1, max_length=120)
    production_metrics: dict[str, Any] = Field(default_factory=dict)
    evolution_metrics: dict[str, Any] = Field(default_factory=dict)
    critical_regressions: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class ProposalInput(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    proposed_change: str = Field(min_length=1, max_length=10000)
    affected_scope: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    benchmark_results: list[str] = Field(default_factory=list)
    known_risks: list[str] = Field(default_factory=list)
    rollback_plan: str = Field(default="", max_length=10000)
    files_that_would_change: list[str] = Field(default_factory=list)
    tests_completed: list[str] = Field(default_factory=list)
    unresolved_warnings: list[str] = Field(default_factory=list)


class ProposalStatusInput(BaseModel):
    status: str = Field(pattern=r"^(draft|ready_for_review|approved|rejected|manually_merged)$")
    note: str = Field(default="", max_length=2000)


class DatasetExportInput(BaseModel):
    dataset_type: str = Field(
        pattern=r"^(sft|preference|mutation|repair|print_outcome|calibration|supervised|failure|all)$"
    )
    format: str = Field(default="jsonl", pattern=r"^(json|jsonl|csv|zip)$")
    run_id: str | None = Field(default=None, max_length=64)
    schema_version: str = Field(
        default="printforge-training-dataset-v1",
        pattern=r"^printforge-training-dataset-v[12]$",
    )
