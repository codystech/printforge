"""Evidence scoring, regression control, and scoped-memory tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from evolution_lab.memory import MemoryService, derive_status, scopes_compatible
from evolution_lab.schemas import CATEGORY_MAXIMA, EvidenceLabel, ScoreCategory
from evolution_lab.scoring import score_candidate, select_winner
from evolution_lab.store import EvolutionStore


def evidence(
    category: ScoreCategory,
    awarded: float,
    possible: float,
    *,
    label: EvidenceLabel = EvidenceLabel.MEASURED,
    critical: bool = False,
    criterion: str = "verified criterion",
) -> dict:
    return {
        "category": category.value,
        "criterion": criterion,
        "points_awarded": awarded,
        "points_possible": possible,
        "label": label.value,
        "source": "unit-test deterministic check",
        "summary": criterion,
        "confidence": 1.0 if label != EvidenceLabel.UNVERIFIED else 0.0,
        "critical": critical,
    }


class EvidenceScoringTests(unittest.TestCase):
    def test_total_equals_category_sum_at_exact_category_maxima(self) -> None:
        items = [
            evidence(ScoreCategory(category), maximum, maximum)
            for category, maximum in CATEGORY_MAXIMA.items()
        ]
        result = score_candidate(items)

        category_sum = sum(category["earned"] for category in result["categories"].values())
        self.assertEqual(result["total"], 100.0)
        self.assertEqual(result["raw_total"], 100.0)
        self.assertEqual(category_sum, result["raw_total"])
        self.assertFalse(result["hard_rejected"])
        self.assertFalse(result["evidence_cap_applied"])

    def test_awards_and_assessment_cannot_exceed_category_cap(self) -> None:
        result = score_candidate(
            [
                evidence(ScoreCategory.PRINTABILITY, 20, 20, criterion="mesh QA"),
                evidence(ScoreCategory.PRINTABILITY, 20, 20, criterion="slicer QA"),
            ]
        )

        printability = result["categories"][ScoreCategory.PRINTABILITY.value]
        self.assertEqual(printability["earned"], 25.0)
        self.assertEqual(printability["assessed_points"], 25.0)
        self.assertEqual(result["total"], 25.0)

    def test_unverified_claim_earns_zero_and_critical_gap_hard_rejects(self) -> None:
        result = score_candidate(
            [
                evidence(
                    ScoreCategory.FUNCTION,
                    25,
                    25,
                    label=EvidenceLabel.UNVERIFIED,
                    critical=True,
                    criterion="moving parts work",
                )
            ]
        )

        function = result["categories"][ScoreCategory.FUNCTION.value]
        self.assertEqual(function["earned"], 0.0)
        self.assertIn("moving parts work", function["deductions"])
        self.assertTrue(result["hard_rejected"])
        self.assertIn("critical_evidence_unavailable", result["hard_rejection_reasons"])

    def test_ai_only_high_score_is_evidence_capped(self) -> None:
        items = [
            evidence(
                ScoreCategory(category),
                maximum,
                maximum,
                label=EvidenceLabel.AI_JUDGED,
            )
            for category, maximum in CATEGORY_MAXIMA.items()
        ]
        result = score_candidate(items)

        self.assertEqual(result["raw_total"], 100.0)
        self.assertEqual(result["total"], 74.0)
        self.assertTrue(result["evidence_cap_applied"])

    def test_each_declared_hard_failure_rejects_the_candidate(self) -> None:
        for failure in (
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
        ):
            with self.subTest(failure=failure):
                result = score_candidate(
                    [evidence(ScoreCategory.PRINTABILITY, 25, 25)],
                    failure_codes=[failure],
                )
                self.assertTrue(result["hard_rejected"])
                self.assertIn(failure, result["hard_rejection_reasons"])

    def test_lower_scoring_candidates_never_replace_current_best(self) -> None:
        candidates = [
            {"candidate_id": "candidate_a", "status": "evaluated", "score": {"total": 89}},
            {"candidate_id": "candidate_b", "status": "evaluated", "score": {"total": 90}},
        ]
        result = select_winner(candidates, current_best_score=91)

        self.assertEqual(result["highest_scoring_candidate_id"], "candidate_b")
        self.assertIsNone(result["winner_candidate_id"])
        self.assertFalse(result["improved_current_best"])
        self.assertTrue(result["current_best_preserved"])

    def test_hard_rejected_candidate_cannot_win_even_with_higher_score(self) -> None:
        candidates = [
            {
                "candidate_id": "invalid_high",
                "status": "evaluated",
                "score": {"total": 99, "hard_rejected": True},
            },
            {
                "candidate_id": "valid_improvement",
                "status": "evaluated",
                "score": {"total": 92, "hard_rejected": False},
            },
        ]
        result = select_winner(candidates, current_best_score=91)

        self.assertEqual(result["winner_candidate_id"], "valid_improvement")
        self.assertTrue(result["improved_current_best"])


class MemoryLearningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = EvolutionStore(Path(self.tempdir.name) / "memory-store")
        self.memory = MemoryService(self.store)

    def create_rule(self, *, material: str = "PLA", title: str = "Captured slider clearance") -> dict:
        return self.memory.create_rule(
            {
                "category": "printer_calibrations",
                "title": title,
                "description": "Scoped clearance evidence",
                "scope": {
                    "printer": "Bambu P1S",
                    "material": material,
                    "nozzle": 0.4,
                    "layer_height": 0.2,
                    "feature": "captured_slider",
                },
                "trigger_conditions": "clearance below recommendation",
                "recommendation": "Begin at 0.35mm clearance",
                "notes": "unit test",
            }
        )

    def observe_success(self, rule_id: str, index: int, *, physical: bool = False) -> dict:
        return self.memory.observe(
            rule_id,
            {
                "success": True,
                "source_model_id": "model_one" if index < 6 else "model_two",
                "source_candidate_id": f"candidate_{index}",
                "physical": physical,
                "major_regression": False,
                "note": "successful observation",
            },
        )

    def test_one_observation_remains_a_hypothesis(self) -> None:
        rule = self.create_rule()
        observed = self.observe_success(rule["id"], 1)

        self.assertEqual(observed["evidence_count"], 1)
        self.assertEqual(observed["success_count"], 1)
        self.assertEqual(observed["status"], "hypothesis")
        self.assertGreater(observed["confidence"], 0)

    def test_promotion_thresholds_require_repeated_success_and_multiple_models(self) -> None:
        rule = self.create_rule()
        observed = None
        for index in range(1, 11):
            observed = self.observe_success(rule["id"], index, physical=index in {5, 10})
            if index == 2:
                self.assertEqual(observed["status"], "hypothesis")
            elif index == 3:
                self.assertEqual(observed["status"], "provisional")
            elif index == 5:
                self.assertEqual(observed["status"], "validated")

        assert observed is not None
        self.assertEqual(observed["status"], "high-confidence")
        self.assertEqual(observed["evidence_count"], 10)
        self.assertEqual(set(observed["source_model_ids"]), {"model_one", "model_two"})
        self.assertEqual(observed["physical_evidence_count"], 2)

    def test_major_regression_blocks_validated_and_high_confidence_status(self) -> None:
        rule = {
            "status": "hypothesis",
            "evidence_count": 10,
            "success_count": 10,
            "failure_count": 0,
            "major_regression_count": 1,
            "source_model_ids": ["model_one", "model_two"],
        }
        self.assertEqual(derive_status(rule), "provisional")

    def test_scope_compatibility_is_exact_and_does_not_bleed_materials(self) -> None:
        pla_rule = self.create_rule(material="PLA", title="PLA rule")
        petg_rule = self.create_rule(material="PETG", title="PETG rule")
        for index in range(1, 6):
            self.observe_success(pla_rule["id"], index)
            self.observe_success(petg_rule["id"], index)

        context = {
            "printer": "Bambu P1S",
            "material": "PLA",
            "nozzle": 0.4,
            "layer_height": 0.2,
            "feature": "captured_slider",
        }
        query = self.memory.query(context)
        applied_ids = {rule["id"] for rule in query["applied"]}
        ignored_ids = {item["rule"]["id"] for item in query["ignored"]}
        self.assertIn(pla_rule["id"], applied_ids)
        self.assertIn(petg_rule["id"], ignored_ids)
        compatible, reasons = scopes_compatible(petg_rule["scope"], context)
        self.assertFalse(compatible)
        self.assertIn("material mismatch", reasons)

    def test_deprecated_and_rejected_rules_are_never_applied(self) -> None:
        context = {
            "printer": "Bambu P1S",
            "material": "PLA",
            "nozzle": 0.4,
            "layer_height": 0.2,
            "feature": "captured_slider",
        }
        for action, expected_status in (("deprecate", "deprecated"), ("reject", "rejected")):
            with self.subTest(action=action):
                rule = self.create_rule(title=f"rule to {action}")
                for index in range(1, 6):
                    self.observe_success(rule["id"], index)
                reviewed = self.memory.review(rule["id"], action, "reviewed by unit test")
                self.assertEqual(reviewed["status"], expected_status)
                query = self.memory.query(context)
                self.assertNotIn(rule["id"], {item["id"] for item in query["applied"]})
                ignored = {item["rule"]["id"]: item["reasons"] for item in query["ignored"]}
                self.assertIn(rule["id"], ignored)
                self.assertIn(f"status {expected_status}", ignored[rule["id"]])


if __name__ == "__main__":
    unittest.main()
