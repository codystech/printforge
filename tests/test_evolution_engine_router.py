"""Focused orchestration and feature-gated router contract tests.

All generation and evaluation are deterministic in-memory fakes.  Persistence is
restricted to temporary directories; these tests never call PrintForge generation,
OpenSCAD, a model provider, or the production model library.
"""

from __future__ import annotations

import asyncio
import random
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import HTTPException

from evolution_lab.benchmarks import benchmark_catalog
from evolution_lab.adaptive import (
    MUTATION_CATALOG,
    adaptive_history_scope,
    mutation_weights,
    select_mutations,
)
from evolution_lab.config import EvolutionLabConfig
from evolution_lab.demo import DEMO_BANNER, DEMO_RUN_ID
from evolution_lab.engine import EvolutionAdapters, EvolutionEngine
from evolution_lab.router import create_router
from evolution_lab.schemas import CATEGORY_MAXIMA, CreateRunRequest, EvidenceLabel
from evolution_lab.store import EvolutionStore


def score_evidence(total: float) -> list[dict[str, Any]]:
    """Create deterministic evidence whose category sum is exactly ``total``."""

    remaining = float(total)
    items = []
    for category, maximum in CATEGORY_MAXIMA.items():
        awarded = min(maximum, max(0.0, remaining))
        remaining -= awarded
        items.append(
            {
                "category": category,
                "criterion": f"fake deterministic {category} check",
                "points_awarded": awarded,
                "points_possible": maximum,
                "label": EvidenceLabel.MEASURED.value,
                "source": "unit-test fake adapter",
                "summary": "Deterministic test evidence; no real generation occurred",
                "confidence": 1.0,
                "critical": False,
            }
        )
    if remaining > 0:
        raise ValueError("test score exceeds 100")
    return items


def route_endpoint(router: Any, path: str, method: str = "GET") -> Any:
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


class FakeEvolutionBackend:
    def __init__(
        self,
        *,
        baseline_score: float,
        candidate_scores: dict[str, float],
        failure_codes: dict[str, list[str]] | None = None,
        stop_on_first_generation_call: bool = False,
        fail_generation: bool = False,
        block_generation: bool = False,
        block_generation_labels: set[str] | None = None,
    ) -> None:
        self.baseline_score = baseline_score
        self.candidate_scores = candidate_scores
        self.failure_codes = failure_codes or {}
        self.stop_on_first_generation_call = stop_on_first_generation_call
        self.fail_generation = fail_generation
        self.block_generation = block_generation
        self.block_generation_labels = block_generation_labels or set()
        self.engine: EvolutionEngine | None = None
        self.generation_contexts: list[dict[str, Any]] = []

    async def load_source_model(self, source_model_id: str) -> dict[str, Any]:
        return {
            "scad": "// isolated test baseline\ncube([10, 10, 10]);\n",
            "meta": {"prompt": "fake source prompt", "source_model_id": source_model_id},
        }

    async def generate_candidate(self, parent_scad: str, context: dict[str, Any]) -> dict[str, Any]:
        self.generation_contexts.append(
            {
                "run_id": context["run"]["run_id"],
                "generation": context["generation"],
                "variant_label": context["variant_label"],
                "mutation": dict(context["mutation"]),
                "validated_spec": context["validated_spec"],
                "locked_constraints": list(context["locked_constraints"]),
                "printer_profile": dict(context["printer_profile"]),
                "material_profile": dict(context["material_profile"]),
                "attached_reference_roles": list(context["attached_reference_roles"]),
                "export_exclusions": list(context["export_exclusions"]),
                "parent_scad": parent_scad,
            }
        )
        if self.stop_on_first_generation_call and len(self.generation_contexts) == 1:
            assert self.engine is not None
            self.engine.stop_after_generation(context["run"]["run_id"])
        label = context["variant_label"]
        if self.block_generation or label in self.block_generation_labels:
            await asyncio.sleep(60)
        if self.fail_generation:
            raise RuntimeError("deterministic fake generation failure")
        return {
            "scad": f"{parent_scad}\n// fake variant {label}\n",
            "backend": "unit-test/fake",
            "estimated_cost": 0,
            "backend_calls": 0,
        }

    async def generate_initial_candidate(self, context: dict[str, Any]) -> dict[str, Any]:
        self.generation_contexts.append({
            "run_id": context["run"]["run_id"], "generation": 0,
            "variant_label": "GENERATION_ZERO", "validated_spec": context["validated_spec"],
            "locked_constraints": list(context["locked_constraints"]),
            "printer_profile": dict(context["printer_profile"]),
            "material_profile": dict(context["material_profile"]),
            "attached_reference_roles": list(context["attached_reference_roles"]),
            "export_exclusions": list(context["export_exclusions"]), "parent_scad": "",
        })
        if self.block_generation:
            await asyncio.sleep(60)
        if self.fail_generation:
            raise RuntimeError("deterministic fake generation-zero failure")
        return {"scad": "// generated from specification\ncube([12, 12, 12]);\n", "backend": "unit-test/fresh", "backend_calls": 0}

    async def evaluate_candidate(self, scad: str, context: dict[str, Any]) -> dict[str, Any]:
        if context.get("baseline"):
            score = self.baseline_score
            label = "BASELINE"
        else:
            label = context["variant_label"]
            score = self.candidate_scores.get(label, self.baseline_score)
        return {
            "evidence": score_evidence(score),
            "failure_codes": list(self.failure_codes.get(label, [])),
            "qa_results": [{"source": "unit-test", "status": "complete"}],
            "slicer_results": {"status": "unavailable", "reason": "fake adapter"},
            "artifacts": {"qa-report.json": "{}"},
            "estimated_cost": 0,
            "backend_calls": 0,
        }

    def adapters(self) -> EvolutionAdapters:
        return EvolutionAdapters(
            load_source_model=self.load_source_model,
            generate_initial_candidate=self.generate_initial_candidate,
            generate_candidate=self.generate_candidate,
            evaluate_candidate=self.evaluate_candidate,
            current_branch=lambda: "feature/evolution-training-lab",
        )


