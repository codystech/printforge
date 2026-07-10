"""Focused tests for lab-only adaptive mutation selection and persistence."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolution_lab.adaptive import (
    MUTATION_CATALOG,
    adaptive_history_scope,
    mutation_weights,
    outcome_record,
    select_mutations,
)
from evolution_lab.store import MUTATION_OUTCOME_MANIFEST, EvolutionStore


class AdaptiveMutationTests(unittest.TestCase):
    def test_success_and_score_delta_raise_strategy_weight(self) -> None:
        history = [
            {"mutation_type": "wall_thickness", "success": True, "score_delta": 8}
            for _ in range(6)
        ] + [
            {"mutation_type": "fit_clearance", "success": False, "score_delta": -4}
            for _ in range(6)
        ]

        weights = mutation_weights(history)

        self.assertGreater(weights["wall_thickness"], weights["fit_clearance"])
        self.assertEqual(set(weights), {item["mutation_type"] for item in MUTATION_CATALOG})

    def test_malformed_history_and_limits_fall_back_safely(self) -> None:
        history = [
            None,
            {"mutation_type": "unknown", "success": True, "score_delta": 100},
            {"mutation_type": "wall_thickness", "success": "yes", "score_delta": "invalid"},
            {"mutation_type": "fit_clearance", "success": True, "score_delta": float("nan")},
            {"mutation_type": "base_adhesion", "success": True, "score_delta": 1e308},
        ]

        weights = mutation_weights(history, prior=0)
        selected = select_mutations(
            history,
            n=100,
            exploration_rate=float("nan"),
            rng=random.Random(7),
        )

        self.assertTrue(all(weight > 0 for weight in weights.values()))
        self.assertTrue(all(weight < 3 for weight in weights.values()))
        self.assertEqual(len(selected), len(MUTATION_CATALOG))
        self.assertEqual(len({item["mutation_type"] for item in selected}), len(selected))

    def test_exploitation_favors_successful_history_without_eliminating_others(self) -> None:
        history = [
            {"mutation_type": "wall_thickness", "success": True, "score_delta": 8}
            for _ in range(8)
        ] + [
            {"mutation_type": "fit_clearance", "success": False, "score_delta": -4}
            for _ in range(8)
        ]
        counts = {item["mutation_type"]: 0 for item in MUTATION_CATALOG}

        for seed in range(500):
            selected = select_mutations(
                history,
                n=1,
                exploration_rate=0,
                rng=random.Random(seed),
            )
            counts[selected[0]["mutation_type"]] += 1

        self.assertGreater(counts["wall_thickness"], counts["fit_clearance"])
        self.assertGreater(sum(counts.values()) - counts["wall_thickness"], 0)

    def test_ineligible_high_score_never_receives_positive_credit(self) -> None:
        history = [
            outcome_record(
                MUTATION_CATALOG[0],
                success=True,
                eligible=False,
                hard_rejected=True,
                score_delta=49,
                run_id="run_rejected",
                candidate_id="candidate_rejected",
                generation=1,
            ),
            outcome_record(
                MUTATION_CATALOG[2],
                success=True,
                eligible=True,
                score_delta=20,
                run_id="run_valid",
                candidate_id="candidate_valid",
                generation=1,
            ),
        ]

        weights = mutation_weights(history)
        counts = {"fit_clearance": 0, "wall_thickness": 0}
        for seed in range(500):
            selected = select_mutations(
                history,
                n=1,
                exploration_rate=0,
                rng=random.Random(seed),
            )[0]["mutation_type"]
            if selected in counts:
                counts[selected] += 1

        self.assertFalse(history[0]["success"])
        self.assertLess(weights["fit_clearance"], weights["wall_thickness"])
        self.assertLess(counts["fit_clearance"], counts["wall_thickness"])

    def test_scope_is_stable_and_separates_design_material_and_printer(self) -> None:
        base = {
            "source_model_id": "ABC123",
            "validated_spec": "Preserve   the body",
            "printer_profile": {
                "name": "Bambu P1S PLA", "printer": "Bambu P1S",
                "material": "PLA", "nozzle": 0.4, "layer": 0.2,
            },
            "material_profile": {"material": "PLA", "layer_height": 0.2},
        }
        same = {**base, "validated_spec": "unrelated text ignored for source-model runs"}
        different_material = {
            **base,
            "material_profile": {"material": "PETG", "layer_height": 0.2},
        }

        self.assertEqual(adaptive_history_scope(base), adaptive_history_scope(same))
        self.assertNotEqual(adaptive_history_scope(base), adaptive_history_scope(different_material))

        from_spec = {**base, "source_model_id": None}
        same_spec = {**from_spec, "validated_spec": "  preserve the BODY  "}
        different_spec = {**from_spec, "validated_spec": "Preserve the lid"}
        self.assertEqual(adaptive_history_scope(from_spec), adaptive_history_scope(same_spec))
        self.assertNotEqual(adaptive_history_scope(from_spec), adaptive_history_scope(different_spec))

    def test_seeded_selection_is_repeatable_and_returns_copies(self) -> None:
        first = select_mutations([], rng=random.Random(42))
        second = select_mutations([], rng=random.Random(42))

        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        first[0]["mutation_type"] = "changed-by-test"
        self.assertNotEqual(MUTATION_CATALOG[0]["mutation_type"], "changed-by-test")


class MutationOutcomeStoreTests(unittest.TestCase):
    def test_outcome_survives_store_reopen_inside_isolated_root(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            workspace = Path(tempdir)
            production_library = workspace / "library"
            production_library.mkdir()
            sentinel = production_library / "sentinel.txt"
            sentinel.write_text("production-user-data", encoding="utf-8")
            root = workspace / "training-lab-data"
            store = EvolutionStore(root)
            outcome = outcome_record(
                MUTATION_CATALOG[0],
                success=True,
                score_delta=4.5,
                run_id="run_prior",
                candidate_id="candidate_prior",
                generation=2,
            )

            created = store.create_mutation_outcome(outcome)
            reopened = EvolutionStore(root)
            persisted = reopened.list_mutation_outcomes()

            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0]["id"], created["id"])
            self.assertEqual(persisted[0]["run_id"], "run_prior")
            self.assertEqual(persisted[0]["candidate_id"], "candidate_prior")
            self.assertEqual(persisted[0]["score_delta"], 4.5)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "production-user-data")
            self.assertEqual(list(production_library.iterdir()), [sentinel])

    def test_wrong_shape_and_bad_fields_on_disk_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = EvolutionStore(Path(tempdir) / "training-lab-data")
            scope = {"version": 1, "design": {"kind": "source_model", "value": "safe"}}
            created = store.create_mutation_outcome(
                outcome_record(
                    MUTATION_CATALOG[0],
                    success=True,
                    score_delta=4,
                    run_id="run_safe",
                    candidate_id="candidate_safe",
                    generation=1,
                    adaptive_scope=scope,
                )
            )
            outcome_dir = store.root / "mutation_outcomes"
            (outcome_dir / "bad_shape.json").write_text("[]", encoding="utf-8")
            store.write_json(outcome_dir / "bad_list.json", {
                "id": "bad_list", "adaptive_scope": scope,
                "mutation_type": [], "success": True, "score_delta": 100,
            })
            store.write_json(outcome_dir / "bad_dict.json", {
                "id": "bad_dict", "adaptive_scope": scope,
                "mutation_type": {"wall_thickness": True},
                "success": True, "score_delta": 100,
            })
            store.write_json(outcome_dir / MUTATION_OUTCOME_MANIFEST, {
                "version": 1,
                "ids": ["bad_shape", "bad_list", "bad_dict", created["id"]],
            })

            persisted = store.list_mutation_outcomes(scope)
            weights = mutation_weights(persisted)

            self.assertEqual(
                [item["id"] for item in persisted],
                ["bad_list", "bad_dict", created["id"]],
            )
            self.assertGreater(weights["fit_clearance"], weights["wall_thickness"])

    def test_retry_repairs_manifest_without_duplicate_outcome_file(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = EvolutionStore(Path(tempdir) / "training-lab-data")
            outcome = outcome_record(
                MUTATION_CATALOG[0], success=True, score_delta=2,
                run_id="run_retry", candidate_id="candidate_retry", generation=3,
            )
            rid = store._mutation_outcome_id(outcome)
            partial = {**outcome, "id": rid, "created_at": 1, "updated_at": 1}
            store.write_json(store.root / "mutation_outcomes" / f"{rid}.json", partial, exclusive=True)

            first = store.create_mutation_outcome(outcome)
            second = store.create_mutation_outcome(outcome)
            manifest = store.read_json(store.root / "mutation_outcomes" / MUTATION_OUTCOME_MANIFEST)

            self.assertEqual(first, partial)
            self.assertEqual(second, partial)
            self.assertEqual(manifest["ids"], [rid])
            self.assertEqual(
                list(store.root.joinpath("mutation_outcomes").glob(f"{rid}.json")),
                [store.root / "mutation_outcomes" / f"{rid}.json"],
            )

    def test_recent_manifest_is_capped_and_reads_ignore_lifetime_orphans(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = EvolutionStore(Path(tempdir) / "training-lab-data")
            outcome_dir = store.root / "mutation_outcomes"
            old_ids = [f"old_{index}" for index in range(1000)]
            store.write_json(outcome_dir / MUTATION_OUTCOME_MANIFEST, {
                "version": 1, "ids": old_ids,
            })
            for index in range(50):
                (outcome_dir / f"orphan_{index}.json").write_text("{}", encoding="utf-8")
            created = store.create_mutation_outcome(outcome_record(
                MUTATION_CATALOG[0], success=True, score_delta=3,
                run_id="run_new", candidate_id="candidate_new", generation=1,
            ))
            manifest = store.read_json(outcome_dir / MUTATION_OUTCOME_MANIFEST)

            self.assertEqual(len(manifest["ids"]), 1000)
            self.assertEqual(manifest["ids"][0], created["id"])
            with patch.object(store, "read_json", wraps=store.read_json) as reader:
                records = store.list_mutation_outcomes(limit=1)
            self.assertEqual([item["id"] for item in records], [created["id"]])
            self.assertEqual(reader.call_count, 2)


if __name__ == "__main__":
    unittest.main()
