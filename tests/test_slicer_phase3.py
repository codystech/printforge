"""Mocked CPU-only Phase 3 slicing and hard-gate tests."""

from __future__ import annotations

import io
import json
import subprocess
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from evolution_lab.cadquery import CadQueryPipeline, SandboxResult
from evolution_lab.config import EvolutionLabConfig
from evolution_lab.dataset_v2 import printable_artifact
from evolution_lab.router import create_router
from evolution_lab.scoring import score_candidate
from evolution_lab.slicer import (
    ADAPTER_VERSION,
    LOG_ARTIFACT,
    SLICE_ARTIFACT,
    BambuBinaryIdentity,
    BambuProfileBundle,
    BambuStudioCLIAdapter,
    SlicerError,
    runtime_readiness,
)


MODEL_SOURCE = '''\
import cadquery as cq
PARAMETERS = {"width": {"type": "float", "default": 20.0, "min": 10.0, "max": 40.0}}
def build(params, assets):
    return {"body": cq.Workplane("XY").box(params["width"], 10, 4)}
'''

PARTS = [{
    "name": "body", "export_role": "printable",
    "transform": {"translation_mm": [0, 0, 0], "rotation_deg": [0, 0, 0]},
    "step_artifact": "body.step", "stl_artifact": "body.stl",
}]


def profile(kind: str) -> bytes:
    return json.dumps({
        "type": kind, "name": f"Pinned {kind}", "from": "system",
        "instantiation": "true", "full_setting": ["fixture"],
    }, sort_keys=True).encode()


def sliced_3mf(*, metrics: bool = True, plate_gcode: bool = True) -> bytes:
    with io.BytesIO() as output:
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr(
                "3D/3dmodel.model",
                '<model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
                '<resources/><build><item objectid="1"/></build></model>',
            )
            gcode = "; generated plate gcode\n"
            if metrics:
                gcode += (
                    "; total estimated time: 1h 2m 3s\n"
                    "; total filament used [g] = 12.5\n"
                    "; total layer number: 42\n"
                    "; support_used: false\n"
                )
            archive.writestr("Metadata/plate_1.gcode" if plate_gcode else "Metadata/slice.config", gcode)
        return output.getvalue()


class FakeCadQueryExecutor:
    def __init__(self, checks: dict[str, bool] | None = None):
        self.checks = {
            "brep_valid": True, "step_exported": True,
            "step_roundtrip_valid": True, "stl_tessellated": True,
            "mesh_checks_passed": True, "build_volume_ok": True,
            "hard_locks_ok": True, "reference_roles_excluded": True,
            **(checks or {}),
        }

    def execute(self, source, parameters, assets):
        return SandboxResult(
            report={"parts": PARTS, "checks": self.checks},
            artifacts={"body.step": b"STEP", "body.stl": b"solid fixture"},
            trusted_evidence=True,
        )


