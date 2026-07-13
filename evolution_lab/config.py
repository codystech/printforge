"""Feature flags and isolated storage configuration for the Training Lab."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EvolutionLabConfig:
    """All experimental defaults are intentionally off.

    ``data_root`` is separate from ``library/`` and ``uploads/``.  Merely importing
    this module creates no directories and changes no application behavior.
    """

    evolution_enabled: bool = False
    training_lab_enabled: bool = False
    memory_learning_enabled: bool = False
    physical_feedback_enabled: bool = False
    actual_training_enabled: bool = False
    cadquery_enabled: bool = False
    bambu_slicer_enabled: bool = False
    training_enabled: bool = False
    lab_only: bool = False
    training_backend: str = ""
    training_dataset: str = ""
    base_model: str = ""
    trained_model_path: str = ""
    trained_model_version: str = ""
    trained_model_approved: bool = False
    bambu_binary: str = ""
    bambu_version: str = ""
    bambu_sha256: str = ""
    bwrap_binary: str = ""
    bwrap_version: str = ""
    bwrap_sha256: str = ""
    bambu_machine_profile: str = ""
    bambu_process_profile: str = ""
    bambu_filament_profile: str = ""
    bambu_smoke_evidence: str = ""
    data_root: Path = Path(__file__).resolve().parent.parent / "training_lab_data"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EvolutionLabConfig":
        e = os.environ if env is None else env
        default_root = Path(__file__).resolve().parent.parent / "training_lab_data"
        return cls(
            evolution_enabled=_flag(e, "PRINT_FORGE_EVOLUTION_ENABLED"),
            training_lab_enabled=_flag(e, "PRINT_FORGE_TRAINING_LAB_ENABLED"),
            memory_learning_enabled=_flag(e, "PRINT_FORGE_MEMORY_LEARNING_ENABLED"),
            physical_feedback_enabled=_flag(e, "PRINT_FORGE_PHYSICAL_FEEDBACK_ENABLED"),
            actual_training_enabled=_flag(e, "PRINT_FORGE_ACTUAL_TRAINING_ENABLED"),
            cadquery_enabled=_flag(e, "PRINT_FORGE_CADQUERY_ENABLED"),
            bambu_slicer_enabled=_flag(e, "PRINT_FORGE_BAMBU_SLICER_ENABLED"),
            training_enabled=_flag(e, "PRINT_FORGE_TRAINING_ENABLED"),
            lab_only=_flag(e, "PRINT_FORGE_LAB_ONLY"),
            training_backend=e.get("PRINT_FORGE_TRAINING_BACKEND", "").strip(),
            training_dataset=e.get("PRINT_FORGE_TRAINING_DATASET", "").strip(),
            base_model=e.get("PRINT_FORGE_BASE_MODEL", "").strip(),
            trained_model_path=e.get("PRINT_FORGE_TRAINED_MODEL_PATH", "").strip(),
            trained_model_version=e.get("PRINT_FORGE_TRAINED_MODEL_VERSION", "").strip(),
            trained_model_approved=_flag(e, "PRINT_FORGE_TRAINED_MODEL_APPROVED"),
            bambu_binary=e.get("PRINT_FORGE_BAMBU_BINARY", "").strip(),
            bambu_version=e.get("PRINT_FORGE_BAMBU_VERSION", "").strip(),
            bambu_sha256=e.get("PRINT_FORGE_BAMBU_SHA256", "").strip(),
            bwrap_binary=e.get("PRINT_FORGE_BWRAP_BINARY", "").strip(),
            bwrap_version=e.get("PRINT_FORGE_BWRAP_VERSION", "").strip(),
            bwrap_sha256=e.get("PRINT_FORGE_BWRAP_SHA256", "").strip(),
            bambu_machine_profile=e.get("PRINT_FORGE_BAMBU_MACHINE_PROFILE", "").strip(),
            bambu_process_profile=e.get("PRINT_FORGE_BAMBU_PROCESS_PROFILE", "").strip(),
            bambu_filament_profile=e.get("PRINT_FORGE_BAMBU_FILAMENT_PROFILE", "").strip(),
            bambu_smoke_evidence=e.get("PRINT_FORGE_BAMBU_SMOKE_EVIDENCE", "").strip(),
            data_root=Path(e.get("PRINT_FORGE_TRAINING_LAB_DATA_ROOT", str(default_root))).expanduser(),
        )

    def public_dict(self) -> dict:
        """Return only non-secret, browser-safe capability information."""

        return {
            "evolution_enabled": self.evolution_enabled,
            "training_lab_enabled": self.training_lab_enabled,
            "memory_learning_enabled": self.memory_learning_enabled,
            "physical_feedback_enabled": self.physical_feedback_enabled,
            "actual_training_enabled": self.actual_training_enabled,
            "cadquery_enabled": self.cadquery_enabled,
            "bambu_slicer_enabled": self.bambu_slicer_enabled,
            "bambu_slicer_identity_configured": bool(
                self.bambu_binary and self.bambu_version and self.bambu_sha256
                and self.bwrap_binary and self.bwrap_version and self.bwrap_sha256
            ),
            "bambu_slicer_profiles_configured": bool(
                self.bambu_machine_profile and self.bambu_process_profile
                and self.bambu_filament_profile
            ),
            "bambu_slicer_smoke_configured": bool(self.bambu_smoke_evidence),
            "training_enabled": self.training_enabled,
            "lab_only": self.lab_only,
            "training_backend_configured": bool(self.training_backend),
            "training_dataset_configured": bool(self.training_dataset),
            "base_model_configured": bool(self.base_model),
            "trained_model_configured": bool(self.trained_model_path),
            "trained_model_version": self.trained_model_version or None,
            "trained_model_approved": self.trained_model_approved,
        }