class RouterFeatureGateTests(unittest.TestCase):
    def test_disabled_bootstrap_does_not_create_data_root(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "must-not-exist"
            config = EvolutionLabConfig(data_root=data_root)

            router = create_router(
                config,
                EvolutionAdapters(current_branch=lambda: "feature/evolution-training-lab"),
                production_branch="main",
            )
            bootstrap = asyncio.run(
                route_endpoint(router, "/training-lab/api/bootstrap")()
            )

            self.assertFalse(data_root.exists())
            self.assertFalse(bootstrap["enabled"])
            self.assertFalse(bootstrap["training_lab_enabled"])
            self.assertEqual(bootstrap["runs"], [])
            self.assertIsNone(bootstrap["active_run"])
            self.assertIsNone(bootstrap["demo_run_id"])
            self.assertFalse(bootstrap["actual_training_performed"])
            self.assertEqual(bootstrap["current_branch"], "feature/evolution-training-lab")
            self.assertEqual(bootstrap["production_branch"], "main")

            with self.assertRaises(HTTPException) as raised:
                asyncio.run(route_endpoint(router, "/training-lab/api/runs")())
            self.assertEqual(raised.exception.status_code, 403)
            self.assertEqual(raised.exception.detail, "Training Lab is disabled")
            self.assertFalse(data_root.exists())

    def test_enabled_bootstrap_persists_an_isolated_labelled_demo(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            data_root = Path(tempdir) / "enabled-lab"
            config = EvolutionLabConfig(training_lab_enabled=True, data_root=data_root)
            router = create_router(
                config,
                EvolutionAdapters(current_branch=lambda: "feature/evolution-training-lab"),
                production_branch="main",
            )

            bootstrap = asyncio.run(
                route_endpoint(router, "/training-lab/api/bootstrap")()
            )
            demo = EvolutionStore(data_root).get_run(DEMO_RUN_ID)

            self.assertTrue(bootstrap["enabled"])
            self.assertEqual(bootstrap["demo_run_id"], DEMO_RUN_ID)
            self.assertEqual(len(bootstrap["runs"]), 1)
            self.assertEqual(bootstrap["runs"][0]["run_id"], DEMO_RUN_ID)
            self.assertTrue(bootstrap["runs"][0]["demo"])
            self.assertTrue(demo["demo"])
            self.assertEqual(demo["demo_banner"], DEMO_BANNER)
            self.assertFalse(demo["summary"]["actual_training_performed"])
            self.assertFalse(bootstrap["capabilities"]["actual_model_training"])

    def test_actual_training_endpoint_is_honest_even_when_flags_are_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            config = EvolutionLabConfig(
                training_lab_enabled=True,
                actual_training_enabled=True,
                training_enabled=True,
                training_backend="fake-configured-backend",
                data_root=Path(tempdir) / "training-lab",
            )
            router = create_router(config, EvolutionAdapters(current_branch=lambda: "test-branch"))

            result = asyncio.run(
                route_endpoint(router, "/training-lab/api/actual-training", "POST")()
            )

            self.assertFalse(result["supported"])
            self.assertTrue(result["enabled"])
            self.assertFalse(result["executed"])
            self.assertFalse(result["evaluated"])
            self.assertFalse(result["deployed"])
            self.assertIn("No configured PrintForge backend", result["reason"])

    def test_invalid_and_deleted_starting_models_return_useful_errors(self) -> None:
        async def load_source(model_id: str) -> dict[str, Any]:
            if model_id == "not-a-public-id":
                raise ValueError("invalid public model ID")
            raise FileNotFoundError(model_id)

        with tempfile.TemporaryDirectory() as tempdir:
            config = EvolutionLabConfig(
                evolution_enabled=True, training_lab_enabled=True,
                data_root=Path(tempdir) / "router-errors",
            )
            router = create_router(config, EvolutionAdapters(load_source_model=load_source))
            endpoint = route_endpoint(router, "/training-lab/api/runs", "POST")
            base = EvolutionEngineContractTests.run_request()
            base["source_model_id"] = "not-a-public-id"
            with self.assertRaises(HTTPException) as invalid:
                asyncio.run(endpoint(CreateRunRequest(**base)))
            self.assertEqual(invalid.exception.status_code, 400)
            self.assertIn("invalid public model ID", invalid.exception.detail)

            base["source_model_id"] = "deadbeefcafe"
            with self.assertRaises(HTTPException) as deleted:
                asyncio.run(endpoint(CreateRunRequest(**base)))
            self.assertEqual(deleted.exception.status_code, 404)
            self.assertIn("no longer exists", deleted.exception.detail)


class EvolutionEngineContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def make_engine(
        self,
        fake: FakeEvolutionBackend,
    ) -> tuple[EvolutionEngine, EvolutionStore]:
        store = EvolutionStore(Path(self.tempdir.name) / "engine-store")
        config = EvolutionLabConfig(
            evolution_enabled=True,
            training_lab_enabled=True,
            memory_learning_enabled=False,
            data_root=store.root,
        )
        engine = EvolutionEngine(store, config, fake.adapters())
        fake.engine = engine
        return engine, store

    @staticmethod
    def run_request(*, maximum_generations: int = 5) -> dict[str, Any]:
        return {
            "source_model_id": "fake-source-model",
            "source_prompt": "controlled fake A/B test",
            "validated_spec": "Preserve the body, slider, text, and all hard locks.",
            "printer_profile": {
                "name": "Unit Test Printer PLA",
                "printer": "Unit Test Printer",
                "material": "PLA",
                "nozzle": 0.4,
                "layer": 0.2,
            },
            "material_profile": {"material": "PLA", "layer_height": 0.2},
            "locked_constraints": [
                {"type": "hard", "name": "body geometry"},
                {"type": "hard", "name": "SIX SEVEN text"},
            ],
            "attached_reference_roles": [{"id": "ref-1", "role": "reference"}],
            "export_exclusions": ["ref-1"],
            "active_backend": "unit-test/fake",
            "limits": {
                "variants_per_generation": 2,
                "maximum_generations": maximum_generations,
                "target_reward_score": 100,
                "maximum_runtime_seconds": 3600,
                "maximum_estimated_cost": 100,
                "no_improvement_limit": 20,
                "mutation_strength": 0.25,
                "exploration_rate": 0.15,
                "benchmark_mode": False,
                "physical_validation_required": False,
            },
            "initial_mutations": [
                {
                    "mutation_type": "spinner_clearance",
                    "parameter": "spinner_clearance",
                    "original_value": 0.4,
                    "mutated_value": 0.45,
                    "expected_benefit": "reduce binding",
                    "reason": "controlled clearance test",
                },
                {
                    "mutation_type": "spinner_retention",
                    "parameter": "retention_lip",
                    "original_value": 0.5,
                    "mutated_value": 0.6,
                    "expected_benefit": "improve retention",
                    "reason": "controlled retention test",
                },
            ],
            "auto_start": False,
        }

    async def test_ab_candidates_share_parent_constraints_and_preserve_loser(self) -> None:
        fake = FakeEvolutionBackend(
            baseline_score=60,
            candidate_scores={"A": 90, "B": 80},
        )
        engine, store = self.make_engine(fake)
        created = await engine.create_run(self.run_request())
        run_id = created["run_id"]
        baseline_id = created["baseline_candidate_id"]
        self.assertEqual(created["run_mode"], "evolve_existing")
        self.assertEqual(created["source_model_id"], "fake-source-model")

        await engine._generation(run_id, 1)

        snapshot = engine.snapshot(run_id)
        variants = [candidate for candidate in snapshot["candidates"] if candidate["generation"] == 1]
        self.assertEqual(len(variants), 2)
        self.assertEqual({candidate["variant_label"] for candidate in variants}, {"A", "B"})
        self.assertEqual({candidate["parent_candidate_id"] for candidate in variants}, {baseline_id})
        self.assertEqual({candidate["current_best_parent_id"] for candidate in variants}, {baseline_id})
        self.assertEqual({candidate["selection_status"] for candidate in variants}, {"winner", "loser"})

        context_a, context_b = fake.generation_contexts
        for field in (
            "validated_spec",
            "locked_constraints",
            "printer_profile",
            "material_profile",
            "attached_reference_roles",
            "export_exclusions",
            "parent_scad",
        ):
            with self.subTest(field=field):
                self.assertEqual(context_a[field], context_b[field])

        winner = next(candidate for candidate in variants if candidate["selection_status"] == "winner")
        loser = next(candidate for candidate in variants if candidate["selection_status"] == "loser")
        self.assertEqual(snapshot["current_best_candidate_id"], winner["candidate_id"])
        self.assertEqual(snapshot["current_best_score"], 90)
        self.assertEqual(
            store.candidate_artifact(run_id, loser["candidate_id"], "model.scad").read_text(
                encoding="utf-8"
            ).splitlines()[-1],
            "// fake variant B",
        )
        self.assertIn(
            loser["candidate_id"],
            snapshot["generation_results"][0]["candidate_ids"],
        )

    async def test_both_regressions_preserve_current_best_and_audit_both(self) -> None:
        fake = FakeEvolutionBackend(
            baseline_score=90,
            candidate_scores={"A": 80, "B": 85},
        )
        engine, store = self.make_engine(fake)
        created = await engine.create_run(self.run_request())
        run_id = created["run_id"]
        baseline_id = created["baseline_candidate_id"]

        await engine._generation(run_id, 1)

        snapshot = engine.snapshot(run_id)
        generation = snapshot["generation_results"][0]
        variants = [candidate for candidate in snapshot["candidates"] if candidate["generation"] == 1]
        self.assertEqual(snapshot["current_best_candidate_id"], baseline_id)
        self.assertEqual(snapshot["current_best_score"], 90)
        self.assertTrue(generation["current_best_preserved"])
        self.assertFalse(generation["improved_current_best"])
        self.assertIsNone(generation["winner_candidate_id"])
        self.assertEqual({candidate["selection_status"] for candidate in variants}, {"loser"})
        self.assertEqual(len(variants), 2)
        for candidate in variants:
            self.assertTrue(
                store.candidate_artifact(run_id, candidate["candidate_id"], "model.scad").is_file()
            )

    async def test_mutation_outcomes_persist_and_feed_later_run_selection(self) -> None:
        fake = FakeEvolutionBackend(
            baseline_score=60,
            candidate_scores={"A": 90, "B": 80},
        )
        engine, store = self.make_engine(fake)
        first_request = self.run_request()
        first_request["initial_mutations"] = [dict(MUTATION_CATALOG[2]), dict(MUTATION_CATALOG[0])]
        first = await engine.create_run(first_request)

        await engine._generation(first["run_id"], 1)

        snapshot = engine.snapshot(first["run_id"])
        variants = [candidate for candidate in snapshot["candidates"] if candidate["generation"] == 1]
        outcomes = store.list_mutation_outcomes(first["adaptive_history_scope"])
        by_candidate = {item["candidate_id"]: item for item in outcomes}
        self.assertEqual(set(by_candidate), {candidate["candidate_id"] for candidate in variants})
        for candidate in variants:
            outcome = by_candidate[candidate["candidate_id"]]
            self.assertEqual(outcome["run_id"], first["run_id"])
            self.assertEqual(outcome["generation"], 1)
            self.assertEqual(outcome["mutation_type"], candidate["mutation"]["mutation_type"])
            self.assertEqual(outcome["score_delta"], candidate["score"]["total"] - 60)
            self.assertEqual(outcome["success"], candidate["selection_status"] == "winner")
            self.assertEqual(outcome["selection_status"], candidate["selection_status"])
            self.assertTrue(outcome["eligible"])
            self.assertEqual(outcome["adaptive_scope"], first["adaptive_history_scope"])

        learned_weights = mutation_weights(outcomes)
        neutral_weights = mutation_weights([])
        self.assertGreater(learned_weights["wall_thickness"], neutral_weights["wall_thickness"])
        learned_counts = 0
        neutral_counts = 0
        for seed in range(500):
            if select_mutations(outcomes, n=1, exploration_rate=0, rng=random.Random(seed))[0]["mutation_type"] == "wall_thickness":
                learned_counts += 1
            if select_mutations([], n=1, exploration_rate=0, rng=random.Random(seed))[0]["mutation_type"] == "wall_thickness":
                neutral_counts += 1
        self.assertGreater(learned_counts, neutral_counts)

        reopened = EvolutionStore(store.root)
        self.assertEqual(
            {item["candidate_id"] for item in reopened.list_mutation_outcomes(first["adaptive_history_scope"])},
            set(by_candidate),
        )
        self.assertEqual(snapshot["next_mutation_generation"], 2)
        self.assertEqual(len(snapshot["next_mutation_proposals"]), 2)

        second_request = self.run_request()
        second_request["initial_mutations"] = []
        second_request["limits"]["random_seed"] = 17
        second = await engine.create_run(second_request)
        second_run = store.get_run(second["run_id"])
        with patch("evolution_lab.engine.select_mutations", wraps=select_mutations) as selector:
            selected = engine._mutations(second_run, 1)

        history = selector.call_args.args[0]
        self.assertEqual(len(selected), 2)
        self.assertTrue(any(item["run_id"] == first["run_id"] for item in history))
        self.assertEqual(selector.call_args.kwargs["exploration_rate"], 0.15)

        unrelated_request = self.run_request()
        unrelated_request["initial_mutations"] = []
        unrelated_request["printer_profile"] = {
            **unrelated_request["printer_profile"], "name": "Unit Test Printer PETG", "material": "PETG",
        }
        unrelated_request["material_profile"] = {"material": "PETG", "layer_height": 0.2}
        unrelated = await engine.create_run(unrelated_request)
        with patch("evolution_lab.engine.select_mutations", wraps=select_mutations) as unrelated_selector:
            engine._mutations(store.get_run(unrelated["run_id"]), 1)
        self.assertEqual(unrelated_selector.call_args.args[0], [])

        proposed = snapshot["next_mutation_proposals"]
        fake.generation_contexts.clear()
        await engine._generation(first["run_id"], 2)
        self.assertEqual(
            [context["mutation"] for context in fake.generation_contexts],
            proposed,
        )

    async def test_hard_lock_rejection_cannot_win_despite_higher_score(self) -> None:
        fake = FakeEvolutionBackend(
            baseline_score=50,
            candidate_scores={"A": 99, "B": 70},
            failure_codes={"A": ["broken_hard_lock"]},
        )
        engine, store = self.make_engine(fake)
        request = self.run_request()
        request["initial_mutations"] = [dict(MUTATION_CATALOG[0]), dict(MUTATION_CATALOG[2])]
        created = await engine.create_run(request)
        run_id = created["run_id"]

        await engine._generation(run_id, 1)

        snapshot = engine.snapshot(run_id)
        variants = {
            candidate["variant_label"]: candidate
            for candidate in snapshot["candidates"]
            if candidate["generation"] == 1
        }
        invalid = variants["A"]
        winner = variants["B"]
        self.assertEqual(invalid["score"]["total"], 99)
        self.assertTrue(invalid["score"]["hard_rejected"])
        self.assertIn("broken_hard_lock", invalid["score"]["hard_rejection_reasons"])
        self.assertEqual(invalid["selection_status"], "rejected")
        self.assertEqual(winner["selection_status"], "winner")
        self.assertEqual(snapshot["current_best_candidate_id"], winner["candidate_id"])
        self.assertNotEqual(snapshot["current_best_candidate_id"], invalid["candidate_id"])

        outcomes = store.list_mutation_outcomes(created["adaptive_history_scope"])
        by_candidate = {item["candidate_id"]: item for item in outcomes}
        rejected_outcome = by_candidate[invalid["candidate_id"]]
        winner_outcome = by_candidate[winner["candidate_id"]]
        self.assertEqual(rejected_outcome["score_delta"], 49)
        self.assertFalse(rejected_outcome["eligible"])
        self.assertFalse(rejected_outcome["success"])
        self.assertEqual(rejected_outcome["selection_status"], "rejected")
        self.assertTrue(winner_outcome["eligible"])
        self.assertEqual(winner_outcome["selection_status"], "winner")
        weights = mutation_weights(outcomes)
        self.assertLess(weights["fit_clearance"], weights["wall_thickness"])

    async def test_promotion_requires_current_checked_best_but_revoke_remains_independent(self) -> None:
        fake = FakeEvolutionBackend(
            baseline_score=50,
            candidate_scores={"A": 90, "B": 70},
        )
        engine, store = self.make_engine(fake)
        created = await engine.create_run(self.run_request())
        await engine._generation(created["run_id"], 1)
        variants = {
            item["variant_label"]: item
            for item in engine.snapshot(created["run_id"])["candidates"]
            if item["generation"] == 1
        }
        promoted: list[str] = []
        revoked: list[str] = []
        adapters = fake.adapters()
        adapters.promote_exemplar = lambda _scad, _name, _spec, _score, candidate_id: (
            promoted.append(candidate_id) or "abc123def456"
        )
        adapters.revoke_exemplar = lambda candidate_id: revoked.append(candidate_id) or 1
        router = create_router(
            engine.config,
            adapters,
            store=store,
            production_branch="main",
        )
        promote = route_endpoint(
            router,
            "/training-lab/api/candidates/{candidate_id}/promote-exemplar",
            "POST",
        )
        revoke = route_endpoint(
            router,
            "/training-lab/api/candidates/{candidate_id}/revoke-exemplar",
            "POST",
        )

        with self.assertRaises(HTTPException) as not_best:
            await promote(variants["B"]["candidate_id"])
        self.assertEqual(not_best.exception.status_code, 409)
        self.assertIn("current winning candidate", not_best.exception.detail)

        store.update_candidate(
            created["run_id"],
            variants["A"]["candidate_id"],
            lambda row: row.update({"required_checks_passed": False}),
        )
        with self.assertRaises(HTTPException) as unchecked:
            await promote(variants["A"]["candidate_id"])
        self.assertEqual(unchecked.exception.status_code, 409)
        self.assertIn("not passed required checks", unchecked.exception.detail)

        store.update_candidate(
            created["run_id"],
            variants["A"]["candidate_id"],
            lambda row: row.update({"required_checks_passed": True}),
        )
        result = await promote(variants["A"]["candidate_id"])
        self.assertEqual(result["library_model_id"], "abc123def456")
        self.assertEqual(promoted, [variants["A"]["candidate_id"]])

        engine.restore_candidate(created["run_id"], variants["B"]["candidate_id"])
        self.assertNotEqual(
            engine.snapshot(created["run_id"])["current_best_candidate_id"],
            variants["A"]["candidate_id"],
        )
        self.assertEqual(await revoke(variants["A"]["candidate_id"]), {"revoked": 1})
        self.assertEqual(revoked, [variants["A"]["candidate_id"]])

    async def test_stop_request_finishes_both_variants_then_stops_before_next_generation(self) -> None:
        fake = FakeEvolutionBackend(
            baseline_score=10,
            candidate_scores={"A": 20, "B": 15},
            stop_on_first_generation_call=True,
        )
        engine, _ = self.make_engine(fake)
        created = await engine.create_run(self.run_request(maximum_generations=5))
        run_id = created["run_id"]

        await engine._run(run_id)

        snapshot = engine.snapshot(run_id)
        variants = [candidate for candidate in snapshot["candidates"] if candidate["generation"] > 0]
        self.assertEqual(snapshot["status"], "complete")
        self.assertTrue(snapshot["stop_after_current_generation"])
        self.assertEqual(snapshot["current_generation"], 1)
        self.assertEqual(len(snapshot["generation_results"]), 1)
        self.assertEqual(len(variants), 2)
        self.assertEqual([context["variant_label"] for context in fake.generation_contexts], ["A", "B"])
        self.assertFalse(any(candidate["generation"] > 1 for candidate in variants))
        event_types = [event["event_type"] for event in snapshot["events"]]
        self.assertIn("stop_requested", event_types)
        self.assertIn("winner_selected", event_types)
        self.assertEqual(event_types[-1], "run_completed")

    async def test_create_from_spec_generates_and_checkpoints_generation_zero_without_source(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=88, candidate_scores={})
        engine, store = self.make_engine(fake)
        request = self.run_request()
        request.update({"run_mode": "create_from_spec", "source_model_id": None})
        request["limits"]["target_reward_score"] = 88

        created = await engine.create_run(request)
        self.assertIsNone(created["source_model_id"])
        self.assertEqual(created["run_mode"], "create_from_spec")
        self.assertEqual(created["candidates"][0]["status"], "pending")

        await engine._run(created["run_id"])
        snapshot = engine.snapshot(created["run_id"])
        generation_zero = next(item for item in snapshot["candidates"] if item["generation"] == 0)
        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["stop_reason"], "target_reached")
        self.assertEqual(snapshot["current_best_candidate_id"], generation_zero["candidate_id"])
        self.assertTrue(store.candidate_artifact(created["run_id"], generation_zero["candidate_id"], "model.scad").is_file())
        self.assertEqual(len(snapshot["generation_results"]), 0)

    async def test_generation_zero_failure_is_preserved_and_explained(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=0, candidate_scores={}, fail_generation=True)
        engine, _ = self.make_engine(fake)
        request = self.run_request()
        request.update({"run_mode": "create_from_spec", "source_model_id": None})
        created = await engine.create_run(request)

        await engine._run(created["run_id"])
        snapshot = engine.snapshot(created["run_id"])
        attempt = snapshot["candidates"][0]
        self.assertEqual(snapshot["status"], "failed")
        self.assertEqual(snapshot["stop_reason"], "generation_zero_failed")
        self.assertEqual(attempt["status"], "failed")
        self.assertIn("generation-zero failure", attempt["failure_reason"])
        self.assertEqual(attempt["generation_prompt"], request["validated_spec"])

    async def test_cadquery_generation_uses_dedicated_evaluator_and_persists_block(self) -> None:
        calls = {"legacy": 0, "cadquery": 0}

        async def generate(context):
            return {
                "model_format": "cadquery-v1",
                "source": "PARAMETERS = {}\ndef build(params, assets):\n    return {}\n",
                "backend": "unit-test/cadquery",
                "backend_calls": 0,
            }

        async def legacy(source, context):
            calls["legacy"] += 1
            raise AssertionError("CadQuery source reached the legacy evaluator")

        async def cadquery(source, context):
            calls["cadquery"] += 1
            self.assertEqual(context["model_format"], "cadquery-v1")
            return {
                "model_format": "cadquery-v1",
                "evidence": [],
                "failure_codes": ["cadquery_runtime_unavailable"],
                "slicer_results": {"status": "failed", "failure_codes": ["slicer_unavailable"]},
                "promotion_blocked": True,
                "bambuddy_send_blocked": True,
                "artifacts": {},
            }

        store = EvolutionStore(Path(self.tempdir.name) / "cadquery-engine-store")
        engine = EvolutionEngine(store, EvolutionLabConfig(
            evolution_enabled=True, training_lab_enabled=True, data_root=store.root,
        ), EvolutionAdapters(
            generate_initial_candidate=generate,
            evaluate_candidate=legacy,
            evaluate_cadquery_candidate=cadquery,
        ))
        request = self.run_request()
        request.update({"run_mode": "create_from_spec", "source_model_id": None})
        created = await engine.create_run(request)
        await engine._run(created["run_id"])
        snapshot = engine.snapshot(created["run_id"])
        candidate = snapshot["candidates"][0]
        self.assertEqual(calls, {"legacy": 0, "cadquery": 1})
        self.assertEqual(candidate["model_format"], "cadquery-v1")
        self.assertTrue(candidate["promotion_blocked"])
        self.assertTrue(candidate["bambuddy_send_blocked"])
        self.assertIn("cadquery_runtime_unavailable", candidate["failure_reasons"])
        self.assertTrue(store.candidate_artifact(
            created["run_id"], candidate["candidate_id"], "model.py"
        ).is_file())

    async def test_iteration_limit_stops_after_configured_bound(self) -> None:
        fake = FakeEvolutionBackend(
            baseline_score=10, candidate_scores={"A": 20, "B": 15},
            failure_codes={"A": ["soft_quality_gap"], "B": ["soft_quality_gap"]},
        )
        engine, _ = self.make_engine(fake)
        request = self.run_request()
        request["limits"].update({"maximum_iterations": 1, "target_reward_score": None})
        created = await engine.create_run(request)
        await engine._run(created["run_id"])
        snapshot = engine.snapshot(created["run_id"])
        self.assertEqual(snapshot["current_generation"], 1)
        self.assertEqual(snapshot["stop_reason"], "iteration_limit")
        self.assertEqual(len(snapshot["generation_results"]), 1)

    async def test_repeated_generation_failures_stop_at_limit(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=10, candidate_scores={}, fail_generation=True)
        engine, _ = self.make_engine(fake)
        request = self.run_request()
        request["limits"].update({
            "maximum_iterations": 10, "target_reward_score": None,
            "repeated_generation_failure_limit": 2, "no_improvement_limit": 10,
        })
        created = await engine.create_run(request)
        await engine._run(created["run_id"])
        snapshot = engine.snapshot(created["run_id"])
        self.assertEqual(snapshot["stop_reason"], "repeated_generation_failures")
        self.assertEqual(snapshot["consecutive_generation_failures"], 2)
        self.assertEqual(len(snapshot["generation_results"]), 2)
        self.assertEqual(len([item for item in snapshot["candidates"] if item["status"] == "failed"]), 4)

    async def test_no_improvement_stop_preserves_successful_baseline(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=90, candidate_scores={"A": 80, "B": 85})
        engine, _ = self.make_engine(fake)
        request = self.run_request()
        request["limits"].update({"maximum_iterations": 10, "target_reward_score": None, "no_improvement_limit": 2})
        created = await engine.create_run(request)
        baseline_id = created["baseline_candidate_id"]
        await engine._run(created["run_id"])
        snapshot = engine.snapshot(created["run_id"])
        self.assertEqual(snapshot["stop_reason"], "no_improvement")
        self.assertEqual(snapshot["current_best_candidate_id"], baseline_id)
        self.assertEqual(len(snapshot["generation_results"]), 2)

    async def test_target_score_completes_successfully(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=10, candidate_scores={"A": 94, "B": 70})
        engine, _ = self.make_engine(fake)
        request = self.run_request()
        request["limits"].update({"maximum_iterations": 10, "target_reward_score": 92})
        created = await engine.create_run(request)
        await engine._run(created["run_id"])
        snapshot = engine.snapshot(created["run_id"])
        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["stop_reason"], "target_reached")
        self.assertEqual(snapshot["current_best_score"], 94)

    async def test_immediate_cancellation_preserves_in_progress_candidate(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=10, candidate_scores={"A": 20, "B": 15}, block_generation=True)
        engine, store = self.make_engine(fake)
        created = await engine.create_run(self.run_request())
        run_id = created["run_id"]
        engine.start(run_id)
        for _ in range(100):
            if engine.snapshot(run_id).get("active_candidate_id"):
                break
            await asyncio.sleep(0.01)
        engine.cancel(run_id)
        task = engine._tasks[run_id]
        await asyncio.wait_for(task, timeout=2)
        snapshot = engine.snapshot(run_id)
        self.assertEqual(snapshot["status"], "cancelled")
        self.assertEqual(snapshot["stop_reason"], "user_cancelled")
        cancelled = [item for item in snapshot["candidates"] if item["status"] == "cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["failure_reasons"], ["user_cancelled"])
        outcomes = store.list_mutation_outcomes(snapshot["adaptive_history_scope"])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["selection_status"], "cancelled")
        self.assertFalse(outcomes[0]["eligible"])
        self.assertLessEqual(outcomes[0]["score_delta"], 0)

    async def test_cancellation_records_completed_and_active_attempts_before_rethrow(self) -> None:
        fake = FakeEvolutionBackend(
            baseline_score=10,
            candidate_scores={"A": 20, "B": 15},
            block_generation_labels={"B"},
        )
        engine, store = self.make_engine(fake)
        created = await engine.create_run(self.run_request())
        engine.start(created["run_id"])
        for _ in range(200):
            active = engine.snapshot(created["run_id"]).get("active_candidate_id")
            if active:
                candidate = store.get_candidate(created["run_id"], active)
                if candidate.get("variant_label") == "B":
                    break
            await asyncio.sleep(0.01)
        else:
            self.fail("variant B did not become active")

        engine.cancel(created["run_id"])
        await asyncio.wait_for(engine._tasks[created["run_id"]], timeout=2)
        snapshot = engine.snapshot(created["run_id"])
        outcomes = store.list_mutation_outcomes(snapshot["adaptive_history_scope"])
        by_status = {item["selection_status"]: item for item in outcomes}

        self.assertEqual(set(by_status), {"loser", "cancelled"})
        self.assertTrue(by_status["loser"]["eligible"])
        self.assertFalse(by_status["cancelled"]["eligible"])
        self.assertLessEqual(by_status["cancelled"]["score_delta"], 0)

    async def test_candidate_restore_branch_and_guarded_delete_preserve_versions(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=10, candidate_scores={"A": 90, "B": 80})
        engine, store = self.make_engine(fake)
        created = await engine.create_run(self.run_request())
        run_id = created["run_id"]
        await engine._generation(run_id, 1)
        variants = [item for item in engine.snapshot(run_id)["candidates"] if item["generation"] == 1]
        winner = next(item for item in variants if item["selection_status"] == "winner")
        loser = next(item for item in variants if item["selection_status"] == "loser")

        restored = engine.restore_candidate(run_id, loser["candidate_id"])
        self.assertEqual(restored["current_best_candidate_id"], loser["candidate_id"])
        self.assertTrue(any(cp["checkpoint_type"] == "restored_best" for cp in restored["checkpoints"]))

        branched = await engine.branch_candidate(run_id, loser["candidate_id"])
        self.assertNotEqual(branched["run_id"], run_id)
        self.assertEqual(branched["source_run_id"], run_id)
        self.assertEqual(branched["source_candidate_id"], loser["candidate_id"])

        after_delete = engine.delete_candidate(run_id, winner["candidate_id"])
        self.assertFalse(any(item["candidate_id"] == winner["candidate_id"] for item in after_delete["candidates"]))
        with self.assertRaises(ValueError):
            engine.delete_candidate(run_id, loser["candidate_id"])
        self.assertTrue(store.candidate_artifact(run_id, loser["candidate_id"], "model.scad").is_file())

    async def test_spec_created_branches_recompute_scope_without_cross_spec_bleed(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=60, candidate_scores={})
        engine, _ = self.make_engine(fake)

        async def create_branch(spec: str) -> dict:
            request = self.run_request()
            request.update({
                "run_mode": "create_from_spec",
                "source_model_id": None,
                "validated_spec": spec,
            })
            created = await engine.create_run(request)
            await engine._generation_zero(created["run_id"])
            completed = engine.snapshot(created["run_id"])
            return await engine.branch_candidate(
                created["run_id"], completed["current_best_candidate_id"]
            )

        first = await create_branch("Make a fitted PLA phone stand")
        second = await create_branch("Make a sealed PLA electronics box")

        self.assertIsNone(first["source_model_id"])
        self.assertEqual(first["adaptive_history_scope"], adaptive_history_scope(first))
        self.assertEqual(second["adaptive_history_scope"], adaptive_history_scope(second))
        self.assertEqual(first["adaptive_history_scope"]["design"]["kind"], "validated_spec")
        self.assertNotEqual(first["adaptive_history_scope"], second["adaptive_history_scope"])

    async def test_runtime_limit_cancels_active_work_and_completes_with_reason(self) -> None:
        fake = FakeEvolutionBackend(baseline_score=10, candidate_scores={"A": 20, "B": 15}, block_generation=True)
        engine, _ = self.make_engine(fake)
        request = self.run_request()
        request["limits"]["maximum_runtime_seconds"] = 0.05
        created = await engine.create_run(request)
        engine.start(created["run_id"])
        await asyncio.wait_for(engine._tasks[created["run_id"]], timeout=2)
        snapshot = engine.snapshot(created["run_id"])
        self.assertEqual(snapshot["status"], "complete")
        self.assertEqual(snapshot["stop_reason"], "maximum_runtime")
        self.assertTrue(any(item["status"] == "cancelled" for item in snapshot["candidates"]))