class SlicerPhase3Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = BambuProfileBundle(profile("machine"), profile("process"), profile("filament"))
        self.identity = BambuBinaryIdentity("2.3.4", "a" * 64)
        self.bwrap_identity = BambuBinaryIdentity("0.11.0", "a" * 64)
        self.commands: list[list[str]] = []
        trusted = patch(
            "evolution_lab.slicer._trusted_executable",
            side_effect=lambda value, label: Path(value),
        )
        checksum = patch("evolution_lab.slicer._file_sha256", return_value="sha256:" + "a" * 64)
        trusted.start()
        checksum.start()
        self.addCleanup(trusted.stop)
        self.addCleanup(checksum.stop)

    def adapter(self, *, output: bytes | None = None, returncode: int = 0):
        payload = sliced_3mf() if output is None else output

        def runner(command, **kwargs):
            self.commands.append(command)
            if payload:
                (Path(kwargs["cwd"]) / SLICE_ARTIFACT).write_bytes(payload)
            return subprocess.CompletedProcess(command, returncode, b"WARNING: fixture warning\n", b"")

        return BambuStudioCLIAdapter(
            self.profiles, binary_identity=self.identity, bwrap_identity=self.bwrap_identity,
            bambu_binary="/nix/store/test-bambu/bin/bambu-studio",
            bwrap_binary="/nix/store/test-bwrap/bin/bwrap", runner=runner,
        )

    def test_profiles_must_be_full_instantiated_snapshots(self) -> None:
        with self.assertRaisesRegex(SlicerError, "full JSON|full instantiated"):
            BambuProfileBundle(b"{}", profile("process"), profile("filament"))
        first = self.profiles.fingerprint
        second = BambuProfileBundle(profile("machine"), profile("process"), profile("filament")).fingerprint
        self.assertEqual(first, second)

    def test_documented_cli_contract_and_complete_evidence(self) -> None:
        result = self.adapter().slice({"body.stl": b"solid fixture"})
        self.assertEqual(result.results["status"], "complete")
        self.assertEqual(result.results["adapter_version"], ADAPTER_VERSION)
        self.assertEqual(result.results["estimated_time_seconds"], 3723)
        self.assertEqual(result.results["filament_grams"], 12.5)
        self.assertEqual(result.results["layer_count"], 42)
        self.assertFalse(result.results["support_used"])
        self.assertEqual(set(result.artifacts), {SLICE_ARTIFACT, LOG_ARTIFACT})
        command = self.commands[0]
        joined = " ".join(command)
        for fragment in (
            "--debug 3", "--outputdir /work", "--arrange 0",
            "--load-settings /work/profiles/machine.json;/work/profiles/process.json",
            "--load-filaments /work/profiles/filament.json",
            "--slice 0", f"--export-3mf /work/{SLICE_ARTIFACT}",
        ):
            self.assertIn(fragment, joined)
        self.assertIn("--unshare-all", command)
        self.assertNotIn("/home/cody", joined)
        self.assertNotIn("library", joined)
        self.assertNotIn("uploads", joined)

    def test_nonzero_empty_invalid_and_incomplete_slices_fail_closed(self) -> None:
        nonzero = self.adapter(returncode=2).slice({"body.stl": b"solid fixture"})
        self.assertIn("slice_failed", nonzero.results["failure_codes"])
        empty = self.adapter(output=b"").slice({"body.stl": b"solid fixture"})
        self.assertIn("slice_empty", empty.results["failure_codes"])
        invalid = self.adapter(output=b"not a zip").slice({"body.stl": b"solid fixture"})
        self.assertIn("slice_empty", invalid.results["failure_codes"])
        incomplete = self.adapter(output=sliced_3mf(metrics=False)).slice({"body.stl": b"solid fixture"})
        self.assertIn("slice_metrics_incomplete", incomplete.results["failure_codes"])
        no_plate = self.adapter(output=sliced_3mf(plate_gcode=False)).slice({"body.stl": b"solid fixture"})
        self.assertIn("slice_empty", no_plate.results["failure_codes"])

    def test_binary_identity_is_required_and_part_of_fingerprint(self) -> None:
        adapter = BambuStudioCLIAdapter(self.profiles, runner=lambda *args, **kwargs: None)
        result = adapter.slice({"body.stl": b"solid fixture"})
        self.assertEqual(result.results["failure_codes"], ["slicer_binary_unpinned"])
        other = BambuStudioCLIAdapter(
            self.profiles,
            binary_identity=BambuBinaryIdentity("2.3.5", "a" * 64),
            bwrap_identity=self.bwrap_identity,
        )
        self.assertNotEqual(self.adapter().evaluator_fingerprint, other.evaluator_fingerprint)

    def test_cadquery_pipeline_persists_slice_evidence_and_hard_rejects_failures(self) -> None:
        accepted = CadQueryPipeline(
            FakeCadQueryExecutor(), slicer=self.adapter(),
        ).evaluate(MODEL_SOURCE)
        self.assertFalse(accepted["hard_rejected"])
        self.assertEqual(accepted["slicer_results"]["status"], "complete")
        self.assertIn(SLICE_ARTIFACT, accepted["artifacts"])
        self.assertIn(LOG_ARTIFACT, accepted["artifacts"])
        self.assertEqual(
            accepted["manifest"]["slicer_profile_fingerprint"],
            accepted["slicer_profile_fingerprint"],
        )
        sliced_record = next(
            item for item in accepted["manifest"]["artifacts"]
            if item["name"] == SLICE_ARTIFACT
        )
        self.assertEqual(sliced_record["role"], "sliced-printable")
        self.assertIsNotNone(printable_artifact(
            {
                "model_format": "cadquery-v1",
                "slicer_results": accepted["slicer_results"],
                "artifacts": accepted["manifest"]["artifacts"],
            },
            sliced_record["sha256"], SLICE_ARTIFACT,
        ))

        failed = CadQueryPipeline(
            FakeCadQueryExecutor(),
            slicer=self.adapter(output=sliced_3mf(metrics=False)),
        ).evaluate(MODEL_SOURCE)
        self.assertTrue(failed["hard_rejected"])
        self.assertTrue(failed["promotion_blocked"])
        self.assertTrue(failed["bambuddy_send_blocked"])
        self.assertTrue(score_candidate([], failed["failure_codes"])["hard_rejected"])

    def test_geometry_failure_blocks_slicer_but_preserves_reason(self) -> None:
        adapter = self.adapter()
        result = CadQueryPipeline(
            FakeCadQueryExecutor({"brep_valid": False}), slicer=adapter,
        ).evaluate(MODEL_SOURCE)
        self.assertTrue(result["hard_rejected"])
        self.assertEqual(result["slicer_results"]["status"], "blocked")
        self.assertIn("invalid_brep", result["failure_codes"])
        self.assertEqual(self.commands, [])

    def test_readiness_and_bootstrap_stay_false_without_matching_real_smoke(self) -> None:
        with patch("evolution_lab.slicer.shutil.which", side_effect=lambda name: f"/nix/store/test/{name}"):
            status = runtime_readiness(profiles=self.profiles, binary_identity=self.identity)
        self.assertFalse(status["runtime_ready"])
        router = create_router(EvolutionLabConfig(
            training_lab_enabled=False, bambu_slicer_enabled=True,
        ))
        endpoint = next(route.endpoint for route in router.routes if route.path == "/training-lab/api/bootstrap")
        import asyncio
        bootstrap = asyncio.run(endpoint())
        self.assertTrue(bootstrap["capabilities"]["bambu_slicer_contract_supported"])
        self.assertTrue(bootstrap["capabilities"]["bambu_slicer_requested"])
        self.assertFalse(bootstrap["capabilities"]["bambu_slicer_runtime_ready"])
        self.assertFalse(bootstrap["capabilities"]["slicer"])
        self.assertFalse(bootstrap["capabilities"]["cadquery_exemplar_promotion"])


if __name__ == "__main__":
    unittest.main()
