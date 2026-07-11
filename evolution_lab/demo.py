"""Clearly labelled, isolated seeded demo fixture."""

from __future__ import annotations

from .schemas import EvidenceLabel
from .scoring import score_candidate
from .store import EvolutionStore, utc_ts


DEMO_RUN_ID = "demo-six-seven-v1"
DEMO_BANNER = "DEMO DATA — NOT A REAL TRAINING RUN"


def _evidence(total_shape: tuple[float, float, float, float, float, float], warning: str = "") -> list[dict]:
    categories = (
        "printability",
        "function",
        "prompt_spec_adherence",
        "structural_quality",
        "user_experience_ergonomics",
        "simplicity_efficiency",
    )
    maxima = (25, 25, 20, 10, 10, 10)
    out = []
    for index, (category, earned, possible) in enumerate(zip(categories, total_shape, maxima)):
        out.append({
            "category": category,
            "criterion": "seeded demonstration criterion",
            "points_awarded": earned,
            "points_possible": possible,
            "label": EvidenceLabel.MEASURED.value if index == 0 else EvidenceLabel.AI_JUDGED.value,
            "source": "seeded-demo-fixture",
            "summary": warning if index == 0 and warning else "Seeded example for interface demonstration",
            "confidence": 0.8 if index == 0 else 0.55,
            "critical": False,
        })
    return out


def _candidate(
    run_id: str,
    candidate_id: str,
    generation: int,
    label: str,
    mutation: dict,
    shape: tuple[float, float, float, float, float, float],
    status: str,
    issues: list[dict],
) -> dict:
    evidence = _evidence(shape, issues[0]["message"] if issues else "")
    return {
        "candidate_id": candidate_id,
        "run_id": run_id,
        "generation": generation,
        "variant_label": label,
        "parent_candidate_id": "demo-baseline",
        "current_best_parent_id": "demo-baseline",
        "mutation": mutation,
        "expected_benefit": mutation.get("expected_benefit", ""),
        "prompt_used": "Seeded demo prompt; no backend call occurred.",
        "spec_used": "Seeded SIX SEVEN demo specification.",
        "locks_applied": ["body outline", "slider", "SIX SEVEN text"],
        "printer_profile_snapshot": {"printer": "Bambu P1S", "material": "PETG", "nozzle": 0.4, "layer": 0.2},
        "material_profile_snapshot": {"material": "PETG"},
        "backend": "demo-fixture/no-provider",
        "generation_duration_seconds": 0,
        "estimated_generation_cost": 0,
        "qa_results": issues,
        "slicer_results": {"status": "skipped", "reason": "demo fixture"},
        "score_evidence": evidence,
        "score": score_candidate(evidence),
        "failure_reasons": [],
        "rejection_reasons": [] if status == "winner" else ["lower score than Variant A"],
        "selection_status": status,
        "status": "complete",
        "artifacts": [],
        "memory_rules_applied": [],
        "memory_rules_ignored": [],
        "demo": True,
        "created_at": utc_ts(),
        "updated_at": utc_ts(),
    }


