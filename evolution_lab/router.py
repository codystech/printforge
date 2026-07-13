"""Feature-gated HTTP API for the isolated Training Lab."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .benchmarks import benchmark_catalog
from .cadquery import model_envelope
from .config import EvolutionLabConfig
from .dataset_v2 import (
    SLICE_SUCCESS, artifact_role, canonical_sha256, has_verified_physical_failure,
    printable_artifact, profile_fingerprint, slice_evidence_ready,
)
from .datasets import create_export
from .demo import DEMO_RUN_ID, load_demo_fixture
from .engine import EvolutionAdapters, EvolutionEngine
from .memory import derive_status
from .schemas import (
    BenchmarkInput, CalibrationInput, CreateRunRequest, DatasetExportInput,
    MemoryObservationInput, MemoryReviewInput, MemoryRuleInput,
    PhysicalValidationInput, ProposalInput, ProposalStatusInput,
)
from .store import EvolutionStore, new_id, safe_id, utc_ts


def _dump(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return dict(value)


def _git_branch(adapters: EvolutionAdapters) -> str:
    if adapters.current_branch:
        try:
            return adapters.current_branch()
        except Exception:
            return "unavailable"
    try:
        return subprocess.run(
            ["git", "branch", "--show-current"], capture_output=True, text=True,
            timeout=2, check=False,
        ).stdout.strip() or "detached"
    except Exception:
        return "unavailable"


def create_router(
    config: EvolutionLabConfig | None = None,
    adapters: EvolutionAdapters | None = None,
    *,
    store: EvolutionStore | None = None,
    production_branch: str = "main",
) -> APIRouter:
    config = config or EvolutionLabConfig.from_env()
    adapters = adapters or EvolutionAdapters()
    router = APIRouter(prefix="/training-lab/api", tags=["training-lab"])
    engine: EvolutionEngine | None = None
    if config.training_lab_enabled:
        store = store or EvolutionStore(config.data_root)
        engine = EvolutionEngine(store, config, adapters)
        load_demo_fixture(store)

    def require_lab() -> tuple[EvolutionStore, EvolutionEngine]:
        if not config.training_lab_enabled or store is None or engine is None:
            raise HTTPException(403, "Training Lab is disabled")
        return store, engine

    def find_candidate(candidate_id: str) -> tuple[str, dict]:
        db, _ = require_lab()
        safe_id(candidate_id, "candidate id")
        for run in db.list_runs(include_demo=True):
            try:
                return run["run_id"], db.get_candidate(run["run_id"], candidate_id)
            except FileNotFoundError:
                continue
        raise HTTPException(404, "candidate not found")

    def candidate_response(run_id: str, record: dict) -> dict:
        """Expose generic model fields while preserving legacy SCAD aliases only."""

        model_format = record.get("model_format") or "openscad-legacy"
        source_name = "model.py" if model_format == "cadquery-v1" else "model.scad"
        try:
            source = store.candidate_artifact(  # type: ignore[union-attr]
                run_id, record["candidate_id"], source_name
            ).read_text(encoding="utf-8")
        except (OSError, ValueError, FileNotFoundError):
            source = None
        role_by_artifact = {
            part.get(key): part.get("export_role")
            for part in record.get("parts") or []
            for key in ("step_artifact", "stl_artifact")
            if part.get(key)
        }
        if model_format == "openscad-legacy":
            role_by_artifact.update({
                item.get("name"): "printable"
                for item in record.get("artifacts", [])
                if str(item.get("name") or "").lower().endswith(".stl")
            })
        slicer = record.get("slicer_results") if isinstance(record.get("slicer_results"), dict) else {}
        for name in (slicer.get("sliced_3mf_artifact"), slicer.get("log_artifact")):
            if name:
                role_by_artifact[name] = (
                    "sliced-printable" if name == slicer.get("sliced_3mf_artifact") else "slicer-evidence"
                )
        artifacts = [
            {**item, "role": role_by_artifact.get(item.get("name"), "metadata")}
            for item in record.get("artifacts", [])
        ]
        return {
            **record,
            "artifacts": artifacts,
            **model_envelope(
                model_format=model_format,
                source=source,
                parameters=record.get("parameters") or {},
                parts=record.get("parts") or [],
                artifact_id=record.get("artifact_id"),
            ),
        }

    def lifecycle_block_reason(run_id: str, record: dict) -> str | None:
        db, _ = require_lab()
        run = db.get_run(run_id)
        score = record.get("score") if isinstance(record.get("score"), dict) else {}
        if record.get("status") in {"failed", "rejected", "cancelled"}:
            return "candidate status is failed or rejected"
        if score.get("hard_rejected") is True or record.get("hard_rejected") is True:
            return "candidate has a deterministic hard rejection"
        if record.get("required_checks_passed") is not True:
            return "candidate has not passed required checks"
        if has_verified_physical_failure(db, run, record):
            return "candidate has a checksum-verified failed physical print"
        if record.get("model_format") == "cadquery-v1" and not slice_evidence_ready(db, run, record):
            return "CadQuery candidate lacks exact complete persisted Bambu slice evidence"
        return None

    @router.get("/bootstrap")
    async def bootstrap():
        flags = config.public_dict()
        slicer_status = {"runtime_ready": False, "reason": "slicer adapter is not configured"}
        if adapters.slicer_status:
            try:
                slicer_status = dict(adapters.slicer_status())
            except Exception as exc:
                slicer_status = {"runtime_ready": False, "reason": str(exc)[:500]}
        slicer_ready = bool(slicer_status.get("runtime_ready"))
        runs = store.list_runs(include_demo=True) if store else []
        active = next((row for row in runs if row.get("status") in {"running", "stopping", "interrupted"} and not row.get("demo")), None)
        return {
            "enabled": config.training_lab_enabled,
            "training_lab_enabled": config.training_lab_enabled,
            "feature_flags": flags,
            "current_branch": _git_branch(adapters),
            "production_branch": production_branch,
            "active_run": active,
            "runs": [{k: row.get(k) for k in ("run_id", "status", "demo", "created_at", "run_mode", "source_model_id", "current_generation", "current_best_score", "active_stage", "latest_failure")} for row in runs],
            "demo_run_id": DEMO_RUN_ID if store else None,
            "capabilities": {
                "safe_pause": False, "abort": True, "cancel": True, "stop_after_current_generation": True,
                "slicer": slicer_ready, "actual_model_training": False,
                "bambu_slicer_contract_supported": True,
                "bambu_slicer_requested": bool(config.bambu_slicer_enabled),
                "bambu_slicer_runtime_ready": slicer_ready,
                "bambu_slicer_readiness": slicer_status,
                "cadquery_v1": False,
                "cadquery_v1_contract_supported": True,
                "cadquery_v1_requested": bool(config.cadquery_enabled),
                "cadquery_v1_runtime_ready": False,
                "cadquery_exemplar_promotion": False,
            },
            "actual_training_performed": False,
        }

    @router.get("/runs")
    async def list_runs():
        db, _ = require_lab()
        return {"runs": db.list_runs(include_demo=True)}

    @router.post("/runs")
    async def create_run(request: CreateRunRequest):
        _, lab = require_lab()
        if not config.evolution_enabled:
            raise HTTPException(403, "Evolution is disabled")
        try:
            return await lab.create_run(request)
        except FileNotFoundError as exc:
            raise HTTPException(404, "Starting model no longer exists") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/runs/{run_id}")
    async def get_run(run_id: str):
        _, lab = require_lab()
        try:
            snapshot = lab.snapshot(run_id)
            snapshot["memory_rules"] = store.list_records("memory") if store else []
            return snapshot
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/runs/{run_id}/start")
    async def start_run(run_id: str):
        _, lab = require_lab()
        try:
            return lab.start(run_id)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/runs/{run_id}/stop-after-generation")
    async def stop_run(run_id: str):
        _, lab = require_lab()
        try:
            return lab.stop_after_generation(run_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.post("/runs/{run_id}/cancel")
    async def cancel_run(run_id: str):
        _, lab = require_lab()
        try:
            return lab.cancel(run_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/runs/{run_id}/events")
    async def events(run_id: str, after_seq: int = 0, limit: int = 1000):
        db, _ = require_lab()
        try:
            return {"events": db.list_events(run_id, after=max(0, after_seq), limit=limit)}
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/candidates/{candidate_id}")
    async def candidate(candidate_id: str):
        run_id, record = find_candidate(candidate_id)
        return candidate_response(run_id, record)

    @router.get("/candidates/{candidate_id}/artifacts")
    async def candidate_artifacts(candidate_id: str):
        run_id, record = find_candidate(candidate_id)
        model_format = record.get("model_format") or "openscad-legacy"
        role_by_artifact = {
            part.get(key): part.get("export_role")
            for part in record.get("parts") or []
            for key in ("step_artifact", "stl_artifact")
            if part.get(key)
        }
        if model_format == "openscad-legacy":
            role_by_artifact.update({
                item.get("name"): "printable"
                for item in record.get("artifacts", [])
                if str(item.get("name") or "").lower().endswith(".stl")
            })
        slicer = record.get("slicer_results") if isinstance(record.get("slicer_results"), dict) else {}
        for name in (slicer.get("sliced_3mf_artifact"), slicer.get("log_artifact")):
            if name:
                role_by_artifact[name] = (
                    "sliced-printable" if name == slicer.get("sliced_3mf_artifact") else "slicer-evidence"
                )
        files = []
        for item in record.get("artifacts", []):
            name = item.get("name")
            if not name:
                continue
            files.append({
                **item,
                "url": f"/training-lab/api/candidates/{candidate_id}/artifacts/{name}",
                "role": role_by_artifact.get(name, "metadata"),
            })
        return {"run_id": run_id, "candidate_id": candidate_id, "artifacts": files}

    @router.get("/candidates/{candidate_id}/artifacts/{name}")
    async def candidate_artifact(candidate_id: str, name: str):
        run_id, _ = find_candidate(candidate_id)
        try:
            path = store.candidate_artifact(run_id, candidate_id, name)  # type: ignore[union-attr]
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, filename=path.name)

    @router.post("/candidates/{candidate_id}/restore")
    async def restore_candidate(candidate_id: str):
        run_id, record = find_candidate(candidate_id)
        _, lab = require_lab()
        reason = lifecycle_block_reason(run_id, record)
        if reason:
            raise HTTPException(409, reason)
        try:
            return lab.restore_candidate(run_id, candidate_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/candidates/{candidate_id}/branch")
    async def branch_candidate(candidate_id: str):
        run_id, record = find_candidate(candidate_id)
        _, lab = require_lab()
        reason = lifecycle_block_reason(run_id, record)
        if reason:
            raise HTTPException(409, reason)
        try:
            return await lab.branch_candidate(run_id, candidate_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.delete("/candidates/{candidate_id}")
    async def delete_candidate(candidate_id: str):
        run_id, _ = find_candidate(candidate_id)
        _, lab = require_lab()
        try:
            return lab.delete_candidate(run_id, candidate_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/candidates/{candidate_id}/promote-exemplar")
    async def promote_exemplar(candidate_id: str):
        """Human-gated: promote a checks-passing candidate into the production library as a
        thumbs-up few-shot exemplar. Crosses the lab->production boundary on explicit request only."""
        run_id, record = find_candidate(candidate_id)
        db, _ = require_lab()
        if adapters.promote_exemplar is None and adapters.promote_exemplar_with_context is None:
            raise HTTPException(501, "promotion to production is not wired on this deployment")
        run = db.get_run(run_id)
        if run.get("current_best_candidate_id") != candidate_id:
            raise HTTPException(409, "only the run's current winning candidate can be promoted")
        reason = lifecycle_block_reason(run_id, record)
        if reason:
            raise HTTPException(409, reason)
        source_name = "model.py" if record.get("model_format") == "cadquery-v1" else "model.scad"
        try:
            source = store.candidate_artifact(run_id, candidate_id, source_name).read_text()  # type: ignore[union-attr]
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, f"candidate has no {source_name} to promote") from exc
        if record.get("model_format") == "cadquery-v1":
            raise HTTPException(409, "production exemplar promotion does not yet support cadquery-v1 source")
        spec = run.get("validated_spec") or run.get("source_prompt") or "evolution lab exemplar"
        score = record.get("score", {}).get("total") if isinstance(record.get("score"), dict) else record.get("score")
        promotion_context = {
            "run_id": run_id,
            "candidate_id": candidate_id,
            "model_format": record.get("model_format") or "openscad-legacy",
            "source_sha256": record.get("source_sha256"),
            "artifact_id": record.get("artifact_id"),
            "evaluator_fingerprint": record.get("evaluator_fingerprint"),
            "slicer_profile_fingerprint": record.get("slicer_profile_fingerprint"),
            "provenance_audit": record.get("provenance_audit"),
            "physical_outcomes": record.get("physical_outcomes") or [],
        }
        if adapters.promote_exemplar_with_context is not None:
            model_id = adapters.promote_exemplar_with_context(
                source, run.get("title") or spec, spec, score, candidate_id, promotion_context
            )
        else:
            model_id = adapters.promote_exemplar(source, run.get("title") or spec, spec, score, candidate_id)
        return {"promoted": True, "library_model_id": model_id, "candidate_id": candidate_id}

    @router.post("/candidates/{candidate_id}/revoke-exemplar")
    async def revoke_exemplar(candidate_id: str):
        find_candidate(candidate_id)
        if adapters.revoke_exemplar is None:
            raise HTTPException(501, "promotion to production is not wired on this deployment")
        return {"revoked": adapters.revoke_exemplar(candidate_id)}

    @router.get("/memory")
    async def list_memory():
        db, _ = require_lab()
        return {"rules": db.list_records("memory")}

    @router.post("/memory")
    async def create_memory(request: MemoryRuleInput):
        _, lab = require_lab()
        if not config.memory_learning_enabled:
            raise HTTPException(403, "Memory learning is disabled")
        return lab.memory.create_rule(_dump(request))

    @router.post("/memory/{rule_id}/observations")
    async def observe_memory(rule_id: str, request: MemoryObservationInput):
        _, lab = require_lab()
        if not config.memory_learning_enabled:
            raise HTTPException(403, "Memory learning is disabled")
        return lab.memory.observe(rule_id, _dump(request))

    @router.post("/memory/{rule_id}/review")
    async def review_memory(rule_id: str, request: MemoryReviewInput):
        _, lab = require_lab()
        return lab.memory.review(rule_id, request.action, request.note)

    @router.post("/memory/{rule_id}/promote-rule")
    async def promote_rule(rule_id: str):
        """Human-gated: inject a validated/high-confidence lab design rule into the production
        generation prompt. Only rules that cleared the confidence bar are eligible."""
        db, _ = require_lab()
        safe_id(rule_id, "rule id")
        if adapters.promote_rule is None:
            raise HTTPException(501, "promotion to production is not wired on this deployment")
        try:
            rule = db.get_record("memory", rule_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "memory rule not found") from exc
        status = derive_status(rule)
        if status not in {"validated", "high-confidence"}:
            raise HTTPException(409, f"rule status is '{status}'; only validated or high-confidence rules can be promoted")
        return {"promoted": True, "rule": adapters.promote_rule(rule)}

    @router.post("/memory/{rule_id}/revoke-rule")
    async def revoke_rule(rule_id: str):
        require_lab()
        safe_id(rule_id, "rule id")
        if adapters.revoke_rule is None:
            raise HTTPException(501, "promotion to production is not wired on this deployment")
        return {"revoked": adapters.revoke_rule(rule_id)}

    @router.post("/physical-validations")
    async def physical_validation(request: PhysicalValidationInput):
        db, lab = require_lab()
        if not config.physical_feedback_enabled:
            raise HTTPException(403, "Physical feedback is disabled")
        payload = _dump(request)
        try:
            safe_id(payload["run_id"], "run id")
            safe_id(payload["candidate_id"], "candidate id")
            run = db.get_run(payload["run_id"])
            candidate_record = db.get_candidate(payload["run_id"], payload["candidate_id"])
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, "physical validation must reference an existing run and candidate") from exc
        if run.get("demo"):
            raise HTTPException(400, "demo candidates cannot receive training evidence")
        artifact = printable_artifact(
            candidate_record, payload["artifact_checksum"], payload.get("artifact_name")
        )
        if artifact is None:
            raise HTTPException(409, "artifact checksum must match a printable artifact on the referenced candidate")
        try:
            artifact_path = db.candidate_artifact(
                payload["run_id"], payload["candidate_id"], artifact["name"]
            )
            artifact_bytes = artifact_path.read_bytes()
        except (OSError, ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, "referenced printable artifact is no longer safely stored") from exc
        if (
            not artifact_bytes
            or len(artifact_bytes) != int(artifact.get("size", -1))
            or hashlib.sha256(artifact_bytes).hexdigest() != str(artifact.get("sha256") or "").removeprefix("sha256:")
        ):
            raise HTTPException(409, "stored printable artifact no longer matches its immutable checksum")
        failure_classes = list(payload.get("failure_classes") or [])
        if len(failure_classes) != len(set(failure_classes)):
            raise HTTPException(400, "physical failure classes must be unique")
        if payload.get("printed_successfully") and failure_classes:
            raise HTTPException(400, "successful prints cannot include failure classes")
        if not payload.get("printed_successfully") and not failure_classes:
            raise HTTPException(400, "failed prints require at least one fixed failure class")
        if "other" in failure_classes and not str(payload.get("failure_notes") or "").strip():
            raise HTTPException(400, "the 'other' physical failure class requires failure notes")
        physical_id = db.physical_validation_id(
            payload["run_id"], payload["candidate_id"], artifact["sha256"]
        )
        normalized_submission = {
            **payload,
            "artifact_checksum": f"sha256:{artifact['sha256']}",
            "artifact_name": artifact["name"],
        }
        submission_checksum = f"sha256:{canonical_sha256(normalized_submission)}"
        record = {
            "id": physical_id,
            **payload,
            "artifact_checksum": f"sha256:{artifact['sha256']}",
            "artifact_name": artifact["name"],
            "artifact_role": artifact_role(candidate_record, artifact["name"]),
            "generation": int(candidate_record.get("generation", 0)),
            "submission_checksum": submission_checksum,
            "evidence_label": "PENDING PHYSICAL JOIN",
            "join_status": "pending",
            "verified_join": False,
            "candidate_joined": False,
            "mutation_outcome_joined": False,
            "memory_joined": False,
            "slicer_profile_fingerprint": (
                candidate_record.get("slicer_profile_fingerprint")
                or profile_fingerprint(
                    payload.get("printer_profile") or {},
                    payload.get("material"),
                    payload.get("nozzle"),
                    payload.get("layer_height"),
                    payload.get("slicer_profile"),
                )
            ),
            "production_rule_activated": False,
        }
        try:
            persisted = db.get_record("physical", physical_id)
        except FileNotFoundError:
            persisted = db.create_record("physical", record, prefix="physical")
        if persisted.get("submission_checksum") != submission_checksum:
            raise HTTPException(409, "this candidate artifact already has a different physical outcome")
        if persisted.get("verified_join") is True:
            return persisted
        record = persisted

        mutation_required = bool(candidate_record.get("mutation")) and int(candidate_record.get("generation", 0)) > 0
        mutation = None
        if mutation_required:
            try:
                mutation = db.mutation_outcome_for_candidate(
                    record["run_id"], record["candidate_id"], record["generation"]
                )
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(409, "candidate mutation outcome is not yet finalized") from exc
        rule_ids = [
            rule_id for rule_id in candidate_record.get("memory_rules_applied") or []
            if isinstance(rule_id, str)
        ]
        try:
            for rule_id in rule_ids:
                db.get_record("memory", rule_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, "candidate references unavailable memory evidence") from exc

        try:
            db.attach_physical_to_candidate(
                record["run_id"], record["candidate_id"], record, verified=False
            )
            if mutation is not None:
                db.attach_physical_to_mutation(record, verified=False)
            for rule_id in rule_ids:
                lab.memory.observe(rule_id, {
                    "success": bool(record["printed_successfully"]),
                    "physical": True,
                    "major_regression": not bool(record["printed_successfully"]),
                    "source_model_id": run.get("source_model_id"),
                    "source_candidate_id": record["candidate_id"],
                    "physical_validation_id": record["id"],
                    "note": "checksum-verified physical print outcome",
                })
            db.attach_physical_to_candidate(
                record["run_id"], record["candidate_id"], record, verified=True
            )
            if mutation is not None:
                mutation = db.attach_physical_to_mutation(record, verified=True)
            return db.update_record("physical", record["id"], lambda row: row.update({
                "evidence_label": "PHYSICALLY VERIFIED",
                "join_status": "verified",
                "verified_join": True,
                "candidate_joined": True,
                "mutation_outcome_id": mutation.get("id") if mutation else None,
                "mutation_outcome_joined": mutation is not None if mutation_required else True,
                "memory_joined": True,
                "memory_rule_ids_observed": rule_ids,
                "join_error": None,
            }))
        except (OSError, ValueError, FileNotFoundError) as exc:
            db.update_record("physical", record["id"], lambda row: row.update({
                "join_status": "pending",
                "join_error": str(exc)[:500],
                "verified_join": False,
            }))
            raise HTTPException(500, "physical evidence was saved pending backlink completion") from exc

    @router.get("/physical-validations")
    async def physical_validations():
        db, _ = require_lab()
        return {"records": db.list_records("physical")}

    @router.post("/calibrations")
    async def calibration(request: CalibrationInput):
        db, _ = require_lab()
        if not config.physical_feedback_enabled:
            raise HTTPException(403, "Physical feedback is disabled")
        return db.create_record("calibrations", {"id": new_id("calibration"), **_dump(request), "production_rule_activated": False}, prefix="calibration")

    @router.get("/calibrations")
    async def calibrations():
        db, _ = require_lab()
        return {"records": db.list_records("calibrations")}

    @router.get("/benchmarks")
    async def benchmarks():
        db, _ = require_lab()
        return {"catalog": benchmark_catalog(), "results": db.list_records("benchmarks")}

    @router.post("/benchmark-results")
    async def benchmark_result(request: BenchmarkInput):
        db, _ = require_lab()
        payload = _dump(request)
        payload.update({"id": new_id("benchmark"), "promotion_blocked": bool(payload.get("critical_regressions"))})
        return db.create_record("benchmarks", payload, prefix="benchmark")

    @router.post("/promotion-proposals")
    async def proposal(request: ProposalInput):
        db, _ = require_lab()
        payload = {"id": new_id("proposal"), **_dump(request), "status": "draft", "automatic_merge": False, "production_activated": False}
        return db.create_record("proposals", payload, prefix="proposal")

    @router.get("/promotion-proposals")
    async def proposals():
        db, _ = require_lab()
        return {"proposals": db.list_records("proposals")}

    @router.post("/promotion-proposals/{proposal_id}/status")
    async def proposal_status(proposal_id: str, request: ProposalStatusInput):
        db, _ = require_lab()
        return db.update_record("proposals", proposal_id, lambda row: row.update({"status": request.status, "review_note": request.note, "automatic_merge": False}))

    @router.post("/datasets")
    async def dataset(request: DatasetExportInput):
        db, _ = require_lab()
        try:
            return create_export(
                db,
                request.dataset_type,
                request.format,
                request.run_id,
                request.schema_version,
            )
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/datasets/{export_id}/download")
    async def dataset_download(export_id: str):
        db, _ = require_lab()
        try:
            record = db.get_record("datasets", export_id)
            path = db.dataset_file(export_id, record["filename"])
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        return FileResponse(path, media_type=record.get("media_type"), filename=record["filename"])

    @router.post("/actual-training")
    async def actual_training():
        require_lab()
        return {
            "supported": False, "enabled": config.actual_training_enabled and config.training_enabled,
            "executed": False, "evaluated": False, "deployed": False,
            "reason": "No configured PrintForge backend exposes a supported weight-training job",
        }

    return router
