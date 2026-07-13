"""Evidence-first reward scoring and regression-safe winner selection."""

from __future__ import annotations

from collections import defaultdict
import math
import re
from typing import Iterable

from .schemas import CATEGORY_MAXIMA, EvidenceInput, EvidenceLabel


HARD_FAILURES = {
    "corrupted_model",
    "generation_failed",
    "severe_non_manifold",
    "broken_hard_lock",
    "reference_export_leakage",
    "required_feature_missing",
    "build_volume_overflow",
    "not_renderable",
    "not_exportable",
    "unsafe_geometry",
    "critical_evidence_unavailable",
    "invalid_brep",
    "step_export_failed",
    "step_roundtrip_failed",
    "stl_tessellation_failed",
    "mesh_validation_failed",
    "slicer_unavailable",
    "slicer_binary_unpinned",
    "slicer_binary_mismatch",
    "slicer_bwrap_untrusted",
    "slice_no_printable_parts",
    "slice_failed",
    "slice_empty",
    "slice_output_oversized",
    "slice_metrics_incomplete",
    "slice_log_empty",
    "slice_assembly_transform_unsupported",
    "cadquery_runtime_unavailable",
}

DETERMINISTIC_LABELS = {
    EvidenceLabel.MEASURED.value,
    EvidenceLabel.SLICED.value,
    EvidenceLabel.PHYSICALLY_VERIFIED.value,
}


def _dump(item: EvidenceInput | dict) -> dict:
    if isinstance(item, dict):
        model = EvidenceInput(**item)
    else:
        model = item
    if hasattr(model, "model_dump"):
        out = model.model_dump(mode="json")
    else:  # pydantic v1 compatibility
        out = model.dict()
    out["category"] = out["category"].value if hasattr(out["category"], "value") else out["category"]
    out["label"] = out["label"].value if hasattr(out["label"], "value") else out["label"]
    return out


def score_candidate(evidence: Iterable[EvidenceInput | dict], failure_codes: Iterable[str] = ()) -> dict:
    """Calculate an explainable 0-100 reward from stored evidence.

    Unverified claims earn zero.  Category maxima cannot be exceeded.  A candidate
    with no measured, sliced or physical evidence is capped below a high score even
    when AI judgments are favorable.
    """

    normalized = [_dump(item) for item in evidence]
    categories: dict[str, dict] = {}
    grouped: dict[str, list[dict]] = defaultdict(list)
    hard_reasons = [code for code in failure_codes if code in HARD_FAILURES]
    has_deterministic = False
    confidence_weight = 0.0
    confidence_points = 0.0

    for item in normalized:
        category = item["category"]
        grouped[category].append(item)
        possible = min(float(item["points_possible"]), CATEGORY_MAXIMA[category])
        awarded = min(float(item["points_awarded"]), possible)
        if item["label"] == EvidenceLabel.UNVERIFIED.value:
            awarded = 0.0
            if item.get("critical"):
                hard_reasons.append("critical_evidence_unavailable")
        if item["label"] in DETERMINISTIC_LABELS:
            has_deterministic = True
        item["points_possible"] = round(possible, 4)
        item["points_awarded"] = round(awarded, 4)
        confidence_weight += possible
        confidence_points += possible * float(item.get("confidence", 0))

    for name, maximum in CATEGORY_MAXIMA.items():
        items = grouped.get(name, [])
        earned = min(maximum, sum(float(item["points_awarded"]) for item in items))
        assessed = min(maximum, sum(float(item["points_possible"]) for item in items))
        categories[name] = {
            "earned": round(earned, 2),
            "possible": maximum,
            "assessed_points": round(assessed, 2),
            "unassessed_points": round(maximum - assessed, 2),
            "evidence": items,
            "deductions": [
                item["summary"] or item["criterion"]
                for item in items
                if item["points_awarded"] < item["points_possible"]
            ],
            "bonuses": [
                item["summary"] or item["criterion"]
                for item in items
                if item["points_awarded"] > 0
            ],
        }

    raw_total = round(sum(category["earned"] for category in categories.values()), 2)
    evidence_cap_applied = False
    total = raw_total
    if not has_deterministic and total > 74:
        total = 74.0
        evidence_cap_applied = True
    hard_reasons = sorted(set(hard_reasons))
    confidence = confidence_points / confidence_weight if confidence_weight else 0.0
    return {
        "total": round(max(0.0, min(100.0, total)), 2),
        "raw_total": raw_total,
        "categories": categories,
        "confidence": round(confidence, 4),
        "evidence_cap_applied": evidence_cap_applied,
        "hard_rejected": bool(hard_reasons),
        "hard_rejection_reasons": hard_reasons,
        "formula_version": "printforge-evidence-v1",
    }


