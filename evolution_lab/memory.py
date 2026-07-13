"""Scoped, evidence-thresholded persistent learning rules."""

from __future__ import annotations

import math
from typing import Any

from .store import EvolutionStore, new_id, utc_ts


INACTIVE_STATUSES = {"deprecated", "rejected"}
SCOPE_KEYS = (
    "printer_profile",
    "printer",
    "material",
    "nozzle",
    "layer_height",
    "slicer_profile",
    "feature",
    "feature_type",
    "model_category",
)


def scopes_compatible(rule_scope: dict[str, Any], context: dict[str, Any]) -> tuple[bool, list[str]]:
    """Compatibility is explicit: every scoped value must match the context."""

    reasons = []
    for key in SCOPE_KEYS:
        if key not in rule_scope:
            continue
        if key not in context:
            reasons.append(f"context missing {key}")
        elif rule_scope[key] != context[key]:
            reasons.append(f"{key} mismatch")
    return not reasons, reasons


def _confidence(successes: int, failures: int, physical_count: int) -> float:
    observations = successes + failures
    if not observations:
        return 0.0
    # Conservative beta prior; physical observations modestly increase certainty,
    # never the underlying success ratio.
    posterior = (successes + 1) / (observations + 2)
    certainty = 1 - math.exp(-(observations + physical_count) / 4)
    return round(posterior * certainty, 4)


def derive_status(rule: dict) -> str:
    if rule.get("status") in {"disputed", "deprecated", "rejected"}:
        return rule["status"]
    evidence = int(rule.get("evidence_count", 0))
    successes = int(rule.get("success_count", 0))
    failures = int(rule.get("failure_count", 0))
    rate = successes / max(1, successes + failures)
    models = len(set(rule.get("source_model_ids", [])))
    physical = int(rule.get("physical_evidence_count", 0))
    major = bool(rule.get("major_regression_count", 0))
    if evidence >= 10 and rate > 0.90 and models >= 2 and physical > 0 and not major:
        return "high-confidence"
    if evidence >= 5 and rate > 0.80 and physical > 0 and not major:
        return "validated"
    if evidence >= 3 and rate > 0.65:
        return "provisional"
    return "hypothesis"


class MemoryService:
    def __init__(self, store: EvolutionStore):
        self.store = store

    def create_rule(self, payload: dict) -> dict:
        now = utc_ts()
        record = {
            "id": new_id("rule"),
            "rule_id": "",
            "category": payload["category"],
            "title": payload["title"],
            "description": payload.get("description", ""),
            "scope": payload.get("scope", {}),
            "trigger_conditions": payload.get("trigger_conditions", ""),
            "recommendation": payload["recommendation"],
            "evidence_count": 0,
            "success_count": 0,
            "failure_count": 0,
            "physical_evidence_count": 0,
            "major_regression_count": 0,
            "confidence": 0.0,
            "printer_profiles_involved": [],
            "materials_involved": [],
            "nozzle_sizes_involved": [],
            "layer_heights_involved": [],
            "feature_types_involved": [],
            "source_model_ids": [],
            "source_candidate_ids": [],
            "created_at": now,
            "last_validation_at": None,
            "status": "hypothesis",
            "contradiction_history": [],
            "notes": payload.get("notes", ""),
            "observations": [],
        }
        record["rule_id"] = record["id"]
        return self.store.create_record("memory", record, prefix="rule")

    def observe(self, rule_id: str, observation: dict) -> dict:
        def apply(rule: dict) -> None:
            observation_id = observation.get("physical_validation_id") or observation.get("observation_id")
            if observation_id and any(
                item.get("physical_validation_id") == observation_id
                or item.get("observation_id") == observation_id
                for item in rule.get("observations", [])
            ):
                return
            success = bool(observation["success"])
            rule["evidence_count"] = int(rule.get("evidence_count", 0)) + 1
            key = "success_count" if success else "failure_count"
            rule[key] = int(rule.get(key, 0)) + 1
            if observation.get("physical"):
                rule["physical_evidence_count"] = int(rule.get("physical_evidence_count", 0)) + 1
            if observation.get("major_regression"):
                rule["major_regression_count"] = int(rule.get("major_regression_count", 0)) + 1
            for key_name, target in (
                ("source_model_id", "source_model_ids"),
                ("source_candidate_id", "source_candidate_ids"),
            ):
                value = observation.get(key_name)
                if value and value not in rule.setdefault(target, []):
                    rule[target].append(value)
            item = {
                "timestamp": utc_ts(),
                "success": success,
                "physical": bool(observation.get("physical")),
                "major_regression": bool(observation.get("major_regression")),
                "source_model_id": observation.get("source_model_id"),
                "source_candidate_id": observation.get("source_candidate_id"),
                "physical_validation_id": observation.get("physical_validation_id"),
                "observation_id": observation.get("observation_id"),
                "note": observation.get("note", ""),
            }
            rule.setdefault("observations", []).append(item)
            if not success:
                rule.setdefault("contradiction_history", []).append(item)
            rule["confidence"] = _confidence(
                int(rule["success_count"]),
                int(rule["failure_count"]),
                int(rule.get("physical_evidence_count", 0)),
            )
            rule["last_validation_at"] = item["timestamp"]
            rule["status"] = derive_status(rule)

        return self.store.update_record("memory", rule_id, apply)
    def query(self, context: dict[str, Any]) -> dict:
        result = {"applied": [], "recommended": [], "shown": [], "ignored": []}
        for rule in self.store.list_records("memory"):
            status = rule.get("status", "hypothesis")
            compatible, reasons = scopes_compatible(rule.get("scope", {}), context)
            if status in INACTIVE_STATUSES or not compatible:
                result["ignored"].append({"rule": rule, "reasons": reasons or [f"status {status}"]})
            elif status in {"validated", "high-confidence"} and int(
                rule.get("physical_evidence_count", 0)
            ) > 0:
                result["applied"].append(rule)
            elif status in {"validated", "high-confidence"}:
                result["recommended"].append(rule)
            elif status == "provisional":
                result["recommended"].append(rule)
            else:
                result["shown"].append(rule)
        return result

    def review(self, rule_id: str, action: str, note: str = "") -> dict:
        mapping = {
            "approve_for_testing": "provisional",
            "dispute": "disputed",
            "deprecate": "deprecated",
            "reject": "rejected",
        }

        def apply(rule: dict) -> None:
            if action == "reset_confidence":
                rule["confidence"] = 0.0
                rule["status"] = "hypothesis"
            else:
                rule["status"] = mapping[action]
            rule.setdefault("review_history", []).append(
                {"timestamp": utc_ts(), "action": action, "note": note}
            )

        return self.store.update_record("memory", rule_id, apply)