class BenchmarkCatalogTests(unittest.TestCase):
    def test_catalog_has_all_twenty_versioned_benchmarks_with_required_shape(self) -> None:
        catalog = benchmark_catalog()
        required_keys = {
            "benchmark_id",
            "category",
            "prompt",
            "validated_spec",
            "expected_features",
            "required_dimensions",
            "printer_profile",
            "material_profile",
            "hard_locks",
            "pass_fail_conditions",
            "known_traps",
            "expected_export_parts",
            "minimum_acceptable_score",
            "critical_failure_conditions",
            "result",
        }

        self.assertEqual(len(catalog), 20)
        self.assertEqual(len({item["benchmark_id"] for item in catalog}), 20)
        for item in catalog:
            with self.subTest(benchmark_id=item["benchmark_id"]):
                self.assertTrue(required_keys.issubset(item))
                self.assertTrue(item["prompt"])
                self.assertTrue(item["validated_spec"])
                self.assertTrue(item["pass_fail_conditions"])
                self.assertTrue(item["known_traps"])
                self.assertIn("broken_hard_lock", item["critical_failure_conditions"])
                self.assertIn("reference_export_leakage", item["critical_failure_conditions"])
                self.assertGreaterEqual(item["minimum_acceptable_score"], 0)
                self.assertLessEqual(item["minimum_acceptable_score"], 100)
                self.assertIsNone(item["result"])


