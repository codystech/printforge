from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evolution_lab.cadquery import (
    BubblewrapExecutor,
    CadQueryContractError,
    CadQuerySandboxError,
    CadQueryPipeline,
    SandboxLimits,
    SandboxResult,
    model_envelope,
    parse_model_contract,
    validate_parameter_values,
)
from evolution_lab.config import EvolutionLabConfig
from evolution_lab.engine import EvolutionAdapters
from evolution_lab.router import create_router
from evolution_lab.scoring import deterministic_candidate_eligible, score_candidate, select_winner
from evolution_lab.store import EvolutionStore


MODEL_SOURCE = '''\
import cadquery as cq

PARAMETERS = {
    "width": {"type": "float", "default": 24.0, "min": 10.0, "max": 80.0, "step": 0.5, "unit": "mm"},
    "label": {"type": "str", "default": "PF"},
    "enabled": {"type": "bool", "default": True},
}

def build(params, assets):
    return {"body": cq.Workplane("XY").box(params["width"], 12, 4)}
'''


PARTS = [{
    "name": "body",
    "export_role": "printable",
    "transform": {"translation_mm": [0, 0, 0], "rotation_deg": [0, 0, 0]},
    "step_artifact": "body.step",
    "stl_artifact": "body.stl",
}]


class FakeExecutor:
    def __init__(self, checks: dict[str, bool] | None = None):
        self.checks = {
            "brep_valid": True,
            "step_exported": True,
            "step_roundtrip_valid": True,
            "stl_tessellated": True,
            "mesh_checks_passed": True,
            "build_volume_ok": True,
            "hard_locks_ok": True,
            "reference_roles_excluded": True,
            **(checks or {}),
        }
        self.calls = []

    def execute(self, source, parameters, assets):
        self.calls.append((source, dict(parameters), dict(assets)))
        return SandboxResult(
            report={"parts": PARTS, "checks": self.checks},
            artifacts={"body.step": b"STEP", "body.stl": b"solid body"},
            trusted_evidence=True,
        )


class FakeSlicer:
    def slice(self, stls):
        return type("Slice", (), {
            "results": {
                "status": "complete",
                "profile_fingerprint": "sha256:" + "b" * 64,
                "estimated_time_seconds": 60,
                "filament_grams": 1.0,
                "layer_count": 10,
                "support_used": False,
                "warnings": [],
                "sliced_3mf_artifact": "candidate.sliced.3mf",
                "log_artifact": "candidate.slicer.log",
            },
            "artifacts": {
                "candidate.sliced.3mf": b"fixture 3mf",
                "candidate.slicer.log": b"fixture log",
            },
        })()


class CadQueryContractTests(unittest.TestCase):
    def test_parameters_are_literal_parsed_without_executing_source(self) -> None:
        marker = Path("/tmp/printforge-cadquery-parser-must-not-run")
        marker.unlink(missing_ok=True)
        hostile = f'''\
from pathlib import Path
PARAMETERS = Path({str(marker)!r}).write_text("executed")
def build(params, assets):
    return {{}}
'''
        with self.assertRaisesRegex(CadQueryContractError, "literal mapping"):
            parse_model_contract(hostile)
        self.assertFalse(marker.exists())

    def test_contract_requires_exact_build_signature_and_typed_schema(self) -> None:
        parsed = parse_model_contract(MODEL_SOURCE)
        self.assertEqual(parsed["model_format"], "cadquery-v1")
        self.assertEqual(parsed["parameters"]["width"]["default"], 24.0)
        with self.assertRaisesRegex(CadQueryContractError, "exactly build"):
            parse_model_contract(MODEL_SOURCE.replace("build(params, assets)", "build(params)"))
        with self.assertRaisesRegex(CadQueryContractError, "default is above max"):
            parse_model_contract(MODEL_SOURCE.replace('"default": 24.0', '"default": 240.0'))

    def test_parameter_overrides_are_bounded_and_unknowns_rejected(self) -> None:
        schema = parse_model_contract(MODEL_SOURCE)["parameters"]
        values = validate_parameter_values(schema, {"width": 40})
        self.assertEqual(values, {"width": 40, "label": "PF", "enabled": True})
        with self.assertRaisesRegex(CadQueryContractError, "above max"):
            validate_parameter_values(schema, {"width": 1000})
        with self.assertRaisesRegex(CadQueryContractError, "unknown"):
            validate_parameter_values(schema, {"shell": 2})
        with self.assertRaisesRegex(CadQueryContractError, "finite"):
            validate_parameter_values(schema, {"width": float("nan")})
        with self.assertRaisesRegex(CadQueryContractError, "representable|numeric limit"):
            validate_parameter_values(schema, {"width": 10 ** 1000})

    def test_source_ast_and_parameter_text_limits_fail_closed(self) -> None:
        with patch("evolution_lab.cadquery.MAX_SOURCE_BYTES", 32):
            with self.assertRaisesRegex(CadQueryContractError, "source limit"):
                parse_model_contract(MODEL_SOURCE)
        with patch("evolution_lab.cadquery.MAX_AST_NODES", 10):
            with self.assertRaisesRegex(CadQueryContractError, "node AST limit"):
                parse_model_contract(MODEL_SOURCE)
        with patch("evolution_lab.cadquery.MAX_AST_DEPTH", 3):
            with self.assertRaisesRegex(CadQueryContractError, "AST depth"):
                parse_model_contract(MODEL_SOURCE)
        with patch("evolution_lab.cadquery.MAX_PARAMETER_TEXT_BYTES", 1):
            with self.assertRaisesRegex(CadQueryContractError, "aggregate text limit"):
                parse_model_contract(MODEL_SOURCE)


