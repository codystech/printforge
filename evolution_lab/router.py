"""Feature-gated HTTP API for the isolated Training Lab."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from .benchmarks import benchmark_catalog
from .config import EvolutionLabConfig
from .datasets import create_export
from .demo import DEMO_RUN_ID, load_demo_fixture
from .engine import EvolutionAdapters, EvolutionEngine
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

    @router.get("/bootstrap")
    async def bootstrap():
        flags = config.public_dict()
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
                "slicer": False, "actual_model_training": False,
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
        _, record = find_candidate(candidate_id)
        return record

    @router.get("/candidates/{candidate_id}/artifacts")
    async def candidate_artifacts(candidate_id: str):
        run_id, record = find_candidate(candidate_id)
        files = []
        for item in record.get("artifacts", []):
            name = item.get("name")
            if not name:
                continue
            files.append({**item, "url": f"/training-lab/api/candidates/{candidate_id}/artifacts/{name}", "role": "printable"})
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
        run_id, _ = find_candidate(candidate_id)
        _, lab = require_lab()
        try:
            return lab.restore_candidate(run_id, candidate_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.post("/candidates/{candidate_id}/branch")
    async def branch_candidate(candidate_id: str):
        run_id, _ = find_candidate(candidate_id)
        _, lab = require_lab()
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

    @router.post("/physical-validations")
    async def physical_validation(request: PhysicalValidationInput):
        db, _ = require_lab()
        if not config.physical_feedback_enabled:
            raise HTTPException(403, "Physical feedback is disabled")
        record = {"id": new_id("physical"), **_dump(request), "evidence_label": "PHYSICALLY VERIFIED", "production_rule_activated": False}
        return db.create_record("physical", record, prefix="physical")

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
            return create_export(db, request.dataset_type, request.format, request.run_id)
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