class TrainingLabUiContractTests(unittest.TestCase):
    def test_promotion_and_revoke_controls_use_independent_persisted_state(self) -> None:
        source = (
            Path(__file__).parents[1] / "static" / "training-lab" / "training-lab.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "candidateId(candidate) === state.run?.current_best_candidate_id",
            source,
        )
        self.assertIn("candidate?.required_checks_passed === true", source)
        self.assertIn("candidate?.model_format !== 'cadquery-v1' || cadQueryPromotionSupported()", source)
        self.assertIn("add('CadQuery promotion unavailable'", source)
        self.assertIn("if (canPromoteExemplar(candidate))", source)
        self.assertIn("if (promoted) {\n    add('Remove from production'", source)
        self.assertIn("model?.source_candidate_id === candidateId(candidate)", source)
        self.assertIn("button.disabled = true", source)
        self.assertIn("button.textContent = 'Remove from production'", source)

    def test_physical_validation_uses_candidate_slice_evidence_and_backend_roles(self) -> None:
        source = (
            Path(__file__).parents[1] / "static" / "training-lab" / "training-lab.js"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "candidate?.slicer_profile_fingerprint ?? candidate?.slicer_results?.profile_fingerprint",
            source,
        )
        self.assertIn("part?.export_role === 'printable'", source)
        self.assertNotIn("['printable', 'assembly'].includes(part?.export_role)", source)
        self.assertIn("slicer_profile: form.dataset.slicerProfileFingerprint", source)
        self.assertNotIn("slicer_profile: state.run.slicer_profile ?? null", source)


if __name__ == "__main__":
    unittest.main()