class CadQueryPipelineTests(unittest.TestCase):
    def test_valid_pipeline_emits_versioned_immutable_manifest_and_artifacts(self) -> None:
        fake = FakeExecutor()
        result = CadQueryPipeline(fake, slicer=FakeSlicer()).evaluate(
            MODEL_SOURCE, parameter_values={"width": 30}
        )
        self.assertFalse(result["hard_rejected"])
        self.assertEqual(result["model_format"], "cadquery-v1")
        self.assertEqual(result["manifest"]["manifest_version"], "printforge-cadquery-manifest-v1")
        self.assertEqual(result["manifest"]["contract_version"], "printforge-cadquery-model-v1")
        self.assertEqual(result["parts"], [{**PARTS[0], "printable": True,
            "transform": {"translation_mm": [0.0, 0.0, 0.0], "rotation_deg": [0.0, 0.0, 0.0]}}])
        manifest = json.loads(result["artifacts"]["model-manifest.json"])
        self.assertEqual(manifest["artifact_id"], result["artifact_id"])
        self.assertEqual(fake.calls[0][1]["width"], 30)
        self.assertEqual(result["artifacts"]["model.py"], MODEL_SOURCE)

    def test_injected_executor_cannot_overwrite_canonical_pipeline_artifacts(self) -> None:
        class CollidingExecutor(FakeExecutor):
            def execute(self, source, parameters, assets):
                result = super().execute(source, parameters, assets)
                result.artifacts["model.py"] = b"hostile replacement"
                return result

        with self.assertRaisesRegex(CadQuerySandboxError, "reserved artifacts"):
            CadQueryPipeline(CollidingExecutor()).evaluate(MODEL_SOURCE)

    def test_every_geometry_stage_is_a_deterministic_hard_gate(self) -> None:
        mapping = {
            "brep_valid": "invalid_brep",
            "step_exported": "step_export_failed",
            "step_roundtrip_valid": "step_roundtrip_failed",
            "stl_tessellated": "stl_tessellation_failed",
            "mesh_checks_passed": "mesh_validation_failed",
            "build_volume_ok": "build_volume_overflow",
            "hard_locks_ok": "broken_hard_lock",
            "reference_roles_excluded": "reference_export_leakage",
        }
        for check, code in mapping.items():
            with self.subTest(check=check):
                result = CadQueryPipeline(FakeExecutor({check: False})).evaluate(MODEL_SOURCE)
                self.assertTrue(result["hard_rejected"])
                self.assertIn(code, result["failure_codes"])
                score = score_candidate([], result["failure_codes"])
                self.assertTrue(score["hard_rejected"])
                self.assertIn(code, score["hard_rejection_reasons"])

    def test_named_parts_require_explicit_transform_role_and_unique_step_stl(self) -> None:
        invalid_parts = [{**PARTS[0], "export_role": "mystery"}]

        class InvalidExecutor(FakeExecutor):
            def execute(self, source, parameters, assets):
                result = super().execute(source, parameters, assets)
                result.report["parts"] = invalid_parts
                return result

        with self.assertRaisesRegex(CadQueryContractError, "invalid export_role"):
            CadQueryPipeline(InvalidExecutor()).evaluate(MODEL_SOURCE)

        nonfinite_parts = [{**PARTS[0], "transform": {
            "translation_mm": [float("inf"), 0, 0], "rotation_deg": [0, 0, 0],
        }}]

        class NonfiniteExecutor(FakeExecutor):
            def execute(self, source, parameters, assets):
                result = super().execute(source, parameters, assets)
                result.report["parts"] = nonfinite_parts
                return result

        with self.assertRaisesRegex(CadQueryContractError, "finite"):
            CadQueryPipeline(NonfiniteExecutor()).evaluate(MODEL_SOURCE)

    def test_generic_envelope_exposes_scad_aliases_only_for_legacy(self) -> None:
        cadquery = model_envelope(model_format="cadquery-v1", source=MODEL_SOURCE)
        self.assertEqual(set(("model_format", "source", "parameters", "parts", "artifact_id")) - set(cadquery), set())
        self.assertNotIn("scad", cadquery)
        self.assertNotIn("params", cadquery)
        self.assertIsNone(cadquery["artifact_id"])
        self.assertTrue(cadquery["source_available"])
        unavailable = model_envelope(model_format="cadquery-v1", source=None)
        self.assertIsNone(unavailable["source"])
        self.assertFalse(unavailable["source_available"])
        self.assertIsNone(unavailable["artifact_id"])
        legacy = model_envelope(model_format="openscad-legacy", source="cube(1);")
        self.assertEqual(legacy["scad"], "cube(1);")
        self.assertIn("params", legacy)


class BubblewrapProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bwrap_guard = patch(
            "evolution_lab.cadquery._validated_bwrap_executable",
            return_value=Path("/nix/store/printforge-test-bwrap/bin/bwrap"),
        )
        self.bwrap_guard.start()
        self.addCleanup(self.bwrap_guard.stop)

    @staticmethod
    def runtime() -> str:
        # Mocked subprocess tests need an existing canonical executable beneath
        # the production allowlist; the runner never executes this binary.
        return "/usr/local/bin/grype"

    def test_profile_has_no_network_home_or_repository_mount_and_only_scratch_is_writable(self) -> None:
        executor = BubblewrapExecutor(
            "# injected worker",
            bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap",
            runtime_command=(self.runtime(),),
        )
        command = executor.command(Path("/tmp/cq-scratch"))
        joined = " ".join(command)
        self.assertIn("--unshare-all", command)
        self.assertIn("--clearenv", command)
        self.assertIn("--bind /tmp/cq-scratch /work", joined)
        self.assertIn("--setenv HOME /nonexistent", joined)
        self.assertIn("--setenv TMPDIR /work/tmp", joined)
        self.assertIn("--symlink /work/tmp /tmp", joined)
        self.assertNotIn("--tmpfs /tmp", joined)
        self.assertNotIn("/home/cody", joined)
        self.assertNotIn("/home/cody/projects/printforge", joined)
        self.assertNotIn("library", joined)
        self.assertNotIn("uploads", joined)
        writable_binds = [command[i + 1:i + 3] for i, item in enumerate(command) if item == "--bind"]
        self.assertEqual(writable_binds, [["/tmp/cq-scratch", "/work"]])

    def test_executor_collects_only_declared_scratch_artifacts_with_mocked_process(self) -> None:
        def runner(command, **kwargs):
            scratch = Path(kwargs["cwd"])
            (scratch / "output" / "body.step").write_bytes(b"STEP")
            (scratch / "output" / "body.stl").write_bytes(b"STL")
            (scratch / "result.json").write_text(json.dumps({
                "parts": PARTS,
                "checks": {},
                "artifacts": ["body.step", "body.stl"],
            }))
            return subprocess.CompletedProcess(command, 0, "ok", "")

        # The process is mocked; an existing file satisfies the availability guard
        # without requiring Bubblewrap to be installed on the test host.
        executor = BubblewrapExecutor(
            "# worker",
            bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap",
            runtime_command=(self.runtime(),),
            runner=runner,
        )
        result = executor.execute(MODEL_SOURCE, {"width": 24}, {"insert.step": b"asset"})
        self.assertEqual(result.artifacts, {"body.step": b"STEP", "body.stl": b"STL"})
        self.assertFalse(result.trusted_evidence)

    def test_untrusted_worker_report_cannot_satisfy_pipeline_gates(self) -> None:
        with self.assertRaisesRegex(CadQuerySandboxError, "independent trusted validation"):
            CadQueryPipeline(type("Untrusted", (), {"execute": lambda self, *a, **k: SandboxResult(
                report={"parts": PARTS, "checks": FakeExecutor().checks},
                artifacts={"body.step": b"STEP", "body.stl": b"STL"},
            )})()).evaluate(MODEL_SOURCE)

    def test_worker_gate_claims_are_ignored_in_favor_of_parent_file_checks(self) -> None:
        observed = {}

        def runner(command, **kwargs):
            scratch = Path(kwargs["cwd"])
            (scratch / "output" / "body.step").write_bytes(b"trusted-step-bytes")
            (scratch / "output" / "body.stl").write_bytes(b"trusted-stl-bytes")
            (scratch / "result.json").write_text(json.dumps({
                "parts": PARTS,
                "checks": {key: True for key in FakeExecutor().checks},
                "artifacts": ["body.step", "body.stl"],
            }))
            return subprocess.CompletedProcess(command, 0, "ok", "")

        def validator(*, parts, artifact_paths):
            observed["step"] = artifact_paths["body.step"].read_bytes()
            self.assertEqual(parts[0]["export_role"], "printable")
            return {**FakeExecutor().checks, "brep_valid": False}

        executor = BubblewrapExecutor(
            "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap", runtime_command=(self.runtime(),),
            runner=runner, trusted_validator=validator,
        )
        result = CadQueryPipeline(executor).evaluate(MODEL_SOURCE)
        self.assertEqual(observed["step"], b"trusted-step-bytes")
        self.assertTrue(result["hard_rejected"])
        self.assertIn("invalid_brep", result["failure_codes"])

    def test_symlink_outputs_and_runtime_paths_are_rejected_before_resolution(self) -> None:
        def runner(command, **kwargs):
            scratch = Path(kwargs["cwd"])
            (scratch / "output" / "body.step").symlink_to("/etc/passwd")
            (scratch / "output" / "body.stl").write_bytes(b"STL")
            (scratch / "result.json").write_text(json.dumps({
                "parts": PARTS, "artifacts": ["body.step", "body.stl"],
            }))
            return subprocess.CompletedProcess(command, 0, "", "")

        executor = BubblewrapExecutor(
            "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap", runtime_command=(self.runtime(),), runner=runner,
        )
        with self.assertRaisesRegex(CadQuerySandboxError, "independent regular file"):
            executor.execute(MODEL_SOURCE, {}, {})

        with tempfile.TemporaryDirectory() as tempdir:
            alias = Path(tempdir) / "python"
            alias.symlink_to(self.runtime())
            with self.assertRaisesRegex(ValueError, "not a symlink"):
                BubblewrapExecutor(
                    "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap",
                    runtime_command=(str(alias),),
                )
        with self.assertRaisesRegex(ValueError, "not allowlisted"):
            BubblewrapExecutor(
                "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap",
                runtime_command=(self.runtime(),), runtime_roots=("/home",),
            )

    def test_asset_artifact_log_report_and_scratch_limits_are_independent(self) -> None:
        with self.assertRaisesRegex(CadQueryContractError, "aggregate byte limit"):
            BubblewrapExecutor(
                "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap", runtime_command=(self.runtime(),),
                limits=SandboxLimits(max_asset_bytes=10, max_total_asset_bytes=3),
                runner=lambda *a, **k: None,
            ).execute(MODEL_SOURCE, {}, {"a.step": b"12", "b.step": b"34"})

        def runner(command, **kwargs):
            scratch = Path(kwargs["cwd"])
            (scratch / "output" / "body.step").write_bytes(b"STEP")
            (scratch / "output" / "body.stl").write_bytes(b"STL")
            (scratch / "result.json").write_text(json.dumps({
                "parts": PARTS, "artifacts": ["body.step", "body.stl"],
            }))
            return subprocess.CompletedProcess(command, 0, "log-too-long", "")

        executor = BubblewrapExecutor(
            "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap", runtime_command=(self.runtime(),),
            limits=SandboxLimits(max_log_bytes=3), runner=runner,
        )
        with self.assertRaisesRegex(CadQuerySandboxError, "stdout exceeded"):
            executor.execute(MODEL_SOURCE, {}, {})

    def test_scratch_accounting_counts_empty_dirs_and_never_follows_directory_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            parent = Path(tempdir)
            root = parent / "work"
            root.mkdir()
            for index in range(8):
                (root / f"empty-{index}").mkdir()
            with self.assertRaisesRegex(CadQuerySandboxError, "file-count limit"):
                BubblewrapExecutor._tree_usage(root, max_entries=4)

            external = parent / "external"
            external.mkdir()
            for index in range(20):
                (external / f"outside-{index}").mkdir()
            isolated = parent / "isolated"
            isolated.mkdir()
            (isolated / "empty").mkdir()
            (isolated / "linked-dir").symlink_to(external, target_is_directory=True)
            _, entries = BubblewrapExecutor._tree_usage(isolated, max_entries=2)
            self.assertEqual(entries, 2)

    def test_scratch_accounting_fails_closed_when_scandir_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "work"
            root.mkdir()
            with patch("evolution_lab.cadquery.os.scandir", side_effect=PermissionError("denied")):
                with self.assertRaisesRegex(CadQuerySandboxError, "could not be accounted"):
                    BubblewrapExecutor._tree_usage(root)

    def test_reserved_artifact_names_cannot_collide_with_canonical_files(self) -> None:
        for reserved in ("model.py", "model-manifest.json", "result.json"):
            with self.subTest(reserved=reserved):
                def runner(command, **kwargs):
                    scratch = Path(kwargs["cwd"])
                    (scratch / "output" / reserved).write_bytes(b"collision")
                    (scratch / "result.json").write_text(json.dumps({
                        "parts": PARTS, "artifacts": [reserved],
                    }))
                    return subprocess.CompletedProcess(command, 0, "", "")

                executor = BubblewrapExecutor(
                    "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap",
                    runtime_command=(self.runtime(),), runner=runner,
                )
                with self.assertRaisesRegex(CadQuerySandboxError, "reserved artifact name"):
                    executor.execute(MODEL_SOURCE, {}, {})

    def test_output_directory_replacement_and_hardlinked_result_are_rejected(self) -> None:
        def replaced_output(command, **kwargs):
            scratch = Path(kwargs["cwd"])
            (scratch / "output").rmdir()
            (scratch / "output").symlink_to(scratch / "assets", target_is_directory=True)
            (scratch / "result.json").write_text(json.dumps({"parts": PARTS, "artifacts": []}))
            return subprocess.CompletedProcess(command, 0, "", "")

        executor = BubblewrapExecutor(
            "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap",
            runtime_command=(self.runtime(),), runner=replaced_output,
        )
        with self.assertRaisesRegex(CadQuerySandboxError, "replaced its output directory"):
            executor.execute(MODEL_SOURCE, {}, {})

        def hardlinked_result(command, **kwargs):
            scratch = Path(kwargs["cwd"])
            source = scratch / "result-source.json"
            source.write_text(json.dumps({"parts": PARTS, "artifacts": []}))
            os.link(source, scratch / "result.json")
            return subprocess.CompletedProcess(command, 0, "", "")

        executor = BubblewrapExecutor(
            "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap",
            runtime_command=(self.runtime(),), runner=hardlinked_result,
        )
        with self.assertRaisesRegex(CadQuerySandboxError, "independent regular file"):
            executor.execute(MODEL_SOURCE, {}, {})

    def test_trusted_validator_cannot_mutate_replace_or_symlink_captured_artifacts(self) -> None:
        def runner(command, **kwargs):
            scratch = Path(kwargs["cwd"])
            (scratch / "output" / "body.step").write_bytes(b"STEP")
            (scratch / "output" / "body.stl").write_bytes(b"STL!")
            (scratch / "result.json").write_text(json.dumps({
                "parts": PARTS, "artifacts": ["body.step", "body.stl"],
            }))
            return subprocess.CompletedProcess(command, 0, "", "")

        for action in ("mutate", "replace", "symlink"):
            with self.subTest(action=action):
                def validator(*, parts, artifact_paths):
                    target = artifact_paths["body.step"]
                    if action == "mutate":
                        target.write_bytes(b"EVIL")
                    elif action == "replace":
                        target.unlink()
                        target.write_bytes(b"STEP")
                    else:
                        target.unlink()
                        target.symlink_to(artifact_paths["body.stl"])
                    return FakeExecutor().checks

                executor = BubblewrapExecutor(
                    "# worker", bwrap_binary="/nix/store/printforge-test-bwrap/bin/bwrap",
                    runtime_command=(self.runtime(),), runner=runner,
                    trusted_validator=validator,
                )
                with self.assertRaisesRegex(CadQuerySandboxError, "changed during trusted validation"):
                    executor.execute(MODEL_SOURCE, {}, {})


