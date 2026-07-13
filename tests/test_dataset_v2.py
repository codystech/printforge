"""Dataset-v2 eligibility, lineage, split, checksum, and physical-join tests.

All records live in temporary Training Lab stores.  The tests use fake source
and evidence only; they do not slice, render, train, or touch production data.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from pydantic import ValidationError

from evolution_lab.adaptive import outcome_record
from evolution_lab.config import EvolutionLabConfig
from evolution_lab.dataset_v2 import (
    SCHEMA_V2,
    build_examples_v2,
    evidence_coverage,
    family_split,
)
from evolution_lab.datasets import build_examples, create_export
from evolution_lab.engine import EvolutionAdapters
from evolution_lab.memory import MemoryService
from evolution_lab.router import create_router
from evolution_lab.schemas import CreateRunRequest, PhysicalValidationInput
from evolution_lab.store import EvolutionStore


def route_endpoint(router, path: str, method: str = "GET"):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"route not found: {method} {path}")


class DatasetV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = EvolutionStore(Path(self.tempdir.name) / "training_lab_data")
        self.run_id = "run_dataset_v2"
        self.store.create_run({
            "run_id": self.run_id,
            "demo": False,
            "created_at": 1.0,
            "source_prompt": "Make a printable latch",
            "validated_spec": "A 0.4 mm clearance latch",
            "model_format": "cadquery-v1",
            "part_family": "captured-latch",
            "part_family_split_key": "captured-latch",
            "training_consent": True,
            "training_consent_decision": "approved",
            "training_consent_reviewer": "Cody",
            "training_consent_reviewed_at": "2026-07-13T12:00:00-04:00",
            "provenance_status": "self-created",
            "data_provenance": {
                "status": "self-created",
                "source": "PrintForge Training Lab",
                "source_revision": "review-2026-07-13",
                "license": "owner-authored",
                "license_rights": "owned",
            },
            "printer_profile": {"printer": "Bambu P1S"},
            "material_profile": {"material": "PLA"},
        }, {})

    def candidate(self, candidate_id: str, label: str, selection: str, *, source: str | None = None, **updates):
        record = {
            "candidate_id": candidate_id,
            "run_id": self.run_id,
            "generation": 1,
            "variant_label": label,
            "parent_candidate_id": "parent",
            "current_best_parent_id": "parent",
            "mutation": {"mutation_type": "fit_clearance", "parameter": "clearance", "mutated_value": 0.4},
            "status": "complete" if selection == "winner" else "evaluated",
            "selection_status": selection,
            "score": {"total": 90 if selection == "winner" else 75, "hard_rejected": False},
            "required_checks_passed": True,
            "deterministic_evidence": [{"criterion": "trusted geometry", "label": "MEASURED"}],
            "slicer_results": {
                "status": "complete",
                "estimated_time_seconds": 100,
                "filament_grams": 2.5,
                "layer_count": 20,
                "support_used": False,
                "warnings": [],
                "sliced_3mf_artifact": f"{candidate_id}.sliced.3mf",
                "log_artifact": f"{candidate_id}.slicer.log",
            },
            "evaluator_version": "test-evaluator-v1",
            "evaluator_fingerprint": "sha256:" + "a" * 64,
            "slicer_profile_fingerprint": "sha256:" + "b" * 64,
            "model_format": "cadquery-v1",
            "model_contract_version": "cadquery-v1",
            "part_family_split_key": "captured-latch",
            "training_consent": True,
            "training_consent_decision": "approved",
            "training_consent_reviewer": "Cody",
            "training_consent_reviewed_at": "2026-07-13T12:00:00-04:00",
            "data_provenance": {
                "status": "self-created",
                "source": "PrintForge Training Lab",
                "source_revision": "review-2026-07-13",
                "license": "owner-authored",
                "license_rights": "owned",
            },
            "parts": [{
                "name": "body",
                "export_role": "printable",
                "stl_artifact": f"{candidate_id}.stl",
            }],
            "physical_outcomes": [],
            "artifacts": [],
            **updates,
        }
        self.store.create_candidate(self.run_id, record)
        if source is not None:
            self.store.add_candidate_artifacts(self.run_id, candidate_id, {
                "model.py": source,
                f"{candidate_id}.stl": f"solid {candidate_id}",
                f"{candidate_id}.sliced.3mf": b"fixture sliced archive",
                f"{candidate_id}.slicer.log": b"fixture slicer log",
            })
        return self.store.get_candidate(self.run_id, candidate_id)

    def attach_physical(self, candidate_id: str, *, success: bool) -> dict:
        candidate = self.store.get_candidate(self.run_id, candidate_id)
        stl = next(item for item in candidate["artifacts"] if item["name"].endswith(".stl"))
        physical_id = self.store.physical_validation_id(self.run_id, candidate_id, stl["sha256"])
        physical = self.store.create_record("physical", {
            "id": physical_id,
            "run_id": self.run_id,
            "candidate_id": candidate_id,
            "artifact_checksum": f"sha256:{stl['sha256']}",
            "artifact_name": stl["name"],
            "artifact_role": "printable",
            "verified_join": True,
            "candidate_joined": True,
            "memory_joined": True,
            "memory_rule_ids_observed": [],
            "mutation_outcome_joined": int(candidate.get("generation", 0)) == 0,
            "printed_successfully": success,
            "failure_classes": [] if success else ["loose_fit"],
        }, prefix="physical")
        self.store.attach_physical_to_candidate(self.run_id, candidate_id, physical, verified=True)
        return physical

    def test_v1_remains_compatible_while_v2_fails_closed(self) -> None:
        self.candidate("chosen", "A", "winner", source="def build(params, assets): return {}")
        self.candidate("loser", "B", "loser", source="def build(params, assets): return {}")
        self.assertEqual(len(build_examples(self.store, "preference", self.run_id)), 1)
        self.assertEqual(len(build_examples_v2(self.store, "preference", self.run_id)), 1)

        self.store.update_candidate(
            self.run_id, "loser", lambda row: row.update({"slicer_profile_fingerprint": "sha256:" + "c" * 64})
        )
        self.assertEqual(build_examples_v2(self.store, "preference", self.run_id), [])
        self.assertEqual(len(build_examples(self.store, "preference", self.run_id)), 1)

    def test_sft_requires_accepted_cadquery_source_and_slice_not_render_only(self) -> None:
        self.candidate(
            "rendered_only", "A", "winner",
            source="def build(params, assets): return {}",
            slicer_results={"status": "unavailable"},
            slicer_profile_fingerprint=None,
        )
        self.assertEqual(build_examples_v2(self.store, "sft", self.run_id), [])
        self.store.update_candidate(
            self.run_id,
            "rendered_only",
            lambda row: row.update({
                "slicer_results": {
                    "status": "complete",
                    "estimated_time_seconds": 100,
                    "filament_grams": 2.5,
                    "layer_count": 20,
                    "support_used": False,
                    "warnings": [],
                    "sliced_3mf_artifact": "rendered_only.sliced.3mf",
                    "log_artifact": "rendered_only.slicer.log",
                },
                "slicer_profile_fingerprint": "sha256:" + "b" * 64,
            }),
        )
        rows = build_examples_v2(self.store, "sft", self.run_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema"], SCHEMA_V2)
        self.assertEqual(rows[0]["candidate"]["source_sha256"], self.store.get_candidate(
            self.run_id, "rendered_only"
        )["source_sha256"])

    def test_observed_failures_are_present_evidence_not_missing_evidence(self) -> None:
        candidate = self.candidate(
            "observed_failure", "A", "rejected", source="observed failure",
            required_checks_passed=False,
            deterministic_evidence=[{"criterion": "B-rep", "passed": False}],
            slicer_results={"status": "failed", "reason": "empty plate"},
        )
        coverage, missing = evidence_coverage(candidate)
        self.assertEqual(coverage["deterministic"], {"present": True, "passed": False})
        self.assertEqual(coverage["slicer"], {"present": True, "passed": False})
        self.assertFalse(missing["deterministic"])
        self.assertFalse(missing["slicer"])

    def test_run_family_and_immutable_audit_cannot_be_overridden_by_candidate_labels(self) -> None:
        self.candidate("chosen", "A", "winner", source="chosen", part_family_split_key="test-leak")
        self.candidate("loser", "B", "loser", source="loser", part_family_split_key="validation-leak")
        rows = build_examples_v2(self.store, "preference", self.run_id)
        self.assertEqual(rows[0]["split"], family_split("captured-latch"))
        self.assertEqual(rows[0]["chosen"]["part_family_split_key"], "captured-latch")
        self.store.update_candidate(
            self.run_id,
            "loser",
            lambda row: row["provenance_audit"].update({"license_rights": "licensed_for_training"}),
        )
        # Even a plausible label is rejected once the content-hashed audit is changed.
        self.assertEqual(build_examples_v2(self.store, "preference", self.run_id), [])

    def test_sft_exact_failed_artifact_cannot_be_overridden_by_metadata(self) -> None:
        self.candidate("physical_fail", "A", "winner", source="physical fail")
        self.attach_physical("physical_fail", success=False)
        self.assertEqual(build_examples_v2(self.store, "sft", self.run_id), [])
        self.store.update_candidate(self.run_id, "physical_fail", lambda row: row.update({
            "post_physical_acceptance": {
                "decision": "approved",
                "reviewer": "Cody",
                "reviewed_at": "2026-07-13T15:30:00-04:00",
                "reason": "Accepted after measured repair outside this artifact",
            },
        }))
        self.assertEqual(build_examples_v2(self.store, "sft", self.run_id), [])

        self.candidate("physical_repair", "B", "winner", source="repaired source")
        self.attach_physical("physical_repair", success=True)
        rows = build_examples_v2(self.store, "sft", self.run_id)
        self.assertEqual([row["candidate"]["candidate_id"] for row in rows], ["physical_repair"])

    def test_physical_preference_authority_requires_exact_directional_backlinks(self) -> None:
        self.candidate("chosen", "A", "winner", source="chosen", generation=0, mutation=None)
        self.candidate("loser", "B", "loser", source="loser", generation=0, mutation=None)
        chosen_physical = self.attach_physical("chosen", success=False)
        rejected_physical = self.attach_physical("loser", success=True)

        # Decisive verified evidence in the opposite direction vetoes the row.
        self.assertEqual(build_examples_v2(self.store, "preference", self.run_id), [])

        self.store.update_record("physical", chosen_physical["id"], lambda row: row.update({
            "printed_successfully": True, "failure_classes": [],
        }))
        self.store.update_record("physical", rejected_physical["id"], lambda row: row.update({
            "printed_successfully": False, "failure_classes": ["loose_fit"],
        }))
        self.store.attach_physical_to_candidate(
            self.run_id, "chosen", self.store.get_record("physical", chosen_physical["id"]), verified=True
        )
        self.store.attach_physical_to_candidate(
            self.run_id, "loser", self.store.get_record("physical", rejected_physical["id"]), verified=True
        )
        self.assertEqual(
            build_examples_v2(self.store, "preference", self.run_id)[0]["label_authority"],
            "physical_outcome",
        )

        # A claimed physical result with a broken persisted backlink cannot gain authority.
        self.store.update_record("physical", chosen_physical["id"], lambda row: row.update({
            "candidate_joined": False,
        }))
        self.assertEqual(
            build_examples_v2(self.store, "preference", self.run_id)[0]["label_authority"],
            "deterministic_sliced_comparison",
        )

    def test_api_rejects_unrecognized_training_license_rights(self) -> None:
        with self.assertRaises(ValidationError):
            CreateRunRequest(
                validated_spec="printable latch",
                printer_profile={"printer": "Bambu P1S"},
                data_provenance={"license_rights": "visible_on_the_web"},
            )

    def test_consent_provenance_and_hard_rejection_are_strict(self) -> None:
        self.candidate("chosen", "A", "winner", source="chosen")
        self.candidate("loser", "B", "loser", source="loser", training_consent=False)
        self.store.update_run(self.run_id, lambda row: row.update({
            "training_consent": False, "training_consent_decision": "not_reviewed",
        }))
        self.assertEqual(build_examples_v2(self.store, "preference", self.run_id), [])
        self.store.update_run(self.run_id, lambda row: row.update({
            "training_consent": True, "training_consent_decision": "approved",
        }))
        self.store.update_candidate(
            self.run_id,
            "loser",
            lambda row: row.update({"training_consent": True, "score": {"total": 100, "hard_rejected": True}}),
        )
        self.assertEqual(build_examples_v2(self.store, "preference", self.run_id), [])

    def test_family_split_is_stable_for_siblings_and_content_export_is_immutable(self) -> None:
        chosen = self.candidate("chosen", "A", "winner", source="chosen source")
        self.candidate("loser", "B", "loser", source="loser source")
        rows = build_examples_v2(self.store, "preference", self.run_id)
        self.assertEqual(rows[0]["chosen"]["split"], rows[0]["rejected"]["split"])
        self.assertEqual(rows[0]["split"], family_split("captured-latch"))

        first = create_export(self.store, "preference", "jsonl", self.run_id, SCHEMA_V2)
        second = create_export(self.store, "preference", "jsonl", self.run_id, SCHEMA_V2)
        self.assertEqual(first["dataset_id"], second["dataset_id"])
        self.assertRegex(first["checksum"], r"^sha256:[0-9a-f]{64}$")
        with self.assertRaises(FileExistsError):
            self.store.write_dataset_file(first["id"], first["filename"], b"replacement")
        self.assertEqual(chosen["part_family_split_key"], "captured-latch")

    def test_mutation_repair_failure_and_print_outcome_rows_keep_lineage(self) -> None:
        parent = self.candidate(
            "parent", "PARENT", "baseline", source="failed parent",
            verified_failure_types=["dimensional_error"],
        )
        child = self.candidate(
            "child", "A", "winner", source="accepted child",
            repaired_from_candidate_id="parent",
        )
        mutation_outcome = self.store.create_mutation_outcome(outcome_record(
            child["mutation"], success=True, score_delta=8,
            run_id=self.run_id, candidate_id="child", generation=1,
            eligible=True, candidate_status="complete", selection_status="winner",
        ))
        self.store.update_candidate(
            self.run_id,
            "child",
            lambda row: row.update({"verified_failure_types": ["surface_quality"]}),
        )
        child = self.store.get_candidate(self.run_id, "child")
        stl = next(item for item in child["artifacts"] if item["name"].endswith(".stl"))
        physical_id = self.store.physical_validation_id(self.run_id, "child", stl["sha256"])
        physical = self.store.create_record("physical", {
            "id": physical_id,
            "run_id": self.run_id,
            "candidate_id": "child",
            "artifact_checksum": f"sha256:{stl['sha256']}",
            "artifact_name": stl["name"],
            "artifact_role": "printable",
            "verified_join": True,
            "candidate_joined": True,
            "memory_joined": True,
            "memory_rule_ids_observed": [],
            "mutation_outcome_joined": True,
            "mutation_outcome_id": mutation_outcome["id"],
            "printed_successfully": True,
            "printer_profile": {"printer": "Bambu P1S"},
            "material": "PLA",
        }, prefix="physical")
        self.store.attach_physical_to_candidate(self.run_id, "child", physical, verified=True)
        self.store.attach_physical_to_mutation({**physical, "generation": 1}, verified=True)

        mutation = build_examples_v2(self.store, "mutation", self.run_id)
        repair = build_examples_v2(self.store, "repair", self.run_id)
        failure = build_examples_v2(self.store, "failure", self.run_id)
        print_rows = build_examples_v2(self.store, "print_outcome", self.run_id)
        self.assertEqual(mutation[0]["parent_state"]["candidate_id"], parent["candidate_id"])
        self.assertEqual(mutation[0]["reward_delta"], 8)
        self.assertEqual(repair[0]["failed_parent"]["candidate_id"], "parent")
        self.assertEqual(repair[0]["accepted_repair"]["candidate_id"], "child")
        self.assertEqual(failure[0]["verified_failure_types"], ["surface_quality"])
        self.assertEqual(print_rows[0]["candidate"]["candidate_id"], "child")
        self.assertEqual({mutation[0]["split"], repair[0]["split"], failure[0]["split"], print_rows[0]["split"]}, {
            family_split("captured-latch")
        })
        self.store.update_record("physical", physical_id, lambda row: row.update({
            "mutation_outcome_id": "mutation_outcome_" + "0" * 32,
            "mutation_outcome_joined": True,
        }))
        self.assertEqual(build_examples_v2(self.store, "print_outcome", self.run_id), [])
        self.store.update_record("physical", physical_id, lambda row: row.update({
            "mutation_outcome_id": mutation_outcome["id"],
        }))
        mutation_path = self.store.root / "mutation_outcomes" / f"{mutation_outcome['id']}.json"
        original_mutation = self.store.get_mutation_outcome(mutation_outcome["id"])
        corrupt_mutation = {**original_mutation, "physical_outcomes": []}
        self.store.write_json(mutation_path, corrupt_mutation)
        self.assertEqual(build_examples_v2(self.store, "print_outcome", self.run_id), [])
        self.store.write_json(mutation_path, original_mutation)
        self.assertEqual(len(build_examples_v2(self.store, "print_outcome", self.run_id)), 1)
        self.store.update_record("physical", physical_id, lambda row: row.update({
            "artifact_checksum": "sha256:" + "0" * 64,
        }))
        self.assertEqual(build_examples_v2(self.store, "print_outcome", self.run_id), [])
        self.store.update_candidate(self.run_id, "parent", lambda row: row["provenance_audit"].update({
            "source_revision": "tampered",
        }))
        self.assertEqual(build_examples_v2(self.store, "mutation", self.run_id), [])
        self.assertEqual(build_examples_v2(self.store, "repair", self.run_id), [])

    async def test_physical_record_requires_tuple_checksum_and_joins_candidate_mutation_memory(self) -> None:
        memory = MemoryService(self.store)
        rule = memory.create_rule({
            "category": "fit",
            "title": "Latch clearance",
            "recommendation": "Use measured clearance",
            "scope": {},
        })
        candidate = self.candidate(
            "printed", "A", "winner", source="printed source",
            memory_rules_applied=[rule["id"]],
        )
        self.store.create_mutation_outcome(outcome_record(
            candidate["mutation"], success=True, score_delta=5,
            run_id=self.run_id, candidate_id="printed", generation=1,
            eligible=True, candidate_status="complete", selection_status="winner",
        ))
        stl = next(item for item in candidate["artifacts"] if item["name"].endswith(".stl"))
        config = EvolutionLabConfig(
            training_lab_enabled=True,
            physical_feedback_enabled=True,
            memory_learning_enabled=True,
            data_root=self.store.root,
        )
        router = create_router(config, EvolutionAdapters(), store=self.store)
        endpoint = route_endpoint(router, "/training-lab/api/physical-validations", "POST")
        request = PhysicalValidationInput(
            run_id=self.run_id,
            candidate_id="printed",
            artifact_checksum=stl["sha256"],
            artifact_name=stl["name"],
            printed_successfully=False,
            printer_profile={"printer": "Bambu P1S"},
            material="PLA",
            nozzle=0.4,
            layer_height=0.2,
            slicer_profile="0.20mm Standard",
            failure_classes=["loose_fit"],
        )
        result = await endpoint(request)
        self.assertTrue(result["verified_join"])
        self.assertTrue(result["candidate_joined"])
        self.assertTrue(result["mutation_outcome_joined"])
        updated = self.store.get_candidate(self.run_id, "printed")
        self.assertEqual(updated["physical_validation_status"], "failed")
        self.assertEqual(updated["physical_outcomes"][0]["physical_validation_id"], result["id"])
        outcome = self.store.list_mutation_outcomes(limit=10)[0]
        self.assertEqual(outcome["physical_outcomes"][0]["physical_validation_id"], result["id"])
        observed = self.store.get_record("memory", rule["id"])
        self.assertEqual(observed["physical_evidence_count"], 1)
        self.assertEqual(observed["observations"][0]["physical_validation_id"], result["id"])

        replay = await endpoint(request)
        self.assertEqual(replay["id"], result["id"])
        self.assertEqual(len(self.store.get_candidate(self.run_id, "printed")["physical_outcomes"]), 1)
        self.assertEqual(self.store.get_record("memory", rule["id"])["physical_evidence_count"], 1)

        source_artifact = next(item for item in candidate["artifacts"] if item["name"] == "model.py")
        non_printable = PhysicalValidationInput(
            **{**(request.model_dump(mode="json") if hasattr(request, "model_dump") else request.dict()),
               "artifact_checksum": source_artifact["sha256"], "artifact_name": "model.py"}
        )
        with self.assertRaises(HTTPException) as role_error:
            await endpoint(non_printable)
        self.assertEqual(role_error.exception.status_code, 409)

        conflicting_payload = request.model_dump(mode="json") if hasattr(request, "model_dump") else request.dict()
        conflicting_payload.update({"printed_successfully": True, "failure_classes": []})
        with self.assertRaises(HTTPException) as conflict:
            await endpoint(PhysicalValidationInput(**conflicting_payload))
        self.assertEqual(conflict.exception.status_code, 409)

        bad = PhysicalValidationInput(
            **{**request.model_dump(mode="json"), "artifact_checksum": "0" * 64}
        ) if hasattr(request, "model_dump") else PhysicalValidationInput(
            **{**request.dict(), "artifact_checksum": "0" * 64}
        )
        with self.assertRaises(HTTPException) as mismatch:
            await endpoint(bad)
        self.assertEqual(mismatch.exception.status_code, 409)

    async def test_physical_tuple_is_persisted_pending_until_mutation_backlink_exists(self) -> None:
        candidate = self.candidate("pending", "A", "winner", source="pending source")
        stl = next(item for item in candidate["artifacts"] if item["name"].endswith(".stl"))
        config = EvolutionLabConfig(
            training_lab_enabled=True,
            physical_feedback_enabled=True,
            data_root=self.store.root,
        )
        endpoint = route_endpoint(
            create_router(config, EvolutionAdapters(), store=self.store),
            "/training-lab/api/physical-validations", "POST",
        )
        request = PhysicalValidationInput(
            run_id=self.run_id, candidate_id="pending",
            artifact_checksum=stl["sha256"], artifact_name=stl["name"],
            printed_successfully=True, printer_profile={"printer": "Bambu P1S"},
            material="PLA", nozzle=0.4, layer_height=0.2,
        )
        with self.assertRaises(HTTPException) as not_finalized:
            await endpoint(request)
        self.assertEqual(not_finalized.exception.status_code, 409)
        physical_id = self.store.physical_validation_id(self.run_id, "pending", stl["sha256"])
        pending = self.store.get_record("physical", physical_id)
        self.assertEqual(pending["join_status"], "pending")
        self.assertFalse(pending["verified_join"])
        self.assertEqual(self.store.get_candidate(self.run_id, "pending")["physical_outcomes"], [])

    async def test_fixed_physical_failure_taxonomy_rejects_duplicates_and_unnamed_other(self) -> None:
        candidate = self.candidate(
            "fixed_taxonomy", "A", "winner", source="fixed taxonomy source",
            generation=0, mutation=None,
        )
        stl = next(item for item in candidate["artifacts"] if item["name"].endswith(".stl"))
        endpoint = route_endpoint(
            create_router(EvolutionLabConfig(
                training_lab_enabled=True, physical_feedback_enabled=True,
                data_root=self.store.root,
            ), EvolutionAdapters(), store=self.store),
            "/training-lab/api/physical-validations", "POST",
        )
        base = {
            "run_id": self.run_id, "candidate_id": "fixed_taxonomy",
            "artifact_checksum": stl["sha256"], "artifact_name": stl["name"],
            "printed_successfully": False,
            "printer_profile": {"printer": "Bambu P1S"},
            "material": "PLA", "nozzle": 0.4, "layer_height": 0.2,
        }
        with self.assertRaises(HTTPException) as duplicate:
            await endpoint(PhysicalValidationInput(
                **base, failure_classes=["loose_fit", "loose_fit"]
            ))
        self.assertEqual(duplicate.exception.status_code, 400)
        with self.assertRaises(HTTPException) as unnamed_other:
            await endpoint(PhysicalValidationInput(
                **base, failure_classes=["other"], failure_notes=""
            ))
        self.assertEqual(unnamed_other.exception.status_code, 400)

    def test_ui_exposes_explicit_consent_provenance_family_and_schema_controls(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "static/training-lab/index.html").read_text()
        script = (root / "static/training-lab/training-lab.js").read_text()
        for marker in (
            'id="new-part-family"', 'id="new-training-consent"',
            'id="new-consent-reviewer"', 'id="new-provenance-rights"',
            'value="printforge-training-dataset-v1" selected',
            'value="printforge-training-dataset-v2"',
        ):
            self.assertIn(marker, html)
        self.assertIn("training_consent: consent", script)
        self.assertIn("schema_version: schema", script)


if __name__ == "__main__":
    unittest.main()
