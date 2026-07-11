"""Run the explicitly bounded real SIX SEVEN A/B demonstration without deploying.

This invokes the in-process Training Lab API, writes only ``training_lab_data/``,
performs one A/B generation, and never touches the production model library.
Required feature flags must be set in the environment by the operator.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx


SOURCE_ID = "b39de61ede25"


async def main() -> None:
    import app

    if not (app.EVOLUTION_LAB_CONFIG.training_lab_enabled and app.EVOLUTION_LAB_CONFIG.evolution_enabled):
        raise SystemExit("enable PRINT_FORGE_TRAINING_LAB_ENABLED and PRINT_FORGE_EVOLUTION_ENABLED for this process")
    if app.LLM_BACKEND != "codex":
        raise SystemExit("set LLM_BACKEND=codex explicitly; the code default is the local HTTP fallback")
    meta = json.loads((Path(__file__).parent.parent / "library" / SOURCE_ID / "meta.json").read_text())
    intent = meta.get("intent") or []
    spec = intent[0] if intent else meta.get("prompt", "")
    locks = [
        {"type": "module", "name": "body_67_2d"},
        {"type": "module", "name": "slider_knob"},
        {"type": "parameter", "name": "width"},
        {"type": "parameter", "name": "height"},
        {"type": "parameter", "name": "thickness"},
        {"type": "parameter", "name": "slider_track_length"},
        {"type": "parameter", "name": "slider_travel_distance"},
        {"type": "parameter", "name": "slider_knob_width"},
        {"type": "parameter", "name": "slider_knob_height"},
        {"type": "literal", "name": "SIX SEVEN text", "value": "SIX SEVEN"},
    ]
    payload = {
        "source_model_id": SOURCE_ID,
        "source_prompt": meta.get("prompt", "67 / SIX SEVEN fidget"),
        "validated_spec": spec,
        "printer_profile": meta.get("profile") or {},
        "material_profile": {"material": (meta.get("profile") or {}).get("material", "PETG")},
        "locked_constraints": locks,
        "attached_reference_roles": [],
        "export_exclusions": [],
        "active_backend": "codex/cli-default",
        "limits": {
            "variants_per_generation": 2,
            "maximum_generations": 1,
            "target_reward_score": 92,
            "maximum_runtime_seconds": 2400,
            "maximum_estimated_cost": 10,
            "maximum_backend_calls": 4,
            "no_improvement_limit": 2,
            "mutation_strength": 0.2,
            "exploration_rate": 0.1,
            "benchmark_mode": False,
            "physical_validation_required": False,
            "random_seed": 67,
        },
        "initial_mutations": [
            {
                "mutation_type": "spinner_clearance",
                "parameter": "spinner_clearance",
                "original_value": 0.4,
                "mutated_value": 0.45,
                "expected_benefit": "reduce spinner binding while leaving moving_clearance and all slider geometry unchanged",
                "reason": "split spinner-only clearance from the shared slider clearance",
            },
            {
                "mutation_type": "spinner_retention_geometry",
                "parameter": "spinner_retention",
                "original_value": "straight axle with through-hole",
                "mutated_value": "minimal printable retention feature",
                "expected_benefit": "retain the spinner more reliably without changing the slider, body outline, or text",
                "reason": "controlled retention alternative",
            },
        ],
        "auto_start": False,
    }
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://training-lab") as client:
        created = await client.post("/training-lab/api/runs", json=payload)
        created.raise_for_status()
        run_id = created.json()["run_id"]
        started = await client.post(f"/training-lab/api/runs/{run_id}/start")
        started.raise_for_status()
        while True:
            response = await client.get(f"/training-lab/api/runs/{run_id}")
            response.raise_for_status()
            run = response.json()
            if run["status"] in {"complete", "failed", "interrupted"}:
                print(json.dumps({
                    "run_id": run_id,
                    "status": run["status"],
                    "baseline_score": run.get("baseline_score"),
                    "current_best_score": run.get("current_best_score"),
                    "current_best_candidate_id": run.get("current_best_candidate_id"),
                    "generation_results": run.get("generation_results"),
                    "next_mutation_proposals": run.get("next_mutation_proposals"),
                    "estimated_cost": run.get("estimated_cost"),
                    "backend_calls": run.get("backend_calls"),
                    "actual_training_performed": False,
                }, indent=2))
                if run["status"] != "complete":
                    raise SystemExit(1)
                return
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