def _finite_score(value: object) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def deterministic_candidate_eligible(candidate: dict) -> bool:
    """Fail closed before any deterministic or learned ranking is consulted."""

    score = candidate.get("score") if isinstance(candidate.get("score"), dict) else {}
    total = score.get("total")
    finite_total = _finite_score(total)
    cadquery_slice_invalid = False
    if candidate.get("model_format") == "cadquery-v1":
        slicer = candidate.get("slicer_results") if isinstance(candidate.get("slicer_results"), dict) else {}
        artifact_records = {
            item.get("name"): item for item in candidate.get("artifacts") or []
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        }
        sliced_name = slicer.get("sliced_3mf_artifact")
        log_name = slicer.get("log_artifact")
        required_artifacts = [artifact_records.get(sliced_name), artifact_records.get(log_name)]
        cadquery_slice_invalid = not bool(
            str(slicer.get("status") or "").casefold() == "complete"
            and isinstance(slicer.get("estimated_time_seconds"), int)
            and not isinstance(slicer.get("estimated_time_seconds"), bool)
            and slicer["estimated_time_seconds"] > 0
            and isinstance(slicer.get("filament_grams"), (int, float))
            and not isinstance(slicer.get("filament_grams"), bool)
            and float(slicer["filament_grams"]) > 0
            and isinstance(slicer.get("layer_count"), int)
            and not isinstance(slicer.get("layer_count"), bool)
            and slicer["layer_count"] > 0
            and isinstance(slicer.get("support_used"), bool)
            and isinstance(slicer.get("warnings"), list)
            and all(isinstance(item, str) and item for item in slicer["warnings"])
            and isinstance(candidate.get("slicer_profile_fingerprint"), str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", candidate["slicer_profile_fingerprint"])
            and all(
                record and isinstance(record.get("size"), int) and record["size"] > 0
                and isinstance(record.get("sha256"), str)
                and re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", record["sha256"])
                for record in required_artifacts
            )
        )
    return not (
        candidate.get("status") in {"failed", "rejected", "cancelled"}
        or bool(candidate.get("hard_rejected"))
        or bool(candidate.get("failure_codes"))
        or candidate.get("required_checks_passed") is not True
        or score.get("hard_rejected") is True
        or finite_total is None
        or candidate.get("promotion_blocked") is True
        or candidate.get("bambuddy_send_blocked") is True
        or cadquery_slice_invalid
    )

def select_winner(candidates: list[dict], current_best_score: float) -> dict:
    """Select a winner without ever replacing the best with a regression."""

    valid = [
        candidate
        for candidate in candidates
        if deterministic_candidate_eligible(candidate)
    ]
    ranked = sorted(valid, key=lambda item: float(item["score"]["total"]), reverse=True)
    highest = ranked[0] if ranked else None
    normalized_current_score = _finite_score(current_best_score)
    improved = bool(
        highest
        and normalized_current_score is not None
        and float(highest["score"]["total"]) > normalized_current_score
    )
    return {
        "highest_scoring_candidate_id": highest.get("candidate_id") if highest else None,
        "highest_score": float(highest["score"]["total"]) if highest else None,
        "winner_candidate_id": highest.get("candidate_id") if improved else None,
        "improved_current_best": improved,
        "current_best_preserved": not improved,
        "reason": (
            "highest valid candidate beat current best"
            if improved
            else "no valid candidate beat current best; current best preserved"
        ),
    }
