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
    run_id: str | None = Field(default=None, max_length=64)
    candidate_id: str = Field(min_length=1, max_length=64)
    printed_successfully: bool
    printer_profile: dict[str, Any]
    material: str = Field(min_length=1, max_length=120)
    nozzle: float = Field(gt=0, le=10)
    layer_height: float = Field(gt=0, le=10)
    slicer_profile: str = Field(default="", max_length=240)
    results: dict[str, Any] = Field(default_factory=dict)
    measurements: dict[str, float] = Field(default_factory=dict)
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
    dataset_type: str = Field(pattern=r"^(preference|repair|calibration|supervised|failure|all)$")
    format: str = Field(default="jsonl", pattern=r"^(json|jsonl|csv|zip)$")
    run_id: str | None = Field(default=None, max_length=64)
