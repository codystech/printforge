"""Safety and persistence tests for the isolated evolution lab.

These tests deliberately use only temporary directories.  They must never read or
write PrintForge's real ``library/`` or ``uploads/`` user-data directories.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from evolution_lab.config import EvolutionLabConfig
from evolution_lab.store import EvolutionStore, safe_id


class EvolutionConfigTests(unittest.TestCase):
    FLAG_FIELDS = {
        "PRINT_FORGE_EVOLUTION_ENABLED": "evolution_enabled",
        "PRINT_FORGE_TRAINING_LAB_ENABLED": "training_lab_enabled",
        "PRINT_FORGE_MEMORY_LEARNING_ENABLED": "memory_learning_enabled",
        "PRINT_FORGE_PHYSICAL_FEEDBACK_ENABLED": "physical_feedback_enabled",
        "PRINT_FORGE_ACTUAL_TRAINING_ENABLED": "actual_training_enabled",
        "PRINT_FORGE_CADQUERY_ENABLED": "cadquery_enabled",
        "PRINT_FORGE_TRAINED_MODEL_APPROVED": "trained_model_approved",
        "PRINT_FORGE_LAB_ONLY": "lab_only",
    }

    def test_all_experimental_flags_default_off(self) -> None:
        config = EvolutionLabConfig.from_env({})

        for field in self.FLAG_FIELDS.values():
            with self.subTest(field=field):
                self.assertFalse(getattr(config, field))

    def test_boolean_env_parsing_accepts_only_documented_truthy_tokens(self) -> None:
        truthy = ("1", "true", "TRUE", " yes ", "On")
        falsey = ("", "0", "false", "no", "off", "2", "enabled", "truthy")

        for env_name, field in self.FLAG_FIELDS.items():
            for value in truthy:
                with self.subTest(env_name=env_name, value=value):
                    config = EvolutionLabConfig.from_env({env_name: value})
                    self.assertTrue(getattr(config, field))
            for value in falsey:
                with self.subTest(env_name=env_name, value=value):
                    config = EvolutionLabConfig.from_env({env_name: value})
                    self.assertFalse(getattr(config, field))

    def test_configuration_strings_are_trimmed_and_public_view_hides_values(self) -> None:
        config = EvolutionLabConfig.from_env(
            {
                "PRINT_FORGE_TRAINING_BACKEND": " provider-a ",
                "PRINT_FORGE_TRAINING_DATASET": " private-dataset ",
                "PRINT_FORGE_BASE_MODEL": " base-model ",
                "PRINT_FORGE_TRAINED_MODEL_PATH": " /private/model/path ",
                "PRINT_FORGE_TRAINED_MODEL_VERSION": " version-1 ",
            }
        )

        self.assertEqual(config.training_backend, "provider-a")
        self.assertEqual(config.training_dataset, "private-dataset")
        self.assertEqual(config.base_model, "base-model")
        public = config.public_dict()
        serialized = json.dumps(public)
        self.assertNotIn("private-dataset", serialized)
        self.assertNotIn("/private/model/path", serialized)
        self.assertNotIn("base-model", serialized)
        self.assertTrue(public["training_backend_configured"])
        self.assertTrue(public["training_dataset_configured"])
        self.assertTrue(public["trained_model_configured"])
        self.assertEqual(public["trained_model_version"], "version-1")


class EvolutionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.workspace = Path(self.tempdir.name)
        self.library = self.workspace / "library"
        self.uploads = self.workspace / "uploads"
        self.library.mkdir()
        self.uploads.mkdir()
        (self.library / "sentinel.txt").write_text("library-user-data", encoding="utf-8")
        (self.uploads / "sentinel.txt").write_text("upload-user-data", encoding="utf-8")
        self.root = self.workspace / "isolated-evolution-data"
        self.store = EvolutionStore(self.root)

    def assert_user_data_untouched(self) -> None:
        self.assertEqual(
            list(self.library.iterdir()),
            [self.library / "sentinel.txt"],
        )
        self.assertEqual(
            list(self.uploads.iterdir()),
            [self.uploads / "sentinel.txt"],
        )
        self.assertEqual(
            (self.library / "sentinel.txt").read_text(encoding="utf-8"),
            "library-user-data",
        )
        self.assertEqual(
            (self.uploads / "sentinel.txt").read_text(encoding="utf-8"),
            "upload-user-data",
        )

    def create_run(self, run_id: str = "run_safe", *, demo: bool = False) -> dict:
        return self.store.create_run(
            {
                "run_id": run_id,
                "demo": demo,
                "created_at": 1.0,
                "status": "created",
            },
            {"baseline.scad": "cube([1, 1, 1]);"},
        )

    def create_candidate(self, run_id: str = "run_safe", candidate_id: str = "candidate_a") -> dict:
        return self.store.create_candidate(
            run_id,
            {
                "candidate_id": candidate_id,
                "run_id": run_id,
                "generation": 1,
                "variant_label": "A",
                "parent_candidate_id": "baseline",
                "current_best_parent_id": "baseline",
                "score": {"total": 88.0},
                "created_at": 2.0,
            },
        )

    def test_store_writes_only_beneath_explicit_root_and_uses_atomic_replacement(self) -> None:
        self.create_run()
        self.create_candidate()
        self.store.add_candidate_artifacts(
            "run_safe",
            "candidate_a",
            {"model.scad": "sphere(5);", "report.json": "{}"},
        )
        self.store.update_run("run_safe", lambda run: run.update({"status": "active"}))

        self.assertEqual(self.store.get_run("run_safe")["status"], "active")
        self.assertEqual(
            self.store.candidate_artifact("run_safe", "candidate_a", "model.scad").read_text(
                encoding="utf-8"
            ),
            "sphere(5);",
        )
        self.assertFalse(list(self.root.rglob("*.tmp")))
        self.assertFalse([path for path in self.workspace.rglob("*") if path.name.startswith(".run.json.")])
        self.assert_user_data_untouched()

    def test_public_write_helpers_reject_directories_outside_store_root(self) -> None:
        with self.assertRaises(ValueError):
            self.store.write_artifact(self.library, "probe.txt", b"must not be written")
        with self.assertRaises(ValueError):
            self.store.write_json(self.uploads / "probe.json", {"must_not": "be written"})

        self.assert_user_data_untouched()

    def test_concurrent_event_appends_remain_valid_and_strictly_sequenced(self) -> None:
        self.create_run()

        def append(index: int) -> None:
            self.store.append_event(
                "run_safe",
                "info",
                "concurrent_test",
                f"event {index}",
                data={"index": index},
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(append, range(40)))

        events = self.store.list_events("run_safe")
        self.assertEqual(len(events), 41)  # includes create_run's initial event
        self.assertEqual([event["seq"] for event in events], list(range(1, 42)))
        self.assertEqual(
            {event["data"]["index"] for event in events if event["event_type"] == "concurrent_test"},
            set(range(40)),
        )
        self.assert_user_data_untouched()

    def test_ids_and_artifact_names_reject_path_traversal(self) -> None:
        invalid_ids = (
            "",
            ".",
            "..",
            "../escape",
            "safe/../../escape",
            "/absolute",
            "with space",
            "a" * 65,
        )
        for value in invalid_ids:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    safe_id(value)

        self.create_run()
        self.create_candidate()
        for name in ("../secret", "nested/file.stl", "/tmp/file", ".", "..", "bad name.stl"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.store.write_artifact(self.root, name, b"x")
                with self.assertRaises((ValueError, FileNotFoundError)):
                    self.store.candidate_artifact("run_safe", "candidate_a", name)

        with self.assertRaises(ValueError):
            self.store.get_run("../library")
        with self.assertRaises(ValueError):
            self.store.candidate_dir("run_safe", "../../uploads")
        self.assertFalse((self.workspace / "escape").exists())
        self.assert_user_data_untouched()

    def test_checkpoint_files_and_manifest_are_immutable_snapshots(self) -> None:
        self.create_run()
        self.create_candidate()
        self.store.add_candidate_artifacts("run_safe", "candidate_a", {"model.scad": "sphere(5);"})
        checkpoint = self.store.create_checkpoint("run_safe", "candidate_a", "current_best")
        checkpoint_dir = self.root / "runs" / "run_safe" / "checkpoints" / checkpoint["checkpoint_id"]
        original_manifest = (checkpoint_dir / "manifest.json").read_bytes()
        original_model = (checkpoint_dir / "model.scad").read_bytes()

        self.store.update_candidate(
            "run_safe",
            "candidate_a",
            lambda candidate: candidate.update({"score": {"total": 99.0}}),
        )

        self.assertTrue(checkpoint["immutable"])
        self.assertEqual((checkpoint_dir / "manifest.json").read_bytes(), original_manifest)
        self.assertEqual((checkpoint_dir / "model.scad").read_bytes(), original_model)
        with self.assertRaises(FileExistsError):
            self.store.write_artifact(checkpoint_dir, "model.scad", "changed", immutable=True)
        with self.assertRaises(FileExistsError):
            self.store.write_json(checkpoint_dir / "manifest.json", {"changed": True}, exclusive=True)
        self.assertEqual((checkpoint_dir / "model.scad").read_bytes(), original_model)
        self.assert_user_data_untouched()

    def test_artifact_readers_reject_symlinked_ancestor_directories(self) -> None:
        self.create_run()
        self.create_candidate()
        self.store.add_candidate_artifacts("run_safe", "candidate_a", {"model.scad": "sphere(5);"})
        artifacts = self.store.candidate_dir("run_safe", "candidate_a") / "artifacts"
        real_artifacts = artifacts.with_name("real-artifacts")
        artifacts.rename(real_artifacts)
        artifacts.symlink_to(real_artifacts, target_is_directory=True)
        with self.assertRaises(FileNotFoundError):
            self.store.candidate_artifact("run_safe", "candidate_a", "model.scad")

        export = self.root / "datasets" / "export_safe"
        export.mkdir()
        (export / "dataset.json").write_text("{}", encoding="utf-8")
        real_export = export.with_name("real-export")
        export.rename(real_export)
        export.symlink_to(real_export, target_is_directory=True)
        with self.assertRaises(FileNotFoundError):
            self.store.dataset_file("export_safe", "dataset.json")

    def test_demo_and_real_runs_are_persisted_in_separate_histories(self) -> None:
        self.create_run("real_run", demo=False)
        self.create_run("demo_run", demo=True)

        real_only = self.store.list_runs(include_demo=False)
        all_runs = self.store.list_runs(include_demo=True)
        self.assertEqual([run["run_id"] for run in real_only], ["real_run"])
        self.assertEqual({run["run_id"] for run in all_runs}, {"real_run", "demo_run"})
        self.assertTrue((self.root / "runs" / "real_run" / "run.json").is_file())
        self.assertTrue((self.root / "demo_runs" / "demo_run" / "run.json").is_file())
        self.assertFalse((self.root / "runs" / "demo_run").exists())
        self.assertFalse((self.root / "demo_runs" / "real_run").exists())
        demo_events = self.store.list_events("demo_run")
        real_events = self.store.list_events("real_run")
        self.assertTrue(all(event["demo"] for event in demo_events))
        self.assertTrue(all(not event["demo"] for event in real_events))
        self.assert_user_data_untouched()


if __name__ == "__main__":
    unittest.main()
