"""CPU-only safety tests for Phase 0 ML preflight and QLoRA smoke guards."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from evolution_lab import ml_preflight
from evolution_lab.run_qlora_smoke import (
    MODEL_ID, GpuTelemetryPoller, active_compute_processes, adapter_file_hashes, bounded_steps,
    fixed_length_smoke_tokens, isolated_cache_environment, lab_output_path,
    main, parser, run_smoke, smoke_plan, training_proof_after_failure,
    validate_review_manifest,
)


class MlPreflightTests(unittest.TestCase):
    def test_unavailable_gpu_is_reported_without_claiming_training(self) -> None:
        packages = {
            name: {"installed": True, "version": version, "required_version": version, "compatible": True}
            for name, version in ml_preflight.PINNED_DISTRIBUTIONS.items()
        }
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            ml_preflight, "nvidia_devices", return_value={"paths": [], "nvidiactl": False, "gpu_device_present": False}
        ), patch.object(
            ml_preflight, "nvidia_smi_probe", return_value={"available": True, "ok": False, "reason": "no_device"}
        ), patch.object(
            ml_preflight, "package_versions", return_value=packages
        ), patch.object(
            ml_preflight,
            "torch_cuda_probe",
            return_value={"available": True, "ok": True, "import_ok": True, "cuda_available": False, "device_count": 0, "bf16_supported": False},
        ), patch.object(
            ml_preflight, "bitsandbytes_probe", return_value={"available": True, "ok": True, "import_ok": True}
        ), patch.object(
            ml_preflight, "disk_probe", return_value={"path": tempdir, "free_bytes": 100 * 1024**3, "free_gib": 100.0}
        ):
            report = ml_preflight.build_report(Path(tempdir))

        self.assertEqual(report["schema"], "printforge-ml-preflight-v1")
        self.assertEqual(report["status"], "not_ready_for_smoke")
        self.assertFalse(report["ready_for_smoke"])
        self.assertFalse(report["proof"]["host_gpu_bound"])
        self.assertFalse(report["proof"]["qlora_forward_backward_completed"])
        self.assertFalse(report["proof"]["actual_training"])
        self.assertFalse(report["proof"]["evaluated"])
        self.assertFalse(report["proof"]["deployed"])

    def test_report_writes_are_lab_only_and_refuse_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            path = root / "training_lab_data" / "nested" / "report.json"
            ml_preflight.write_json_new(path, {"value": 1}, root)
            with self.assertRaises(FileExistsError):
                ml_preflight.write_json_new(path, {"value": 2}, root)
            with self.assertRaises(argparse.ArgumentTypeError):
                ml_preflight.write_json_new(root / "outside.json", {}, root)
            self.assertIn('"value": 1', path.read_text(encoding="utf-8"))
            self.assertFalse(list(path.parent.glob("*.tmp")))

    def test_dependency_versions_are_exact_not_minimums(self) -> None:
        with patch("importlib.metadata.version", return_value="999.0"):
            packages = ml_preflight.package_versions()
        self.assertTrue(all(not row["compatible"] for row in packages.values()))


class QloraSmokeGuardTests(unittest.TestCase):
    def test_steps_are_hard_bounded_to_ten_through_fifty(self) -> None:
        self.assertEqual(bounded_steps("10"), 10)
        self.assertEqual(bounded_steps("50"), 50)
        for value in ("0", "9", "51", "100"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                bounded_steps(value)

    def test_output_must_be_isolated_under_training_lab_data(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            accepted = lab_output_path("training_lab_data/ml-smoke/run-1", root)
            self.assertEqual(accepted, root / "training_lab_data/ml-smoke/run-1")
            for rejected in ("training_lab_data", "library/run-1", "uploads/run-1", "../escape"):
                with self.subTest(rejected=rejected), self.assertRaises(argparse.ArgumentTypeError):
                    lab_output_path(rejected, root)

    def test_default_is_a_non_executing_no_download_plan(self) -> None:
        args = parser().parse_args([])
        plan = smoke_plan(args)

        self.assertFalse(args.execute)
        self.assertFalse(plan["network_download_allowed"])
        self.assertEqual(plan["steps"], 10)
        self.assertTrue(plan["quantization"]["load_in_4bit"])
        self.assertEqual(plan["quantization"]["bnb_4bit_quant_type"], "nf4")
        self.assertTrue(plan["quantization"]["bnb_4bit_use_double_quant"])
        self.assertEqual(plan["lora"]["target_modules"], "all-linear")
        self.assertFalse(plan["proof"]["actual_training"])
        self.assertFalse(plan["proof"]["evaluated"])
        self.assertFalse(plan["proof"]["deployed"])
        self.assertEqual(args.model_id, MODEL_ID)
        self.assertEqual(plan["required_package_versions"], ml_preflight.PINNED_DISTRIBUTIONS)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(["--model-id", "other/model"])

    def test_execute_refuses_mutable_or_missing_model_revision_before_gpu_work(self) -> None:
        args = parser().parse_args(["--execute"])
        with self.assertRaisesRegex(ValueError, "immutable 40-character commit hash"):
            run_smoke(args)

    def test_malformed_gpu_process_row_is_busy(self) -> None:
        completed = type("Result", (), {"returncode": 0, "stdout": "broken,row\n", "stderr": ""})()
        with patch("subprocess.run", return_value=completed):
            rows = active_compute_processes()
        self.assertEqual(len(rows), 1)
        self.assertIn("malformed", rows[0]["process_name"])

    def test_cache_environment_is_isolated_offline_and_no_telemetry(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir) / "training_lab_data" / "run"
            values = isolated_cache_environment(output, allow_download=False)
        for key, value in values.items():
            if key.endswith("CACHE") or key in {"HF_HOME", "TORCH_HOME", "TMPDIR", "HF_TOKEN_PATH", "CUDA_CACHE_PATH"}:
                self.assertTrue(value.startswith(str(output)), (key, value))
        self.assertEqual(values["HF_HUB_OFFLINE"], "1")
        self.assertEqual(values["HF_HUB_DISABLE_TELEMETRY"], "1")

    def test_gpu_poller_records_peak_temperature_and_whole_gpu_vram(self) -> None:
        samples = [
            {"memory_used_mib": 100, "temperature_c": 40},
            {"memory_used_mib": 900, "temperature_c": 72},
        ]
        poller = GpuTelemetryPoller(interval_seconds=60)
        with patch("evolution_lab.run_qlora_smoke.gpu_telemetry_sample", side_effect=samples):
            poller.start()
            summary = poller.stop()
        self.assertEqual(summary["peak_gpu_memory_used_mib"], 900)
        self.assertEqual(summary["peak_temperature_c"], 72)

    def test_review_manifest_pins_model_revision_and_evidence_hashes(self) -> None:
        revision = "a" * 40
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            review_dir = root / "training_lab_data" / "reviews" / revision
            review_dir.mkdir(parents=True)
            license_path = review_dir / "LICENSE"
            card_path = review_dir / "README.md"
            license_path.write_text("Apache License 2.0", encoding="utf-8")
            card_path.write_text("Qwen model card", encoding="utf-8")
            digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            manifest = {
                "schema": "printforge-model-review-v1", "decision": "approved",
                "model_id": MODEL_ID, "model_revision": revision, "license_spdx": "Apache-2.0",
                "source_url": f"https://huggingface.co/{MODEL_ID}/tree/{revision}",
                "reviewed_by": "human", "reviewed_at": "2026-07-13T12:00:00Z",
                "license_artifact": "LICENSE", "license_sha256": digest(license_path),
                "model_card_artifact": "README.md", "model_card_sha256": digest(card_path),
            }
            path = review_dir / "review.json"
            path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
            evidence = validate_review_manifest(path, digest(path), revision, root)
            self.assertEqual(evidence["model_revision"], revision)
            with self.assertRaisesRegex(ValueError, "checksum"):
                validate_review_manifest(path, "0" * 64, revision, root)
            for invalid_timestamp in ("2026-07-13", "2026-07-13T12:00:00"):
                with self.subTest(reviewed_at=invalid_timestamp):
                    manifest["reviewed_at"] = invalid_timestamp
                    path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "with timezone"):
                        validate_review_manifest(path, digest(path), revision, root)

    def test_fixed_fixture_uses_exact_requested_length_and_completion_labels(self) -> None:
        class Tokenizer:
            def encode(self, text, add_special_tokens):
                return ([1] if add_special_tokens else []) + list(range(2, 2 + max(1, len(text) // 20)))
        fixture = fixed_length_smoke_tokens(Tokenizer(), 256)
        self.assertEqual(fixture["sequence_length"], 256)
        self.assertEqual(len(fixture["input_ids"]), 256)
        self.assertTrue(all(value == -100 for value in fixture["labels"][:fixture["prompt_tokens"]]))

    def test_adapter_hashes_and_failed_run_truth_follow_durable_step_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir)
            adapter = output / "adapter"
            adapter.mkdir()
            (adapter / "adapter.safetensors").write_bytes(b"weights")
            hashes = adapter_file_hashes(adapter)
            self.assertEqual(hashes["adapter.safetensors"], hashlib.sha256(b"weights").hexdigest())
            (output / "optimizer-step-000001.json").write_text("{}", encoding="utf-8")
            updates, proof = training_proof_after_failure(output)
            self.assertEqual(updates, 1)
            self.assertTrue(proof["actual_training"])
            self.assertFalse(proof["qlora_forward_backward_completed"])

    def test_failure_handler_never_writes_into_preexisting_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            output = Path(tempdir)
            sentinel = output / "sentinel"
            sentinel.write_text("unchanged", encoding="utf-8")
            args = parser().parse_args(["--execute"])
            args.output_dir = output

            class ParsedArguments:
                def parse_args(self, argv):
                    return args

            with patch("evolution_lab.run_qlora_smoke.parser", return_value=ParsedArguments()), patch(
                "evolution_lab.run_qlora_smoke.run_smoke",
                side_effect=FileExistsError("smoke output is immutable"),
            ), patch("evolution_lab.run_qlora_smoke.write_json_new") as write_json, patch("builtins.print"):
                self.assertEqual(main([]), 1)
            write_json.assert_not_called()
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
            self.assertFalse((output / "failure.json").exists())


if __name__ == "__main__":
    unittest.main()