def load_demo_fixture(store: EvolutionStore) -> dict:
    try:
        return store.get_run(DEMO_RUN_ID)
    except FileNotFoundError:
        pass

    now = utc_ts()
    run = {
        "run_id": DEMO_RUN_ID,
        "demo": True,
        "demo_banner": DEMO_BANNER,
        "status": "complete",
        "source_model_id": "b39de61ede25",
        "source_prompt": "67 / SIX SEVEN meme fidget toy",
        "validated_spec": "Seeded demonstration spec; no generation provider was called.",
        "printer_profile": {"name": "Bambu P1S - 0.4mm PETG", "printer": "Bambu P1S", "material": "PETG", "nozzle": 0.4, "layer": 0.2},
        "material_profile": {"material": "PETG"},
        "locked_constraints": ["preserve slider geometry", "preserve body outline", "preserve SIX SEVEN text"],
        "attached_reference_roles": [],
        "export_exclusions": [],
        "active_backend": "demo-fixture/no-provider",
        "limits": {"variants_per_generation": 2, "maximum_generations": 2, "target_reward_score": 92},
        "current_generation": 1,
        "baseline_candidate_id": "demo-baseline",
        "current_best_candidate_id": "demo-g1-a",
        "highest_scoring_candidate_id": "demo-g1-a",
        "latest_validated_candidate_id": "demo-g1-a",
        "current_best_score": 86,
        "baseline_score": 70,
        "stop_after_current_generation": False,
        "lineage_edges": [
            {"parent": "demo-baseline", "child": "demo-g1-a"},
            {"parent": "demo-baseline", "child": "demo-g1-b"},
        ],
        "generation_results": [{
            "generation": 1,
            "candidate_ids": ["demo-g1-a", "demo-g1-b"],
            "winner_candidate_id": "demo-g1-a",
            "current_best_preserved": False,
        }],
        "next_mutation_proposals": [
            {"variant": "A", "mutation": "test spinner clearance 0.45→0.50mm"},
            {"variant": "B", "mutation": "reinforce retention lip base"},
        ],
        "demo_memory_rules": [{
            "rule_id": "demo-spinner-clearance-hypothesis",
            "title": "P1S PETG spinner clearance may benefit from 0.45mm",
            "status": "hypothesis",
            "scope": {"printer": "Bambu P1S", "material": "PETG", "nozzle": 0.4, "layer_height": 0.2, "feature": "spinner"},
            "confidence_progression": [0.42, 0.61],
            "evidence_count": 1,
            "success_count": 1,
            "failure_count": 0,
            "recommendation": "Test physically before promotion.",
        }],
        "summary": {
            "initial_score": 70,
            "final_score": 86,
            "improvement": 16,
            "candidates_generated": 2,
            "candidates_rejected": 1,
            "physical_status": "not printed; demo only",
            "actual_training_performed": False,
        },
        "created_at": now,
        "updated_at": now,
    }
    store.create_run(run, {"model.scad": "// DEMO baseline only\n", "meta.json": '{"demo":true}\n'})
    baseline_evidence = _evidence((18, 16, 15, 7, 7, 7), "Seeded baseline floating-region warning")
    baseline = {
        "candidate_id": "demo-baseline",
        "run_id": DEMO_RUN_ID,
        "generation": 0,
        "variant_label": "BASELINE",
        "parent_candidate_id": None,
        "current_best_parent_id": None,
        "mutation": None,
        "score_evidence": baseline_evidence,
        "score": score_candidate(baseline_evidence),
        "selection_status": "baseline",
        "status": "complete",
        "artifacts": [],
        "demo": True,
        "created_at": now,
        "updated_at": now,
    }
    store.create_candidate(DEMO_RUN_ID, baseline)
    store.add_candidate_artifacts(DEMO_RUN_ID, "demo-baseline", {"model.scad": "// DEMO baseline only\n"})
    a = _candidate(
        DEMO_RUN_ID, "demo-g1-a", 1, "A",
        {"mutation_type": "spinner_clearance", "parameter": "spinner_clearance", "original_value": 0.4, "mutated_value": 0.45, "expected_benefit": "reduce spinner binding"},
        (23, 21, 18, 8, 8, 8), "winner",
        [{"issue_type": "moving_clearance", "severity": "warning", "coordinates": [-21, -5, 8.45], "message": "Moving clearance remains unverified until printed", "source": "seeded-demo"}],
    )
    b = _candidate(
        DEMO_RUN_ID, "demo-g1-b", 1, "B",
        {"mutation_type": "spinner_retention", "parameter": "retention_lip", "original_value": "none", "mutated_value": "0.6mm lip", "expected_benefit": "stronger retention"},
        (16, 20, 18, 8, 7, 7), "rejected",
        [{"issue_type": "floating_geometry", "severity": "error", "coordinates": [-21, -5, 9.2], "message": "Seeded floating-geometry warning at retention lip", "source": "seeded-demo"}],
    )
    for candidate in (a, b):
        store.create_candidate(DEMO_RUN_ID, candidate)
        store.add_candidate_artifacts(
            DEMO_RUN_ID, candidate["candidate_id"],
            {"model.scad": f"// DEMO {candidate['variant_label']} only\n", "qa-report.json": str(candidate["qa_results"])},
        )
    store.create_checkpoint(DEMO_RUN_ID, "demo-baseline", "baseline")
    store.create_checkpoint(DEMO_RUN_ID, "demo-g1-a", "current_best")
    for severity, event_type, message, candidate_id in (
        ("info", "baseline_loaded", "Demo baseline loaded", "demo-baseline"),
        ("info", "candidate_started", "Demo Variant A started", "demo-g1-a"),
        ("warning", "qa_finding", "Demo moving-clearance warning", "demo-g1-a"),
        ("info", "candidate_started", "Demo Variant B started", "demo-g1-b"),
        ("error", "qa_finding", "Demo floating-geometry warning", "demo-g1-b"),
        ("info", "winner_selected", "Demo Variant A selected", "demo-g1-a"),
        ("info", "memory_hypothesis", "Demo memory hypothesis created", "demo-g1-a"),
        ("info", "run_completed", "Demo run completed", None),
    ):
        store.append_event(DEMO_RUN_ID, severity, event_type, message, candidate_id=candidate_id, generation=1)
    return store.get_run(DEMO_RUN_ID)
