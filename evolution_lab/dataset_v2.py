"""Training-quality, evidence-linked dataset rows for the isolated Training Lab.

The v2 builders are deliberately stricter than the legacy export.  A rendered
artifact is useful for inspection, but it is not automatically eligible for
weight training.  Every row retains its source, evidence masks, provenance,
family-separated split, and evaluator/profile identity so later trainers can
fail closed instead of guessing what was measured.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Iterable

from .store import EvolutionStore


SCHEMA_V2 = "printforge-training-dataset-v2"
SOURCE_ARTIFACTS = {"cadquery-v1": "model.py", "openscad-legacy": "model.scad"}
ACCEPTED_PROVENANCE = {"self-created", "verified", "licensed"}
TRAINING_LICENSE_RIGHTS = {"owned", "licensed_for_training", "public_domain"}
INELIGIBLE_STATUSES = {"failed", "cancelled"}
INELIGIBLE_SELECTIONS = {"rejected", "regression", "cancelled"}
SLICE_SUCCESS = {"complete"}


def canonical_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def profile_fingerprint(*profiles: Any) -> str:
    """Return a stable non-secret identity for a versioned evaluator/profile set."""

    return f"sha256:{canonical_sha256(list(profiles))}"


def family_split(split_key: str) -> str:
    """Assign a whole design family to one frozen split.

    Hashing the family key, rather than candidate IDs, guarantees that siblings
    and later repairs cannot leak across train/validation/test.
    """

    if not isinstance(split_key, str) or not split_key.strip():
        raise ValueError("part-family split key is required")
    bucket = int(hashlib.sha256(split_key.strip().casefold().encode("utf-8")).hexdigest()[:8], 16) % 100
    if bucket < 80:
        return "train"
    if bucket < 90:
        return "validation"
    return "test"


def source_artifact_name(candidate: dict) -> str:
    return SOURCE_ARTIFACTS.get(candidate.get("model_format") or "openscad-legacy", "model.scad")


def candidate_source(store: EvolutionStore, run_id: str, candidate: dict) -> str | None:
    try:
        return store.candidate_artifact(
            run_id, candidate["candidate_id"], source_artifact_name(candidate)
        ).read_text(encoding="utf-8")
    except (KeyError, OSError, UnicodeError, ValueError, FileNotFoundError):
        return None


def matching_artifact(candidate: dict, checksum: str, artifact_name: str | None = None) -> dict | None:
    expected = str(checksum or "").removeprefix("sha256:").lower()
    if len(expected) != 64:
        return None
    for artifact in candidate.get("artifacts") or []:
        if artifact_name and artifact.get("name") != artifact_name:
            continue
        if str(artifact.get("sha256") or "").lower() == expected:
            return artifact
    return None


def artifact_role(candidate: dict, artifact_name: str) -> str:
    """Derive export role from persisted model metadata, never request input."""

    model_format = candidate.get("model_format") or "openscad-legacy"
    if model_format == "openscad-legacy" and artifact_name.lower().endswith(".stl"):
        return "printable"
    slicer = candidate.get("slicer_results") if isinstance(candidate.get("slicer_results"), dict) else {}
    if (
        artifact_name == slicer.get("sliced_3mf_artifact")
        and str(slicer.get("status") or "").casefold() in SLICE_SUCCESS
    ):
        return "sliced-printable"
    for part in candidate.get("parts") or []:
        if part.get("stl_artifact") == artifact_name and artifact_name.casefold().endswith(".stl"):
            return str(part.get("export_role") or "metadata")
    return "metadata"


def printable_artifact(candidate: dict, checksum: str, artifact_name: str | None = None) -> dict | None:
    artifact = matching_artifact(candidate, checksum, artifact_name)
    if artifact is None or artifact_role(candidate, str(artifact.get("name") or "")) not in {
        "printable", "sliced-printable"
    }:
        return None
    return artifact


def _timezone_aware(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def evidence_coverage(candidate: dict) -> tuple[dict[str, dict[str, bool | None]], dict[str, bool]]:
    """Report evidence presence independently from its pass/fail result."""

    deterministic_evidence = candidate.get("deterministic_evidence") or candidate.get("score_evidence") or []
    deterministic_present = bool(deterministic_evidence)
    deterministic_passed = bool(candidate.get("required_checks_passed")) if deterministic_present else None
    slicer_results = candidate.get("slicer_results") if isinstance(candidate.get("slicer_results"), dict) else {}
    slicer_status = str(slicer_results.get("status") or "").casefold()
    slicer_present = bool(slicer_results) and slicer_status not in {"", "unavailable", "not_submitted", "missing", "skipped"}
    slicer_passed = slicer_status in SLICE_SUCCESS if slicer_present else None
    verified_physical = [item for item in candidate.get("physical_outcomes") or [] if item.get("verified_join")]
    physical_present = bool(verified_physical)
    physical_passed = (
        all(bool(item.get("printed_successfully")) for item in verified_physical)
        if physical_present else None
    )
    coverage = {
        "deterministic": {"present": deterministic_present, "passed": deterministic_passed},
        "slicer": {"present": slicer_present, "passed": slicer_passed},
        "physical": {"present": physical_present, "passed": physical_passed},
    }
    return coverage, {key: not bool(result["present"]) for key, result in coverage.items()}


def _artifact_hashes(candidate: dict) -> dict[str, str]:
    return {
        str(item.get("name")): f"sha256:{item.get('sha256')}"
        for item in candidate.get("artifacts") or []
        if item.get("name") and item.get("sha256")
    }


def _family_key(run: dict, candidate: dict | None = None) -> str:
    """Use only the validated run/design family; candidates cannot choose splits."""

    return str(run.get("part_family_split_key") or run.get("part_family") or "").strip().casefold()


def _audit_checksum(audit: dict) -> str:
    return canonical_sha256({key: value for key, value in audit.items() if key != "audit_sha256"})


def _consent_and_provenance(run: dict, candidate: dict) -> tuple[bool, dict]:
    """Validate the immutable store-issued audit against the exact source hash."""

    audit = candidate.get("provenance_audit") if isinstance(candidate.get("provenance_audit"), dict) else {}
    source_sha256 = candidate.get("source_sha256")
    status = str(audit.get("provenance_status") or "unknown")
    rights = str(audit.get("license_rights") or "")
    expected_audit = f"sha256:{_audit_checksum(audit)}" if audit else ""
    valid = bool(
        run.get("training_consent") is True
        and run.get("training_consent_decision") == "approved"
        and audit.get("immutable") is True
        and audit.get("issuer") == "printforge-evolution-store-v1"
        and audit.get("decision") == "approved"
        and audit.get("reviewer") == run.get("training_consent_reviewer")
        and audit.get("reviewed_at") == run.get("training_consent_reviewed_at")
        and _timezone_aware(audit.get("reviewed_at"))
        and audit.get("run_id") == run.get("run_id")
        and audit.get("candidate_id") == candidate.get("candidate_id")
        and audit.get("source_artifact") == source_artifact_name(candidate)
        and isinstance(source_sha256, str)
        and audit.get("source_sha256") == source_sha256
        and status in ACCEPTED_PROVENANCE
        and str(audit.get("source") or "").strip()
        and str(audit.get("source_revision") or "").strip()
        and str(audit.get("license") or "").strip()
        and rights in TRAINING_LICENSE_RIGHTS
        and audit.get("audit_sha256") == expected_audit
    )
    return valid, {
        "training_consent": run.get("training_consent") is True,
        "decision": audit.get("decision", "not_reviewed"),
        "reviewer": audit.get("reviewer", ""),
        "reviewed_at": audit.get("reviewed_at"),
        "provenance_status": status,
        "source": audit.get("source", ""),
        "source_revision": audit.get("source_revision", ""),
        "source_sha256": audit.get("source_sha256"),
        "license": audit.get("license", ""),
        "license_rights": rights,
        "audit_sha256": audit.get("audit_sha256"),
        "audit_valid": valid,
    }


def _stored_artifact_verified(
    store: EvolutionStore, run_id: str, candidate: dict, name: str
) -> bool:
    record = next((item for item in candidate.get("artifacts") or [] if item.get("name") == name), None)
    if not record or not isinstance(record.get("sha256"), str) or not isinstance(record.get("size"), int):
        return False
    try:
        path = store.candidate_artifact(run_id, candidate["candidate_id"], name)
        raw = path.read_bytes()
    except (KeyError, OSError, ValueError, FileNotFoundError):
        return False
    return bool(
        raw and len(raw) == record["size"]
        and hashlib.sha256(raw).hexdigest() == record["sha256"].removeprefix("sha256:")
    )


def slice_evidence_ready(store: EvolutionStore, run: dict, candidate: dict) -> bool:
    """Require one exact complete slice plus rehashed persisted 3MF and log."""

    slicer = candidate.get("slicer_results") if isinstance(candidate.get("slicer_results"), dict) else {}
    sliced_name = slicer.get("sliced_3mf_artifact")
    log_name = slicer.get("log_artifact")
    metrics_complete = bool(
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
        and isinstance(sliced_name, str)
        and isinstance(log_name, str)
        and _stored_artifact_verified(store, run["run_id"], candidate, sliced_name)
        and _stored_artifact_verified(store, run["run_id"], candidate, log_name)
    )
    return bool(
        candidate.get("slicer_profile_fingerprint")
        and re.fullmatch(r"sha256:[0-9a-f]{64}", str(candidate.get("slicer_profile_fingerprint")))
        and metrics_complete
    )


def _training_evidence_ready(store: EvolutionStore, run: dict, candidate: dict) -> bool:
    coverage, _ = evidence_coverage(candidate)
    return bool(
        coverage["deterministic"]["present"]
        and coverage["deterministic"]["passed"] is True
        and coverage["slicer"]["present"]
        and coverage["slicer"]["passed"] is True
        and candidate.get("evaluator_fingerprint")
        and slice_evidence_ready(store, run, candidate)
    )


def _base_eligible(store: EvolutionStore, run: dict, candidate: dict) -> bool:
    consent_ok, _ = _consent_and_provenance(run, candidate)
    score = candidate.get("score") if isinstance(candidate.get("score"), dict) else {}
    return bool(
        consent_ok
        and _family_key(run, candidate)
        and candidate.get("status") not in INELIGIBLE_STATUSES
        and candidate.get("selection_status") not in INELIGIBLE_SELECTIONS
        and not score.get("hard_rejected")
        and candidate.get("required_checks_passed") is True
        and _training_evidence_ready(store, run, candidate)
    )


def _source_audited(store: EvolutionStore, run: dict, candidate: dict) -> bool:
    consent_ok, _ = _consent_and_provenance(run, candidate)
    source = candidate_source(store, run["run_id"], candidate)
    if not consent_ok or source is None:
        return False
    actual = f"sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}"
    audit = candidate.get("provenance_audit") or {}
    return actual == candidate.get("source_sha256") == audit.get("source_sha256")


def _candidate_snapshot(store: EvolutionStore, run: dict, candidate: dict) -> dict:
    source = candidate_source(store, run["run_id"], candidate)
    coverage, missing = evidence_coverage(candidate)
    split_key = _family_key(run, candidate)
    _, provenance = _consent_and_provenance(run, candidate)
    return {
        "candidate_id": candidate.get("candidate_id"),
        "run_id": run.get("run_id"),
        "model_format": candidate.get("model_format") or run.get("model_format") or "openscad-legacy",
        "model_contract_version": candidate.get("model_contract_version") or candidate.get("model_format") or "openscad-legacy-v1",
        "source": source,
        "source_sha256": f"sha256:{hashlib.sha256(source.encode('utf-8')).hexdigest()}" if source is not None else None,
        "source_prompt": candidate.get("source_prompt", run.get("source_prompt", "")),
        "validated_spec": candidate.get("validated_spec", candidate.get("spec_used", run.get("validated_spec", ""))),
        "generation_prompt": candidate.get("generation_prompt", candidate.get("prompt_used", "")),
        "lineage": {
            "parent_candidate_id": candidate.get("parent_candidate_id"),
            "current_best_parent_id": candidate.get("current_best_parent_id"),
            "generation": candidate.get("generation"),
            "variant_label": candidate.get("variant_label"),
        },
        "mutation": candidate.get("mutation"),
        "deterministic_evidence": candidate.get("deterministic_evidence") or candidate.get("score_evidence") or [],
        "slicer_evidence": candidate.get("slicer_results") or {"status": "unavailable"},
        "physical_outcomes": [
            item for item in candidate.get("physical_outcomes") or [] if item.get("verified_join") is True
        ],
        "evaluator_version": candidate.get("evaluator_version"),
        "evaluator_fingerprint": candidate.get("evaluator_fingerprint"),
        "slicer_profile_fingerprint": candidate.get("slicer_profile_fingerprint"),
        "artifact_hashes": _artifact_hashes(candidate),
        "evidence_coverage": coverage,
        "missing_evidence_mask": missing,
        "part_family_split_key": split_key,
        "split": family_split(split_key) if split_key else None,
        "provenance": provenance,
        "score": candidate.get("score"),
        "failure_reasons": candidate.get("failure_reasons") or [],
    }


def has_verified_physical_failure(
    store: EvolutionStore, run: dict, candidate: dict
) -> bool:
    """Keep an exact failed artifact out of SFT permanently.

    A metadata approval cannot turn an observed failed print into a successful
    completion.  A repair must be represented by a new candidate/source and
    printable-artifact checksum, which gives it an independent evidence trail.
    """

    for physical in store.list_records("physical"):
        if (
            physical.get("run_id") != run.get("run_id")
            or physical.get("candidate_id") != candidate.get("candidate_id")
            or physical.get("verified_join") is not True
            or physical.get("candidate_joined") is not True
            or physical.get("printed_successfully") is not False
        ):
            continue
        artifact = printable_artifact(
            candidate,
            str(physical.get("artifact_checksum") or ""),
            physical.get("artifact_name"),
        )
        if artifact is None:
            continue
        try:
            expected_id = store.physical_validation_id(
                run["run_id"], candidate["candidate_id"], artifact["sha256"]
            )
        except (KeyError, ValueError):
            continue
        if physical.get("id") != expected_id:
            continue
        if any(
            item.get("physical_validation_id") == expected_id
            and item.get("verified_join") is True
            and item.get("artifact_checksum") == physical.get("artifact_checksum")
            and item.get("artifact_name") == physical.get("artifact_name")
            and item.get("printed_successfully") is False
            for item in candidate.get("physical_outcomes") or []
        ):
            return True
    return False


def _runs(store: EvolutionStore, run_id: str | None) -> list[dict]:
    runs = [store.get_run(run_id)] if run_id else store.list_runs(include_demo=False)
    return [run for run in runs if not run.get("demo")]


def build_sft_rows(store: EvolutionStore, runs: Iterable[dict]) -> list[dict]:
    rows = []
    for run in runs:
        for candidate in store.list_candidates(run["run_id"]):
            accepted = candidate.get("selection_status") in {"winner", "accepted", "promoted"}
            if not (
                _base_eligible(store, run, candidate)
                and _source_audited(store, run, candidate)
                and accepted
                and not has_verified_physical_failure(store, run, candidate)
            ):
                continue
            snapshot = _candidate_snapshot(store, run, candidate)
            if snapshot["model_format"] != "cadquery-v1" or not snapshot["source"]:
                continue
            rows.append({
                "schema": SCHEMA_V2,
                "example_type": "sft",
                "prompt": snapshot["source_prompt"],
                "specification": snapshot["validated_spec"],
                "completion": snapshot["source"],
                "candidate": snapshot,
                "split": snapshot["split"],
            })
    return rows


def _verified_physical_results(
    store: EvolutionStore, run: dict, candidate: dict
) -> list[bool]:
    """Return only physical results with exact, complete persisted backlinks."""

    results: list[bool] = []
    for backlink in candidate.get("physical_outcomes") or []:
        physical_id = backlink.get("physical_validation_id")
        if not physical_id or backlink.get("verified_join") is not True:
            continue
        try:
            physical = store.get_record("physical", physical_id)
        except (ValueError, FileNotFoundError):
            continue
        artifact = printable_artifact(
            candidate,
            str(physical.get("artifact_checksum") or ""),
            physical.get("artifact_name"),
        )
        if artifact is None:
            continue
        try:
            expected_id = store.physical_validation_id(
                run["run_id"], candidate["candidate_id"], artifact["sha256"]
            )
        except (KeyError, ValueError):
            continue
        if not (
            physical.get("id") == expected_id == physical_id
            and physical.get("run_id") == run.get("run_id")
            and physical.get("candidate_id") == candidate.get("candidate_id")
            and physical.get("verified_join") is True
            and physical.get("candidate_joined") is True
            and physical.get("memory_joined") is True
            and backlink.get("artifact_checksum") == physical.get("artifact_checksum")
            and backlink.get("artifact_name") == physical.get("artifact_name")
            and backlink.get("printed_successfully") is physical.get("printed_successfully")
        ):
            continue
        mutation_required = bool(candidate.get("mutation")) and int(candidate.get("generation", 0)) > 0
        if mutation_required:
            mutation_id = physical.get("mutation_outcome_id")
            if not mutation_id or physical.get("mutation_outcome_joined") is not True:
                continue
            try:
                mutation = store.get_mutation_outcome(mutation_id)
            except (ValueError, FileNotFoundError):
                continue
            if not (
                mutation.get("run_id") == run.get("run_id")
                and mutation.get("candidate_id") == candidate.get("candidate_id")
                and int(mutation.get("generation", -1)) == int(candidate.get("generation", 0))
                and any(
                    item.get("physical_validation_id") == physical_id
                    and item.get("verified_join") is True
                    and item.get("artifact_checksum") == physical.get("artifact_checksum")
                    and item.get("artifact_name") == physical.get("artifact_name")
                    for item in mutation.get("physical_outcomes") or []
                )
            ):
                continue
        expected_rules = sorted(
            rule_id for rule_id in candidate.get("memory_rules_applied") or []
            if isinstance(rule_id, str)
        )
        if sorted(physical.get("memory_rule_ids_observed") or []) != expected_rules:
            continue
        memory_ok = True
        for rule_id in expected_rules:
            try:
                rule = store.get_record("memory", rule_id)
            except (ValueError, FileNotFoundError):
                memory_ok = False
                break
            if not any(
                item.get("physical_validation_id") == physical_id
                for item in rule.get("observations") or []
            ):
                memory_ok = False
                break
        if memory_ok:
            results.append(bool(physical.get("printed_successfully")))
    return results


def _label_authority(
    store: EvolutionStore, run: dict, chosen: dict, rejected: dict
) -> tuple[str, bool]:
    chosen_physical = _verified_physical_results(store, run, chosen)
    rejected_physical = _verified_physical_results(store, run, rejected)
    directionally_consistent = bool(
        chosen_physical
        and rejected_physical
        and all(result is True for result in chosen_physical)
        and all(result is False for result in rejected_physical)
    )
    if directionally_consistent:
        return "physical_outcome", False
    decisive_opposite = bool(
        chosen_physical
        and rejected_physical
        and all(result is False for result in chosen_physical)
        and all(result is True for result in rejected_physical)
    )
    if decisive_opposite:
        return "physical_outcome", True
    authority = str(chosen.get("comparison_authority") or rejected.get("comparison_authority") or "")
    if authority in {"human", "human_comparison"}:
        return "human_comparison", False
    if chosen.get("slicer_results") and rejected.get("slicer_results"):
        return "deterministic_sliced_comparison", False
    return "ai_judgment", False


def build_preference_rows(store: EvolutionStore, runs: Iterable[dict]) -> list[dict]:
    rows = []
    for run in runs:
        generations: dict[int, list[dict]] = {}
        for candidate in store.list_candidates(run["run_id"]):
            if candidate.get("variant_label") in {"A", "B"}:
                generations.setdefault(int(candidate.get("generation", 0)), []).append(candidate)
        for generation, candidates in generations.items():
            chosen = next((item for item in candidates if item.get("selection_status") == "winner"), None)
            rejected = next((item for item in candidates if item.get("selection_status") == "loser"), None)
            if (
                not chosen or not rejected
                or not _base_eligible(store, run, chosen) or not _base_eligible(store, run, rejected)
                or not _source_audited(store, run, chosen)
                or not _source_audited(store, run, rejected)
            ):
                continue
            evaluator = chosen.get("evaluator_fingerprint")
            slicer = chosen.get("slicer_profile_fingerprint")
            if not evaluator or not slicer:
                continue
            if evaluator != rejected.get("evaluator_fingerprint") or slicer != rejected.get("slicer_profile_fingerprint"):
                continue
            chosen_row = _candidate_snapshot(store, run, chosen)
            rejected_row = _candidate_snapshot(store, run, rejected)
            if not chosen_row["source"] or not rejected_row["source"]:
                continue
            if (
                not chosen_row["part_family_split_key"]
                or chosen_row["part_family_split_key"] != rejected_row["part_family_split_key"]
                or chosen_row["split"] != rejected_row["split"]
            ):
                continue
            label_authority, physical_veto = _label_authority(store, run, chosen, rejected)
            if physical_veto:
                continue
            rows.append({
                "schema": SCHEMA_V2,
                "example_type": "preference",
                "run_id": run["run_id"],
                "generation": generation,
                "prompt": run.get("source_prompt", ""),
                "specification": run.get("validated_spec", ""),
                "chosen": chosen_row,
                "rejected": rejected_row,
                "reason_codes": chosen.get("selection_reasons") or rejected.get("rejection_reasons") or [],
                "label_authority": label_authority,
                "evaluator_fingerprint": evaluator,
                "slicer_profile_fingerprint": slicer,
                "split": chosen_row["split"],
            })
    return rows


def build_mutation_rows(store: EvolutionStore, runs: Iterable[dict]) -> list[dict]:
    by_run = {run["run_id"]: run for run in runs}
    rows = []
    for outcome in store.list_mutation_outcomes(limit=1000):
        run = by_run.get(outcome.get("run_id"))
        if not run or not outcome.get("eligible"):
            continue
        try:
            child = store.get_candidate(run["run_id"], outcome["candidate_id"])
        except (KeyError, ValueError, FileNotFoundError):
            continue
        if not _base_eligible(store, run, child) or not _source_audited(store, run, child):
            continue
        child_row = _candidate_snapshot(store, run, child)
        if not child_row["source"]:
            continue
        parent = None
        if child.get("parent_candidate_id"):
            try:
                parent_candidate = store.get_candidate(run["run_id"], child["parent_candidate_id"])
                if (
                    not _base_eligible(store, run, parent_candidate)
                    or not _source_audited(store, run, parent_candidate)
                ):
                    continue
                parent = _candidate_snapshot(store, run, parent_candidate)
            except (ValueError, FileNotFoundError):
                parent = None
        if child.get("parent_candidate_id") and not parent:
            continue
        rows.append({
            "schema": SCHEMA_V2,
            "example_type": "mutation",
            "parent_state": parent,
            "allowed_actions": child.get("allowed_mutations") or run.get("allowed_mutations") or [],
            "selected_action": child.get("mutation"),
            "child_outcome": child_row,
            "reward_delta": outcome.get("score_delta"),
            "physical_outcomes": [
                item for item in (outcome.get("physical_outcomes") or child.get("physical_outcomes") or [])
                if item.get("verified_join") is True
            ],
            "split": child_row["split"],
        })
    return rows


def build_repair_rows(store: EvolutionStore, runs: Iterable[dict]) -> list[dict]:
    rows = []
    for run in runs:
        for child in store.list_candidates(run["run_id"]):
            repair = child.get("repair_example") or {}
            parent_id = child.get("repaired_from_candidate_id") or repair.get("failed_candidate_id")
            if (
                not parent_id
                or not _base_eligible(store, run, child)
                or not _source_audited(store, run, child)
            ):
                continue
            try:
                parent = store.get_candidate(run["run_id"], parent_id)
            except (ValueError, FileNotFoundError):
                continue
            verified_failure = parent.get("verified_failure_types") or repair.get("verified_failure_types") or []
            if (
                not verified_failure
                or not _base_eligible(store, run, parent)
                or not _source_audited(store, run, parent)
            ):
                continue
            parent_row = _candidate_snapshot(store, run, parent)
            child_row = _candidate_snapshot(store, run, child)
            if not parent_row["source"] or not child_row["source"]:
                continue
            rows.append({
                "schema": SCHEMA_V2,
                "example_type": "repair",
                "prompt": run.get("source_prompt", ""),
                "specification": run.get("validated_spec", ""),
                "failed_parent": parent_row,
                "failure_types": verified_failure,
                "accepted_repair": child_row,
                "split": child_row["split"],
            })
    return rows


def build_failure_rows(store: EvolutionStore, runs: Iterable[dict]) -> list[dict]:
    rows = []
    for run in runs:
        for candidate in store.list_candidates(run["run_id"]):
            failures = candidate.get("verified_failure_types") or []
            if (
                not failures
                or not _base_eligible(store, run, candidate)
                or not _source_audited(store, run, candidate)
            ):
                continue
            snapshot = _candidate_snapshot(store, run, candidate)
            if not snapshot["source"]:
                continue
            rows.append({
                "schema": SCHEMA_V2,
                "example_type": "failure",
                "source": snapshot,
                "verified_failure_types": failures,
                "deterministic_evidence": snapshot["deterministic_evidence"],
                "split": snapshot["split"],
            })
    return rows


def build_print_outcome_rows(store: EvolutionStore, runs: Iterable[dict]) -> list[dict]:
    by_run = {run["run_id"]: run for run in runs}
    rows = []
    for physical in store.list_records("physical"):
        if not physical.get("verified_join"):
            continue
        run = by_run.get(physical.get("run_id"))
        if not run or run.get("demo"):
            continue
        try:
            candidate = store.get_candidate(run["run_id"], physical["candidate_id"])
        except (KeyError, ValueError, FileNotFoundError):
            continue
        if not _base_eligible(store, run, candidate) or not _source_audited(store, run, candidate):
            continue
        artifact = printable_artifact(
            candidate,
            str(physical.get("artifact_checksum") or ""),
            physical.get("artifact_name"),
        )
        if artifact is None:
            continue
        try:
            expected_physical_id = store.physical_validation_id(
                run["run_id"], candidate["candidate_id"], artifact["sha256"]
            )
        except ValueError:
            continue
        if physical.get("id") != expected_physical_id or physical.get("artifact_role") != "printable":
            continue
        backlink = next((
            item for item in candidate.get("physical_outcomes") or []
            if item.get("physical_validation_id") == physical.get("id")
            and item.get("verified_join") is True
            and item.get("artifact_checksum") == physical.get("artifact_checksum")
            and item.get("artifact_name") == physical.get("artifact_name")
        ), None)
        if (
            backlink is None
            or physical.get("candidate_joined") is not True
            or physical.get("memory_joined") is not True
        ):
            continue
        mutation_required = bool(candidate.get("mutation")) and int(candidate.get("generation", 0)) > 0
        if mutation_required:
            mutation_id = physical.get("mutation_outcome_id")
            if not mutation_id or physical.get("mutation_outcome_joined") is not True:
                continue
            try:
                mutation = store.get_mutation_outcome(mutation_id)
            except (ValueError, FileNotFoundError):
                continue
            if not (
                mutation.get("run_id") == run.get("run_id")
                and mutation.get("candidate_id") == candidate.get("candidate_id")
                and int(mutation.get("generation", -1)) == int(candidate.get("generation", 0))
                and any(
                    item.get("physical_validation_id") == physical.get("id")
                    and item.get("verified_join") is True
                    and item.get("artifact_checksum") == physical.get("artifact_checksum")
                    and item.get("artifact_name") == physical.get("artifact_name")
                    for item in mutation.get("physical_outcomes") or []
                )
            ):
                continue
        expected_rules = sorted(
            rule_id for rule_id in candidate.get("memory_rules_applied") or []
            if isinstance(rule_id, str)
        )
        if sorted(physical.get("memory_rule_ids_observed") or []) != expected_rules:
            continue
        memory_backlinks_ok = True
        for rule_id in expected_rules:
            try:
                rule = store.get_record("memory", rule_id)
            except (ValueError, FileNotFoundError):
                memory_backlinks_ok = False
                break
            if not any(
                item.get("physical_validation_id") == physical.get("id")
                for item in rule.get("observations") or []
            ):
                memory_backlinks_ok = False
                break
        if not memory_backlinks_ok:
            continue
        slicer = candidate.get("slicer_results") or {}
        if (
            str(slicer.get("status") or "").casefold() not in SLICE_SUCCESS
            or not candidate.get("slicer_profile_fingerprint")
            or not candidate.get("evaluator_fingerprint")
        ):
            continue
        snapshot = _candidate_snapshot(store, run, candidate)
        if not snapshot["source"]:
            continue
        rows.append({
            "schema": SCHEMA_V2,
            "example_type": "print_outcome",
            "candidate": snapshot,
            "geometry_features": candidate.get("geometry_features") or {},
            "slicer_features": candidate.get("slicer_results") or {},
            "printer_profile": physical.get("printer_profile") or {},
            "material": physical.get("material"),
            "profile_fingerprint": physical.get("slicer_profile_fingerprint"),
            "physical_result": physical,
            "split": snapshot["split"],
        })
    return rows


def build_examples_v2(store: EvolutionStore, dataset_type: str, run_id: str | None = None) -> list[dict]:
    runs = _runs(store, run_id)
    builders = {
        "sft": build_sft_rows,
        "supervised": build_sft_rows,
        "preference": build_preference_rows,
        "mutation": build_mutation_rows,
        "repair": build_repair_rows,
        "failure": build_failure_rows,
        "print_outcome": build_print_outcome_rows,
    }
    if dataset_type == "all":
        rows = []
        for builder in dict.fromkeys(builders.values()):
            rows.extend(builder(store, runs))
        return rows
    try:
        return builders[dataset_type](store, runs)
    except KeyError as exc:
        raise ValueError("unsupported v2 dataset type") from exc
