"""Dataset-safety and seeded-demo isolation tests."""

from __future__ import annotations

import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from evolution_lab.datasets import build_examples, create_export, render_export, sanitize
from evolution_lab.demo import DEMO_BANNER, DEMO_RUN_ID, load_demo_fixture
from evolution_lab.store import EvolutionStore


class DatasetAndDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "isolated-lab"
        self.store = EvolutionStore(self.root)

    def create_preference_run(self) -> str:
        run_id = "real_preference_run"
        self.store.create_run(
            {
                "run_id": run_id,
                "demo": False,
                "created_at": 1.0,
                "source_prompt": "Make a captured slider",
                "validated_spec": "Slider clearance must be measured",
                "printer_profile": {"printer": "Bambu P1S", "api_key": "profile-secret"},
                "material_profile": {"material": "PLA", "filesystem_path": "/private/material.json"},
            },
            {"model.scad": "cube(1);"},
        )
        common = {
            "run_id": run_id,
            "generation": 1,
            "parent_candidate_id": "baseline",
            "current_best_parent_id": "baseline",
            "created_at": 2.0,
            "status": "complete",
        }
        self.store.create_candidate(
            run_id,
            {
                **common,
                "candidate_id": "chosen_candidate",
                "variant_label": "A",
                "selection_status": "winner",
                "score": {"total": 91, "hard_rejected": False},
                "selection_reasons": ["measured clearance improved"],
                "prompt_used": "candidate generation prompt",
                "system_prompt": "private evaluator law",
                "artifacts": [{"name": "chosen.stl"}],
            },
        )
        self.store.create_candidate(
            run_id,
            {
                **common,
                "candidate_id": "rejected_candidate",
                "variant_label": "B",
                "selection_status": "rejected",
                "score": {"total": 74, "hard_rejected": False},
                "rejection_reasons": ["moving clearance regressed"],
                "artifacts": [{"name": "rejected.stl"}],
            },
        )
        return run_id

    def test_sanitize_redacts_sensitive_keys_and_absolute_paths_recursively(self) -> None:
        sanitized = sanitize(
            {
                "api_key": "top-secret",
                "Authorization": "Bearer token-value",
                "nested": {
                    "system_prompt": "private system text",
                    "trained_model_path": "/srv/private/model.bin",
                },
                "files": ["/home/user/private/model.stl", "safe-relative.stl"],
                "safe": "retained",
            }
        )
        serialized = json.dumps(sanitized)

        for secret in ("top-secret", "token-value", "private system text", "/srv/private", "/home/user"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, serialized)
        self.assertEqual(sanitized["api_key"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["system_prompt"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["trained_model_path"], "[REDACTED-PATH]/model.bin")
        self.assertEqual(sanitized["files"][0], "[REDACTED-PATH]/model.stl")
        self.assertEqual(sanitized["files"][1], "safe-relative.stl")
        self.assertEqual(sanitized["safe"], "retained")

    def test_preference_export_keeps_chosen_and_rejected_candidates_linked(self) -> None:
        run_id = self.create_preference_run()
        examples = build_examples(self.store, "preference", run_id)

        self.assertEqual(len(examples), 1)
        pair = examples[0]
        self.assertEqual(pair["example_type"], "preference_pair")
        self.assertEqual(pair["chosen_candidate"]["candidate_id"], "chosen_candidate")
        self.assertEqual(pair["rejected_candidate"]["candidate_id"], "rejected_candidate")
        self.assertEqual(pair["chosen_score"], 91)
        self.assertEqual(pair["rejected_score"], 74)
        serialized = json.dumps(pair)
        self.assertNotIn("profile-secret", serialized)
        self.assertNotIn("/private/material.json", serialized)

    def test_json_jsonl_csv_and_zip_exports_are_parseable(self) -> None:
        examples = [
            {
                "example_type": "preference_pair",
                "run_id": "run_one",
                "generation": 1,
                "chosen_score": 91,
                "rejected_score": 74,
            }
        ]

        filename, media_type, content = render_export(examples, "json")
        self.assertEqual(filename, "dataset.json")
        self.assertEqual(media_type, "application/json")
        self.assertEqual(json.loads(content), examples)

        filename, media_type, content = render_export(examples, "jsonl")
        self.assertEqual(filename, "dataset.jsonl")
        self.assertEqual(media_type, "application/x-ndjson")
        self.assertEqual([json.loads(line) for line in content.splitlines()], examples)

        filename, media_type, content = render_export(examples, "csv")
        self.assertEqual(filename, "dataset.csv")
        self.assertEqual(media_type, "text/csv")
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["run_id"], "run_one")
        self.assertEqual(json.loads(rows[0]["summary_json"]), examples[0])

        filename, media_type, content = render_export(examples, "zip")
        self.assertEqual(filename, "dataset.zip")
        self.assertEqual(media_type, "application/zip")
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"dataset.json", "dataset.jsonl", "summary.csv", "MANIFEST.json"},
            )
            manifest = json.loads(archive.read("MANIFEST.json"))
            self.assertEqual(manifest["example_count"], 1)
            self.assertFalse(manifest["actual_training_performed"])
            self.assertFalse(manifest["contains_model_weights"])

    def test_export_is_persisted_only_under_dataset_root_and_is_immutable(self) -> None:
        run_id = self.create_preference_run()
        record = create_export(self.store, "preference", "jsonl", run_id)
        exported = self.store.dataset_file(record["id"], record["filename"])

        self.assertTrue(exported.is_relative_to(self.root / "datasets"))
        self.assertEqual(record["example_count"], 1)
        self.assertFalse(record["actual_training_performed"])
        with self.assertRaises(FileExistsError):
            self.store.write_dataset_file(record["id"], record["filename"], b"overwrite")

    def test_demo_fixture_is_permanently_labelled_and_idempotent(self) -> None:
        first = load_demo_fixture(self.store)
        second = load_demo_fixture(self.store)

        self.assertEqual(first["run_id"], DEMO_RUN_ID)
        self.assertEqual(second["run_id"], DEMO_RUN_ID)
        self.assertTrue(first["demo"])
        self.assertEqual(first["demo_banner"], DEMO_BANNER)
        self.assertIn("NOT A REAL TRAINING RUN", first["demo_banner"])
        self.assertFalse(first["summary"]["actual_training_performed"])
        self.assertEqual(
            len([run for run in self.store.list_runs() if run["run_id"] == DEMO_RUN_ID]),
            1,
        )
        self.assertEqual(len(self.store.list_candidates(DEMO_RUN_ID)), 3)

    def test_demo_runs_are_excluded_from_training_dataset_examples(self) -> None:
        load_demo_fixture(self.store)

        self.assertEqual(len(build_examples(self.store, "all")), 0)
        self.assertEqual(len(build_examples(self.store, "all", DEMO_RUN_ID)), 0)

    def test_unsupported_export_format_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            render_export([], "tar")


if __name__ == "__main__":
    unittest.main()