class BubblewrapExecutableValidationTests(unittest.TestCase):
    def test_bwrap_path_must_be_absolute_canonical_root_owned_and_not_writable(self) -> None:
        from evolution_lab.cadquery import _validated_bwrap_executable

        with self.assertRaisesRegex(ValueError, "explicit absolute"):
            _validated_bwrap_executable("bwrap")

        fake_regular = type("Metadata", (), {
            "st_mode": stat.S_IFREG | 0o755, "st_uid": 1000,
        })()
        with patch("evolution_lab.cadquery.os.lstat", return_value=fake_regular):
            with self.assertRaisesRegex(ValueError, "root-owned"):
                _validated_bwrap_executable("/usr/bin/bwrap")

        fake_writable = type("Metadata", (), {
            "st_mode": stat.S_IFREG | 0o775, "st_uid": 0,
        })()
        with patch("evolution_lab.cadquery.os.lstat", return_value=fake_writable):
            with self.assertRaisesRegex(ValueError, "group- or world-writable"):
                _validated_bwrap_executable("/usr/bin/bwrap")


class DeterministicEligibilityTests(unittest.TestCase):
    def test_required_checks_must_be_explicitly_true_before_ranking(self) -> None:
        base = {"candidate_id": "safe", "status": "evaluated", "required_checks_passed": True,
                "hard_rejected": False, "failure_codes": [], "score": {"total": 90, "hard_rejected": False}}
        self.assertTrue(deterministic_candidate_eligible(base))
        for mutation in (
            {"required_checks_passed": None},
            {"hard_rejected": True},
            {"failure_codes": ["invalid_brep"]},
            {"score": {"total": 100, "hard_rejected": True}},
            {"score": {"total": float("nan"), "hard_rejected": False}},
            {"score": {"total": float("inf"), "hard_rejected": False}},
            {"score": {"total": float("-inf"), "hard_rejected": False}},
            {"score": {"total": 10 ** 1000, "hard_rejected": False}},
            {"score": {"total": "90", "hard_rejected": False}},
        ):
            with self.subTest(mutation=mutation):
                self.assertFalse(deterministic_candidate_eligible({**base, **mutation}))
        unsafe = {**base, "candidate_id": "unsafe", "score": {"total": 100, "hard_rejected": False}}
        unsafe.pop("required_checks_passed")
        selection = select_winner([unsafe, base], 80)
        self.assertEqual(selection["winner_candidate_id"], "safe")
        for invalid_best in (float("nan"), float("inf"), 10 ** 1000, "80"):
            with self.subTest(current_best_score=invalid_best):
                selection = select_winner([base], invalid_best)
                self.assertIsNone(selection["winner_candidate_id"])
                self.assertTrue(selection["current_best_preserved"])


class GenericCandidateApiTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def endpoint(router, path, method="GET"):
        return next(route.endpoint for route in router.routes if route.path == path and method in route.methods)

    async def test_candidate_api_uses_generic_fields_and_no_scad_alias_for_cadquery(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = EvolutionStore(Path(tempdir) / "lab")
            router = create_router(
                EvolutionLabConfig(
                    training_lab_enabled=True,
                    cadquery_enabled=True,
                    data_root=store.root,
                ),
                EvolutionAdapters(current_branch=lambda: "test"),
                store=store,
            )
            store.create_run(
                {"run_id": "run_cq", "demo": False, "status": "created", "created_at": 1},
                {"request.json": "{}"},
            )
            store.create_candidate("run_cq", {
                "candidate_id": "candidate_cq",
                "run_id": "run_cq",
                "model_format": "cadquery-v1",
                "parameters": {"width": {"type": "float", "default": 24}},
                "parts": PARTS,
                "artifact_id": "immutable-cq-id",
                "created_at": 2,
            })
            store.add_candidate_artifacts("run_cq", "candidate_cq", {"model.py": MODEL_SOURCE})
            response = await self.endpoint(
                router, "/training-lab/api/candidates/{candidate_id}"
            )("candidate_cq")
            bootstrap = await self.endpoint(router, "/training-lab/api/bootstrap")()
            self.assertEqual(response["source"], MODEL_SOURCE)
            self.assertEqual(response["model_format"], "cadquery-v1")
            self.assertEqual(response["artifact_id"], "immutable-cq-id")
            self.assertTrue(response["source_available"])
            self.assertNotIn("scad", response)
            self.assertNotIn("params", response)
            self.assertFalse(bootstrap["capabilities"]["cadquery_v1"])
            self.assertTrue(bootstrap["capabilities"]["cadquery_v1_contract_supported"])
            self.assertTrue(bootstrap["capabilities"]["cadquery_v1_requested"])
            self.assertFalse(bootstrap["capabilities"]["cadquery_v1_runtime_ready"])

    async def test_candidate_artifact_roles_preserve_legacy_stl_and_cadquery_part_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = EvolutionStore(Path(tempdir) / "lab")
            router = create_router(
                EvolutionLabConfig(training_lab_enabled=True, data_root=store.root),
                EvolutionAdapters(current_branch=lambda: "test"), store=store,
            )
            store.create_run(
                {"run_id": "run_roles", "demo": False, "status": "created", "created_at": 1},
                {"request.json": "{}"},
            )
            store.create_candidate("run_roles", {
                "candidate_id": "legacy_candidate", "run_id": "run_roles", "created_at": 2,
            })
            store.add_candidate_artifacts("run_roles", "legacy_candidate", {
                "model.scad": "cube(1);", "candidate.stl": b"STL",
                "qa-report.json": "{}", "evaluation.json": "{}",
            })
            endpoint = self.endpoint(router, "/training-lab/api/candidates/{candidate_id}/artifacts")
            legacy = await endpoint("legacy_candidate")
            legacy_roles = {item["name"]: item["role"] for item in legacy["artifacts"]}
            self.assertEqual(legacy_roles["candidate.stl"], "printable")
            self.assertEqual(legacy_roles["model.scad"], "metadata")
            self.assertEqual(legacy_roles["qa-report.json"], "metadata")
            self.assertEqual(legacy_roles["evaluation.json"], "metadata")

            store.create_candidate("run_roles", {
                "candidate_id": "cadquery_candidate", "run_id": "run_roles",
                "model_format": "cadquery-v1", "parts": PARTS, "created_at": 3,
            })
            store.add_candidate_artifacts("run_roles", "cadquery_candidate", {
                "model.py": MODEL_SOURCE, "body.step": b"STEP", "body.stl": b"STL",
                "evaluation.json": "{}",
            })
            cadquery = await endpoint("cadquery_candidate")
            cadquery_roles = {item["name"]: item["role"] for item in cadquery["artifacts"]}
            self.assertEqual(cadquery_roles["body.step"], "printable")
            self.assertEqual(cadquery_roles["body.stl"], "printable")
            self.assertEqual(cadquery_roles["model.py"], "metadata")
            self.assertEqual(cadquery_roles["evaluation.json"], "metadata")

    async def test_missing_cadquery_source_is_explicitly_unavailable_and_never_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            store = EvolutionStore(Path(tempdir) / "lab")
            router = create_router(
                EvolutionLabConfig(training_lab_enabled=True, data_root=store.root),
                EvolutionAdapters(current_branch=lambda: "test"), store=store,
            )
            store.create_run(
                {"run_id": "run_missing", "demo": False, "status": "created", "created_at": 1},
                {"request.json": "{}"},
            )
            store.create_candidate("run_missing", {
                "candidate_id": "candidate_missing", "run_id": "run_missing",
                "model_format": "cadquery-v1", "created_at": 2,
            })
            response = await self.endpoint(router, "/training-lab/api/candidates/{candidate_id}")(
                "candidate_missing"
            )
            self.assertIsNone(response["source"])
            self.assertFalse(response["source_available"])
            self.assertIsNone(response["artifact_id"])


if __name__ == "__main__":
    unittest.main()
