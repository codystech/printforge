"""Evidence-first reward scoring and regression-safe winner selection."""

from __future__ import annotations

from collections import defaultdict
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

def select_winner(candidates: list[dict], current_best_score: float) -> dict:
    """Select a winner without ever replacing the best with a regression."""

    valid = [
        candidate
        for candidate in candidates
        if not candidate.get("score", {}).get("hard_rejected")
        and candidate.get("status") not in {"failed", "rejected"}
    ]
    ranked = sorted(valid, key=lambda item: float(item.get("score", {}).get("total", 0)), reverse=True)
    highest = ranked[0] if ranked else None
    improved = bool(highest and float(highest["score"]["total"]) > float(current_best_score))
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
