"""Durable, regression-safe A/B evolution orchestration.

The engine owns persisted run state; HTTP connections only observe it.  Production
integration is deliberately narrow and candidates are never saved to PrintForge's
``library/``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .adaptive import adaptive_history_scope, outcome_record, select_mutations
from .config import EvolutionLabConfig
from .dataset_v2 import (
    has_verified_physical_failure,
    profile_fingerprint,
    slice_evidence_ready,
)
from .memory import MemoryService
from .schemas import EvidenceLabel, ScoreCategory
from .scoring import score_candidate, select_winner
from .store import EvolutionStore, new_id, utc_ts


@dataclass
class EvolutionAdapters:
    load_source_model: Callable[[str], dict[str, Any]] | None = None
    generate_initial_candidate: Callable[[dict[str, Any]], dict[str, Any] | str] | None = None
    generate_candidate: Callable[[str, dict[str, Any]], dict[str, Any] | str] | None = None
    evaluate_candidate: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
    evaluate_cadquery_candidate: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None
    current_branch: Callable[[], str] | None = None
    # Human-gated promotion into production (injected by app.py; the lab store never writes there).
    promote_exemplar: Callable[..., str] | None = None
    promote_exemplar_with_context: Callable[..., str] | None = None
    revoke_exemplar: Callable[[str], int] | None = None
    promote_rule: Callable[[dict], dict] | None = None
    revoke_rule: Callable[[str], int] | None = None
    slicer_status: Callable[[], dict[str, Any]] | None = None
    bambu_slicer: Any | None = None


def _dump(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    raise TypeError("request must be a mapping or pydantic model")


def _unverified(category: ScoreCategory, criterion: str, possible: float, critical: bool) -> dict:
    return {
        "category": category.value,
        "criterion": criterion,
        "points_awarded": 0,
        "points_possible": possible,
        "label": EvidenceLabel.UNVERIFIED.value,
        "source": "evolution-engine",
        "summary": "No qualifying evidence was produced",
        "confidence": 0,
        "critical": critical,
    }


class EvolutionEngine:
    def __init__(self, store: EvolutionStore, config: EvolutionLabConfig, adapters: EvolutionAdapters | None = None):
        self.store = store
        self.config = config
        self.adapters = adapters or EvolutionAdapters()
        self.memory = MemoryService(store)
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._mark_interrupted_runs()

    def _mark_interrupted_runs(self) -> None:
        for run in self.store.list_runs(include_demo=False):
            if run.get("status") in {"running", "stopping"}:
                rid = run["run_id"]
                self.store.update_run(rid, lambda row: row.update({
                    "status": "interrupted", "active_stage": None,
                    "interruption_reason": "application restarted; explicit resume required",
                }))
                self.store.append_event(rid, "warning", "run_interrupted", "Run interrupted by application restart")

    async def _invoke(self, callback: Callable | None, *args) -> Any:
        if callback is None:
            raise RuntimeError("required evolution adapter is unavailable")
        if inspect.iscoroutinefunction(callback):
            return await callback(*args)
        result = await asyncio.to_thread(callback, *args)
        if inspect.isawaitable(result):
            return await result
        return result

    @staticmethod
    def _iterations(limits: dict) -> int:
        value = limits.get("maximum_iterations")
        if value is None:
            value = limits.get("maximum_generations")
        return int(value if value is not None else 5)

    @staticmethod
    def _run_mode(payload: dict) -> str:
        value = payload.get("run_mode") or "evolve_existing"
        return value.value if hasattr(value, "value") else str(value)

    def _set_stage(self, run_id: str, stage: str | None, *, candidate_id: str | None = None) -> None:
        self.store.update_run(run_id, lambda row: row.update({
            "active_stage": stage,
            "active_candidate_id": candidate_id,
        }))

    @staticmethod
    def _candidate_data_fields(run: dict) -> dict:
        """Additive dataset-v2 provenance/evidence placeholders for every candidate."""

        provenance = dict(run.get("data_provenance") or {})
        provenance.setdefault("status", run.get("provenance_status", "unknown"))
        return {
            "source_prompt": run.get("source_prompt", ""),
            "validated_spec": run.get("validated_spec", ""),
            "part_family_split_key": run.get("part_family_split_key", ""),
            "training_consent": bool(run.get("training_consent", False)),
            "training_consent_decision": run.get("training_consent_decision", "not_reviewed"),
            "training_consent_reviewer": run.get("training_consent_reviewer", ""),
            "training_consent_reviewed_at": run.get("training_consent_reviewed_at"),
            "data_provenance": provenance,
            "deterministic_evidence": [],
            "slicer_results": {"status": "unavailable"},
            "physical_outcomes": [],
            "evaluator_version": None,
            "evaluator_fingerprint": None,
            "slicer_profile_fingerprint": None,
            "evidence_coverage": {
                "deterministic": {"present": False, "passed": None},
                "slicer": {"present": False, "passed": None},
                "physical": {"present": False, "passed": None},
            },
            "missing_evidence_mask": {"deterministic": True, "slicer": True, "physical": True},
        }

    @staticmethod
    def _evaluation_data(run: dict, evaluation: dict, evidence: list[dict], passed: bool) -> dict:
        slicer = evaluation.get("slicer_results") or {"status": "unavailable"}
        slicer_ok = str(slicer.get("status") or "").casefold() == "complete"
        evaluator_version = evaluation.get("evaluator_version") or "evolution-adapter-v1"
        evaluator_fingerprint = evaluation.get("evaluator_fingerprint") or profile_fingerprint(
            evaluator_version,
            evaluation.get("required_check_versions") or {},
        )
        slicer_fingerprint = (
            evaluation.get("slicer_profile_fingerprint")
            or slicer.get("profile_fingerprint")
        )
        slicer_present = str(slicer.get("status") or "").casefold() not in {
            "", "unavailable", "not_submitted", "missing", "skipped"
        }
        model_format = evaluation.get("model_format") or run.get("model_format") or "openscad-legacy"
        delivery_blocked = bool(
            model_format == "cadquery-v1"
            and (
                evaluation.get("promotion_blocked")
                or evaluation.get("bambuddy_send_blocked")
                or not slicer_ok
            )
        )
        return {
            "deterministic_evidence": evidence,
            "slicer_results": slicer,
            "evaluator_version": evaluator_version,
            "evaluator_fingerprint": evaluator_fingerprint,
            "slicer_profile_fingerprint": slicer_fingerprint,
            "promotion_blocked": delivery_blocked,
            "bambuddy_send_blocked": delivery_blocked,
            "evidence_coverage": {
                "deterministic": {"present": bool(evidence), "passed": bool(passed) if evidence else None},
                "slicer": {
                    "present": slicer_present,
                    "passed": slicer_ok if slicer_present else None,
                },
                "physical": {"present": False, "passed": None},
            },
            "missing_evidence_mask": {
                "deterministic": not bool(evidence),
                "slicer": not slicer_present,
                "physical": True,
            },
        }

    @staticmethod
    def _fallback_evidence(meta: dict) -> list[dict]:
        report = meta.get("report") or {}
        measured = bool(report)
        print_points = 0.0
        if report.get("watertight"):
            print_points += 8
        if report.get("bed_fit") == "ok":
            print_points += 5
        if report.get("bbox_mm"):
            print_points += 3
        if report.get("parts") is not None:
            print_points += 2
        return [{
            "category": ScoreCategory.PRINTABILITY.value,
            "criterion": "stored baseline geometry report",
            "points_awarded": print_points if measured else 0,
            "points_possible": 18,
            "label": EvidenceLabel.MEASURED.value if measured else EvidenceLabel.UNVERIFIED.value,
            "source": "source model metadata",
            "summary": "Existing PrintForge report; no slicer or physical evidence",
            "confidence": 0.8 if measured else 0,
            "critical": not measured,
        }, _unverified(ScoreCategory.FUNCTION, "baseline functional behavior", 25, True),
            _unverified(ScoreCategory.ADHERENCE, "baseline spec adherence", 20, True)]

    def _run_record(self, payload: dict) -> dict:
        mode = self._run_mode(payload)
        if mode not in {"evolve_existing", "create_from_spec"}:
            raise ValueError("run_mode must be evolve_existing or create_from_spec")
        source_model_id = payload.get("source_model_id")
        if mode == "evolve_existing" and not source_model_id:
            raise ValueError("Starting model is required when evolving an existing model")
        if mode == "create_from_spec":
            source_model_id = None
        if not str(payload.get("validated_spec") or "").strip():
            raise ValueError("Design specification is required")
        if not payload.get("printer_profile"):
            raise ValueError("Printer profile is required")
        run_id = new_id("run")
        now = utc_ts()
        limits = payload.get("limits") or {}
        record = {
            "run_id": run_id, "demo": False, "status": "created",
            "run_mode": mode, "source_model_id": source_model_id,
            "model_format": "openscad-legacy",
            "source_prompt": payload.get("source_prompt", ""),
            "validated_spec": payload["validated_spec"],
            "part_family": str(payload.get("part_family") or "").strip(),
            "part_family_split_key": str(payload.get("part_family") or "").strip().casefold(),
            "training_consent": bool(payload.get("training_consent", False)),
            "training_consent_decision": payload.get("training_consent_decision", "not_reviewed"),
            "training_consent_reviewer": payload.get("training_consent_reviewer", ""),
            "training_consent_reviewed_at": payload.get("training_consent_reviewed_at"),
            "provenance_status": payload.get("provenance_status", "unknown"),
            "data_provenance": payload.get("data_provenance") or {},
            "printer_profile": payload.get("printer_profile", {}),
            "material_profile": payload.get("material_profile", {}),
            "locked_constraints": payload.get("locked_constraints", []),
            "attached_reference_roles": payload.get("attached_reference_roles", []),
            "export_exclusions": payload.get("export_exclusions", []),
            "active_backend": payload.get("active_backend", ""),
            "limits": limits, "initial_mutations": payload.get("initial_mutations", []),
            "current_generation": 0 if mode == "evolve_existing" else -1,
            "lineage_edges": [], "generation_results": [],
            "stop_after_current_generation": False, "estimated_cost": 0.0,
            "backend_calls": 0, "consecutive_generation_failures": 0,
            "latest_failure": None, "stop_reason": None,
            "started_at": None, "created_at": now, "updated_at": now,
            "actual_training_performed": False,
        }
        record["adaptive_history_scope"] = adaptive_history_scope(record)
        return record

    async def _attach_baseline(self, run: dict, scad: str, meta: dict) -> dict:
        run_id = run["run_id"]
        if not isinstance(scad, str) or not scad.strip():
            raise ValueError("source model has no OpenSCAD source")
        if not run.get("source_prompt"):
            run["source_prompt"] = meta.get("prompt", "")
        self.store.create_run(run, {"model.scad": scad, "meta.json": __import__("json").dumps(meta)})
        context = {"run": run, "candidate": {"candidate_id": "baseline"}, "baseline": True, "source_meta": meta}
        try:
            evaluation = await self._invoke(self.adapters.evaluate_candidate, scad, context)
        except Exception:
            evaluation = {"evidence": self._fallback_evidence(meta), "failure_codes": []}
        evidence = evaluation.get("evidence") or self._fallback_evidence(meta)
        score = score_candidate(evidence, evaluation.get("failure_codes", []))
        baseline_id = new_id("candidate")
        baseline = {
            **self._candidate_data_fields(run),
            "candidate_id": baseline_id, "run_id": run_id, "generation": 0,
            "version": 0,
            "variant_label": "BASELINE", "parent_candidate_id": None,
            "current_best_parent_id": None, "mutation": None, "status": "complete",
            "selection_status": "baseline", "score_evidence": evidence, "score": score,
            "qa_results": evaluation.get("qa_results", []),
            "issues": evaluation.get("issues", []), "artifacts": [],
            "model_format": "openscad-legacy", "parameters": {}, "parts": [],
            "generation_prompt": run.get("source_prompt", ""),
            "required_checks_passed": not score["hard_rejected"] and not evaluation.get("failure_codes", []),
            **self._evaluation_data(
                run,
                evaluation,
                evidence,
                not score["hard_rejected"] and not evaluation.get("failure_codes", []),
            ),
            "failure_reason": None, "created_at": utc_ts(), "updated_at": utc_ts(),
        }
        self.store.create_candidate(run_id, baseline)
        self.store.add_candidate_artifacts(run_id, baseline_id, {"model.scad": scad, **(evaluation.get("artifacts") or {})})
        checkpoint = self.store.create_checkpoint(run_id, baseline_id, "baseline")
        self.store.update_run(run_id, lambda row: row.update({
            "baseline_candidate_id": baseline_id, "current_best_candidate_id": baseline_id,
            "highest_scoring_candidate_id": baseline_id, "current_best_score": score["total"],
            "baseline_score": score["total"], "baseline_checkpoint_id": checkpoint["checkpoint_id"],
        }))
        self.store.append_event(run_id, "info", "baseline_loaded", "Baseline loaded and checkpointed", candidate_id=baseline_id, generation=0)
        return self.snapshot(run_id)

    async def create_run(self, request: Any) -> dict:
        payload = _dump(request)
        run = self._run_record(payload)
        if run["run_mode"] == "evolve_existing":
            source = await self._invoke(self.adapters.load_source_model, run["source_model_id"])
            snapshot = await self._attach_baseline(run, source.get("scad"), source.get("meta") or {})
        else:
            # Generation zero is intentionally deferred until Start so creating a
            # run is cheap, cancellable, and never holds an HTTP request open.
            self.store.create_run(run, {
                "request.json": json.dumps({k: v for k, v in payload.items() if k != "auto_start"}, default=str),
            })
            candidate_id = new_id("candidate")
            now = utc_ts()
            self.store.create_candidate(run["run_id"], {
                **self._candidate_data_fields(run),
                "candidate_id": candidate_id, "run_id": run["run_id"],
                "generation": 0, "version": 0, "variant_label": "GENERATION_ZERO",
                "parent_candidate_id": None, "current_best_parent_id": None,
                "mutation": {"mutation_type": "initial_design", "reason": "create from validated specification"},
                "generation_prompt": run["validated_spec"], "prompt_used": run["validated_spec"],
                "spec_used": run["validated_spec"], "locks_applied": run.get("locked_constraints", []),
                "printer_profile_snapshot": run.get("printer_profile", {}),
                "material_profile_snapshot": run.get("material_profile", {}),
                "backend": run.get("active_backend") or "unknown", "status": "pending",
                "model_format": run.get("model_format", "openscad-legacy"),
                "parameters": {}, "parts": [],
                "selection_status": "generation_zero", "artifacts": [],
                "created_at": now, "updated_at": now,
            })
            self.store.update_run(run["run_id"], lambda row: row.update({"generation_zero_candidate_id": candidate_id}))
            self.store.append_event(run["run_id"], "info", "generation_zero_queued", "Generation zero is ready to generate", candidate_id=candidate_id, generation=0)
            snapshot = self.snapshot(run["run_id"])
        if payload.get("auto_start"):
            self.start(run["run_id"])
        return snapshot

    def start(self, run_id: str) -> dict:
        if not self.config.evolution_enabled:
            raise PermissionError("evolution is disabled")
        if self.adapters.generate_candidate is None or self.adapters.evaluate_candidate is None:
            raise RuntimeError("generation/evaluation adapters are unavailable")
        task = self._tasks.get(run_id)
        if task and not task.done():
            return self.snapshot(run_id)
        run = self.store.get_run(run_id)
        if run.get("demo"):
            raise ValueError("demo runs cannot execute")
        if run.get("status") in {"complete", "cancelled"}:
            raise ValueError("completed run cannot be restarted")
        if run.get("run_mode") == "create_from_spec" and self.adapters.generate_initial_candidate is None:
            raise RuntimeError("initial generation adapter is unavailable")
        self._cancel_events[run_id] = threading.Event()
        self._tasks[run_id] = asyncio.create_task(self._run(run_id))
        return self.snapshot(run_id)

    def stop_after_generation(self, run_id: str) -> dict:
        self.store.update_run(run_id, lambda row: row.update({"stop_after_current_generation": True, "status": "stopping" if row.get("status") == "running" else row.get("status")}))
        self.store.append_event(run_id, "warning", "stop_requested", "Run will stop after the current generation")
        return self.snapshot(run_id)

    def cancel(self, run_id: str, *, reason: str = "user_cancelled") -> dict:
        run = self.store.get_run(run_id)
        if run.get("status") in {"complete", "failed", "cancelled"}:
            return self.snapshot(run_id)
        event = self._cancel_events.setdefault(run_id, threading.Event())
        event.set()
        self.store.update_run(run_id, lambda row: row.update({
            "status": "cancelling", "cancellation_reason": reason,
            "stop_reason": reason,
        }))
        self.store.append_event(run_id, "warning", "cancel_requested", "Immediate cancellation requested")
        task = self._tasks.get(run_id)
        if task and not task.done():
            task.cancel()
        else:
            self.store.update_run(run_id, lambda row: row.update({
                "status": "cancelled", "active_stage": None,
                "active_candidate_id": None, "completed_at": utc_ts(),
            }))
        return self.snapshot(run_id)

    async def _runtime_watchdog(self, run_id: str, seconds: float) -> None:
        try:
            await asyncio.sleep(max(0.0, seconds))
            if self.task_active(run_id):
                self.store.append_event(run_id, "warning", "runtime_limit", "Maximum runtime reached; cancelling active work")
                self.cancel(run_id, reason="maximum_runtime")
        except asyncio.CancelledError:
            cancellation_reason = self.store.get_run(run_id).get("cancellation_reason") or "user_cancelled"
            return

    async def _run(self, run_id: str) -> None:
        started = utc_ts()
        self.store.update_run(run_id, lambda row: row.update({"status": "running", "started_at": row.get("started_at") or started}))
        self.store.append_event(run_id, "info", "run_started", "Evolution run started")
        initial = self.store.get_run(run_id)
        maximum_runtime = float((initial.get("limits") or {}).get("maximum_runtime_seconds", 1200))
        watchdog = asyncio.create_task(self._runtime_watchdog(run_id, maximum_runtime))
        try:
            if initial.get("run_mode") == "create_from_spec" and not initial.get("current_best_candidate_id"):
                await self._generation_zero(run_id)
            while True:
                run = self.store.get_run(run_id)
                limits = run.get("limits", {})
                target = limits.get("target_reward_score")
                checks_pass = bool(run.get("current_best_required_checks_passed"))
                target_pass = target is None or float(run.get("current_best_score", 0)) >= float(target)
                if checks_pass and target_pass and (run.get("run_mode") == "create_from_spec" or int(run.get("current_generation", 0)) > 0):
                    reason = "target_reached" if target is not None else "required_checks_passed"
                    self.store.update_run(run_id, lambda row: row.update({"stop_reason": reason}))
                    self.store.append_event(run_id, "info", reason, "All required checks passed" + (" and target reward reached" if target is not None else ""))
                    break
                generation = int(run.get("current_generation", 0)) + 1
                if generation > self._iterations(limits):
                    self.store.update_run(run_id, lambda row: row.update({"stop_reason": "iteration_limit"}))
                    self.store.append_event(run_id, "info", "iteration_limit", "Maximum iteration count reached")
                    break
                if float(run.get("estimated_cost", 0)) >= float(limits.get("maximum_estimated_cost", 10)):
                    self.store.update_run(run_id, lambda row: row.update({"stop_reason": "cost_limit"}))
                    self.store.append_event(run_id, "warning", "cost_limit", "Estimated cost limit reached")
                    break
                if int(run.get("backend_calls", 0)) >= int(limits.get("maximum_backend_calls", 10)):
                    self.store.update_run(run_id, lambda row: row.update({"stop_reason": "backend_call_limit"}))
                    self.store.append_event(run_id, "warning", "backend_call_limit", "Backend-call limit reached")
                    break
                await self._generation(run_id, generation)
                run = self.store.get_run(run_id)
                if run.get("stop_after_current_generation"):
                    self.store.update_run(run_id, lambda row: row.update({"stop_reason": "user_stop_after_generation"}))
                    break
                failure_limit = int(limits.get("repeated_generation_failure_limit", 3))
                if int(run.get("consecutive_generation_failures", 0)) >= failure_limit:
                    self.store.update_run(run_id, lambda row: row.update({"stop_reason": "repeated_generation_failures"}))
                    self.store.append_event(run_id, "warning", "repeated_failure_limit", "Repeated generation failure limit reached")
                    break
                recent = run.get("generation_results", [])[-int(limits.get("no_improvement_limit", 2)):]
                if len(recent) >= int(limits.get("no_improvement_limit", 2)) and all(item.get("current_best_preserved") for item in recent):
                    self.store.update_run(run_id, lambda row: row.update({"stop_reason": "no_improvement"}))
                    self.store.append_event(run_id, "warning", "no_improvement_limit", "No-improvement limit reached")
                    break
            self.store.update_run(run_id, lambda row: row.update({"status": "complete", "active_stage": None, "completed_at": utc_ts()}))
            self.store.append_event(run_id, "info", "run_completed", "Evolution run completed")
        except asyncio.CancelledError:
            run = self.store.get_run(run_id)
            reason = run.get("cancellation_reason") or "user_cancelled"
            status = "complete" if reason == "maximum_runtime" else "cancelled"
            self.store.update_run(run_id, lambda row: row.update({
                "status": status, "active_stage": None, "active_candidate_id": None,
                "stop_reason": reason, "completed_at": utc_ts(),
            }))
            self.store.append_event(run_id, "warning", "run_cancelled" if status == "cancelled" else "run_completed", f"Run stopped: {reason}")
        except Exception as exc:
            self.store.update_run(run_id, lambda row: row.update({"status": "failed", "active_stage": None, "failure": str(exc)[:1000]}))
            self.store.append_event(run_id, "error", "run_failed", f"Run failed: {str(exc)[:500]}")
        finally:
            watchdog.cancel()
            self._cancel_events.pop(run_id, None)

    async def _generation_zero(self, run_id: str) -> None:
        run = self.store.get_run(run_id)
        candidate_id = run["generation_zero_candidate_id"]
        self._set_stage(run_id, "generation_zero", candidate_id=candidate_id)
        self.store.update_candidate(run_id, candidate_id, lambda row: row.update({
            "status": "generating", "started_at": utc_ts(),
        }))
        self.store.append_event(run_id, "info", "generation_zero_started", "Generating the initial design from the specification", candidate_id=candidate_id, generation=0)
        context = {
            "run": run, "candidate_id": candidate_id, "generation": 0,
            "variant_label": "GENERATION_ZERO", "validated_spec": run["validated_spec"],
            "locked_constraints": run.get("locked_constraints", []),
            "printer_profile": run.get("printer_profile", {}),
            "material_profile": run.get("material_profile", {}),
            "attached_reference_roles": run.get("attached_reference_roles", []),
            "export_exclusions": run.get("export_exclusions", []),
            "parent_scad": "", "cancel_event": self._cancel_events.get(run_id),
            "mutation": {"mutation_type": "initial_design", "reason": "create from validated specification"},
        }
        t0 = time.monotonic()
        source = None
        source_name = "model.scad"
        try:
            generated = await self._invoke(self.adapters.generate_initial_candidate, context)
            generated = {"scad": generated} if isinstance(generated, str) else generated
            model_format = str(generated.get("model_format") or "openscad-legacy")
            if model_format not in {"openscad-legacy", "cadquery-v1"}:
                raise ValueError(f"unsupported generated model format: {model_format}")
            source_name = "model.py" if model_format == "cadquery-v1" else "model.scad"
            source = generated.get("source") if model_format == "cadquery-v1" else generated.get("scad")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(f"generated candidate is missing {source_name}")
            context.update({"model_format": model_format, "source_name": source_name})
            self._set_stage(run_id, "generation_zero_evaluation", candidate_id=candidate_id)
            evaluator = (
                self.adapters.evaluate_cadquery_candidate
                if model_format == "cadquery-v1" else self.adapters.evaluate_candidate
            )
            evaluation = await self._invoke(evaluator, source, context)
            evaluation.setdefault("model_format", model_format)
            failure_codes = list(evaluation.get("failure_codes", []))
            evidence = evaluation.get("evidence") or [_unverified(ScoreCategory.FUNCTION, "initial functional behavior", 25, True)]
            score = score_candidate(evidence, failure_codes)
            artifacts = {source_name: source, **(evaluation.get("artifacts") or {}), **(generated.get("artifacts") or {})}
            self.store.add_candidate_artifacts(run_id, candidate_id, artifacts)
            duration = time.monotonic() - t0
            cost = float(generated.get("estimated_cost", 0) or 0) + float(evaluation.get("estimated_cost", 0) or 0)
            candidate = self.store.update_candidate(run_id, candidate_id, lambda row: row.update({
                "status": "rejected" if score["hard_rejected"] else "complete",
                "selection_status": "rejected" if score["hard_rejected"] else "baseline",
                "score_evidence": evidence, "score": score,
                "qa_results": evaluation.get("qa_results", []),
                "issues": evaluation.get("issues", []), "failure_reasons": failure_codes,
                "failure_reason": ", ".join(failure_codes) if failure_codes else None,
                "rejection_reasons": score["hard_rejection_reasons"],
                "required_checks_passed": not score["hard_rejected"] and not failure_codes,
                "generation_duration_seconds": round(duration, 3),
                "estimated_generation_cost": cost, "backend": generated.get("backend", row["backend"]),
                "model_format": model_format,
                "parameters": evaluation.get("parameters") or {},
                "parts": evaluation.get("parts") or [],
                "artifact_id": evaluation.get("artifact_id"),
                **self._evaluation_data(
                    run, evaluation, evidence, not score["hard_rejected"] and not failure_codes
                ),
                "completed_at": utc_ts(),
            }))
            self.store.update_run(run_id, lambda row: row.update({
                "current_generation": 0, "estimated_cost": cost,
                "model_format": model_format,
                "backend_calls": int(generated.get("backend_calls", 1)) + int(evaluation.get("backend_calls", 0)),
            }))
            if score["hard_rejected"]:
                reason = candidate.get("failure_reason") or "generation zero failed required checks"
                self.store.update_run(run_id, lambda row: row.update({"latest_failure": reason, "stop_reason": "generation_zero_failed"}))
                self.store.append_event(run_id, "error", "generation_zero_failed", reason, candidate_id=candidate_id, generation=0)
                raise RuntimeError(reason)
            checkpoint = self.store.create_checkpoint(run_id, candidate_id, "baseline")
            self.store.update_run(run_id, lambda row: row.update({
                "baseline_candidate_id": candidate_id, "current_best_candidate_id": candidate_id,
                "highest_scoring_candidate_id": candidate_id, "current_best_score": score["total"],
                "baseline_score": score["total"], "baseline_checkpoint_id": checkpoint["checkpoint_id"],
                "current_best_required_checks_passed": True,
                "latest_failure": None, "active_stage": None, "active_candidate_id": None,
            }))
            self.store.append_event(run_id, "info", "generation_zero_completed", "Generation zero generated, evaluated, and checkpointed", candidate_id=candidate_id, generation=0)
        except asyncio.CancelledError:
            cancellation_reason = self.store.get_run(run_id).get("cancellation_reason") or "user_cancelled"
            if source:
                try:
                    self.store.add_candidate_artifacts(run_id, candidate_id, {source_name: source})
                except (FileExistsError, ValueError):
                    pass
            cancelled = self.store.update_candidate(run_id, candidate_id, lambda row: row.update({
                "status": "cancelled", "selection_status": "cancelled",
                "failure_reason": f"generation zero cancelled: {cancellation_reason}",
                "failure_reasons": [cancellation_reason], "required_checks_passed": False,
                "completed_at": utc_ts(),
            }))
            self._record_mutation_outcome(run, cancelled, 0, 0.0)
            raise
        except Exception as exc:
            if source:
                try:
                    self.store.add_candidate_artifacts(run_id, candidate_id, {source_name: source})
                except (FileExistsError, ValueError):
                    pass
            candidate = self.store.get_candidate(run_id, candidate_id)
            if candidate.get("status") not in {"rejected", "failed"}:
                evidence = [_unverified(ScoreCategory.PRINTABILITY, "generation zero", 25, True)]
                score = score_candidate(evidence, ["generation_failed"])
                self.store.update_candidate(run_id, candidate_id, lambda row: row.update({
                    "status": "failed", "selection_status": "rejected",
                    "score_evidence": evidence, "score": score,
                    "failure_reason": str(exc)[:1000], "failure_reasons": [str(exc)[:1000]],
                    "rejection_reasons": score["hard_rejection_reasons"], "completed_at": utc_ts(),
                }))
            self.store.update_run(run_id, lambda row: row.update({
                "current_generation": 0, "latest_failure": str(exc)[:1000],
                "stop_reason": "generation_zero_failed",
            }))
            self.store.append_event(run_id, "error", "generation_zero_failed", f"Generation zero failed: {str(exc)[:500]}", candidate_id=candidate_id, generation=0)
            raise

    def _select_mutations(self, run: dict, generation: int) -> list[dict]:
        """Select two mutations while preserving explicit first-generation inputs."""

        limits = run.get("limits") or {}
        seed = limits.get("random_seed")
        rng = random.Random(int(seed) + generation) if seed is not None else None
        history_scope = run.get("adaptive_history_scope") or adaptive_history_scope(run)
        selected = select_mutations(
            self.store.list_mutation_outcomes(history_scope, limit=200),
            n=2,
            exploration_rate=limits.get("exploration_rate", 0.15),
            rng=rng,
        )
        if generation != 1:
            return selected

        explicit = [dict(item) for item in run.get("initial_mutations", []) if isinstance(item, dict)]
        if not explicit:
            return selected
        mutation_types = {item.get("mutation_type") for item in explicit}
        explicit.extend(item for item in selected if item.get("mutation_type") not in mutation_types)
        return explicit[:2]

    def _mutations(self, run: dict, generation: int) -> list[dict]:
        """Use the persisted proposal when present so the UI and executed run agree."""

        proposals = run.get("next_mutation_proposals")
        if run.get("next_mutation_generation") == generation and isinstance(proposals, list):
            valid = [dict(item) for item in proposals if isinstance(item, dict)]
            if len(valid) >= 2:
                return valid[:2]
        return self._select_mutations(run, generation)

    def _record_mutation_outcome(
        self,
        run: dict,
        candidate: dict,
        generation: int,
        parent_score: float,
    ) -> dict:
        """Persist one final candidate disposition without duplicating retries."""

        score = candidate.get("score") if isinstance(candidate.get("score"), dict) else {}
        hard_rejected = bool(score.get("hard_rejected"))
        candidate_status = str(candidate.get("status") or "")
        selection_status = str(candidate.get("selection_status") or "")
        eligible = (
            not hard_rejected
            and candidate_status not in {"failed", "rejected", "cancelled"}
            and selection_status in {"winner", "loser"}
            and candidate.get("required_checks_passed") is True
        )
        try:
            score_delta = float(score.get("total", 0) or 0) - float(parent_score)
        except (TypeError, ValueError):
            score_delta = -float(parent_score)
        if selection_status == "cancelled":
            score_delta = min(0.0, score_delta)
        return self.store.create_mutation_outcome(outcome_record(
            candidate.get("mutation") or {},
            success=eligible and selection_status == "winner",
            score_delta=score_delta,
            run_id=run["run_id"],
            candidate_id=candidate["candidate_id"],
            generation=generation,
            eligible=eligible,
            hard_rejected=hard_rejected,
            candidate_status=candidate_status,
            selection_status=selection_status,
            adaptive_scope=run.get("adaptive_history_scope") or adaptive_history_scope(run),
        ))

    async def _generation(self, run_id: str, generation: int) -> None:
        run = self.store.get_run(run_id)
        parent_id = run["current_best_candidate_id"]
        model_format = run.get("model_format") or "openscad-legacy"
        parent_source_name = "model.py" if model_format == "cadquery-v1" else "model.scad"
        parent_source = self.store.candidate_artifact(run_id, parent_id, parent_source_name).read_text()
        candidates = []
        for label, mutation in zip(("A", "B"), self._mutations(run, generation)):
            cid = new_id("candidate")
            now = utc_ts()
            context = {
                "run": run, "candidate_id": cid, "generation": generation, "variant_label": label,
                "mutation": mutation, "validated_spec": run["validated_spec"],
                "locked_constraints": run.get("locked_constraints", []),
                "printer_profile": run.get("printer_profile", {}), "material_profile": run.get("material_profile", {}),
                "attached_reference_roles": run.get("attached_reference_roles", []), "export_exclusions": run.get("export_exclusions", []),
                "parent_scad": parent_source if model_format == "openscad-legacy" else "",
                "parent_source": parent_source, "model_format": model_format,
                "cancel_event": self._cancel_events.get(run_id),
            }
            memory_match = {"applied": [], "recommended": [], "shown": [], "ignored": []}
            if self.config.memory_learning_enabled:
                profile = run.get("printer_profile", {})
                material = run.get("material_profile", {})
                memory_match = self.memory.query({
                    "printer_profile": profile.get("name"), "printer": profile.get("printer"),
                    "material": material.get("material") or profile.get("material"),
                    "nozzle": profile.get("nozzle"), "layer_height": profile.get("layer") or material.get("layer_height"),
                    "feature_type": mutation.get("mutation_type"),
                })
            context["memory_rules"] = memory_match
            candidate = {
                **self._candidate_data_fields(run),
                "candidate_id": cid, "run_id": run_id, "generation": generation, "variant_label": label,
                "version": generation,
                "parent_candidate_id": parent_id, "current_best_parent_id": parent_id, "mutation": mutation,
                "expected_benefit": mutation.get("expected_benefit", ""),
                "generation_prompt": mutation.get("reason") or mutation.get("expected_benefit") or "controlled mutation",
                "prompt_used": "controlled mutation via edit-in-place adapter",
                "spec_used": run["validated_spec"], "locks_applied": run.get("locked_constraints", []),
                "printer_profile_snapshot": run.get("printer_profile", {}), "material_profile_snapshot": run.get("material_profile", {}),
                "backend": run.get("active_backend") or "unknown", "status": "generating", "selection_status": "pending",
                "model_format": run.get("model_format", "openscad-legacy"),
                "parameters": {}, "parts": [],
                "memory_rules_applied": [rule.get("rule_id") for rule in memory_match["applied"]],
                "memory_rules_ignored": [{"rule_id": item["rule"].get("rule_id"), "reasons": item["reasons"]} for item in memory_match["ignored"]],
                "artifacts": [], "created_at": now, "updated_at": now,
            }
            self.store.create_candidate(run_id, candidate)
            self.store.update_run(run_id, lambda row: row.update({"active_stage": "generation", "active_candidate_id": cid, "current_generation": generation}))
            self.store.append_event(run_id, "info", "candidate_started", f"Variant {label} started", candidate_id=cid, generation=generation)
            t0 = time.monotonic()
            generated = None
            source = None
            source_name = parent_source_name
            try:
                generated = await self._invoke(self.adapters.generate_candidate, parent_source, context)
                generated = {"scad": generated} if isinstance(generated, str) else generated
                candidate_format = str(generated.get("model_format") or model_format)
                if candidate_format != model_format:
                    raise ValueError("candidate mutation cannot change the run model format")
                source_name = "model.py" if candidate_format == "cadquery-v1" else "model.scad"
                source = generated.get("source") if candidate_format == "cadquery-v1" else generated.get("scad")
                if not isinstance(source, str) or not source.strip():
                    raise ValueError(f"generated candidate is missing {source_name}")
                context.update({"model_format": candidate_format, "source_name": source_name})
                self._set_stage(run_id, "evaluation", candidate_id=cid)
                evaluator = (
                    self.adapters.evaluate_cadquery_candidate
                    if candidate_format == "cadquery-v1" else self.adapters.evaluate_candidate
                )
                evaluation = await self._invoke(evaluator, source, context)
                evaluation.setdefault("model_format", candidate_format)
                failure_codes = list(evaluation.get("failure_codes", []))
                evidence = evaluation.get("evidence") or [_unverified(ScoreCategory.FUNCTION, "functional behavior", 25, True)]
                score = score_candidate(evidence, failure_codes)
                artifacts = {source_name: source, **(evaluation.get("artifacts") or {}), **(generated.get("artifacts") or {})}
                self.store.add_candidate_artifacts(run_id, cid, artifacts)
                duration = time.monotonic() - t0
                cost = float(generated.get("estimated_cost", 0) or 0) + float(evaluation.get("estimated_cost", 0) or 0)
                def finish(row: dict) -> None:
                    row.update({
                        "status": "rejected" if score["hard_rejected"] else "evaluated", "score_evidence": evidence,
                        "score": score, "qa_results": evaluation.get("qa_results", []),
                        "issues": evaluation.get("issues", []), "failure_reasons": failure_codes,
                        "failure_reason": ", ".join(failure_codes) if failure_codes else None,
                        "rejection_reasons": score["hard_rejection_reasons"], "generation_duration_seconds": round(duration, 3),
                        "required_checks_passed": not score["hard_rejected"] and not failure_codes,
                        "estimated_generation_cost": cost, "backend": generated.get("backend", row["backend"]),
                        "model_format": candidate_format,
                        "parameters": evaluation.get("parameters") or {},
                        "parts": evaluation.get("parts") or [],
                        "artifact_id": evaluation.get("artifact_id"),
                        **self._evaluation_data(
                            run, evaluation, evidence, not score["hard_rejected"] and not failure_codes
                        ),
                        "memory_rules_applied": evaluation.get("memory_rules_applied", []), "memory_rules_ignored": evaluation.get("memory_rules_ignored", []),
                        "completed_at": utc_ts(),
                    })
                candidate = self.store.update_candidate(run_id, cid, finish)
                self.store.update_run(run_id, lambda row: row.update({"estimated_cost": float(row.get("estimated_cost", 0)) + cost, "backend_calls": int(row.get("backend_calls", 0)) + int(generated.get("backend_calls", 1)) + int(evaluation.get("backend_calls", 0))}))
            except asyncio.CancelledError:
                cancellation_reason = self.store.get_run(run_id).get("cancellation_reason") or "user_cancelled"
                if source:
                    try:
                        self.store.add_candidate_artifacts(run_id, cid, {source_name: source})
                    except (FileExistsError, ValueError):
                        pass
                cancelled = self.store.update_candidate(run_id, cid, lambda row: row.update({
                    "status": "cancelled", "selection_status": "cancelled",
                    "failure_reason": f"generation cancelled: {cancellation_reason}",
                    "failure_reasons": [cancellation_reason], "required_checks_passed": False,
                    "completed_at": utc_ts(),
                }))
                parent_score = float(run.get("current_best_score", 0) or 0)
                for completed in candidates:
                    final_selection = "rejected" if completed.get("score", {}).get("hard_rejected") else "loser"
                    completed = self.store.update_candidate(
                        run_id,
                        completed["candidate_id"],
                        lambda row, status=final_selection: row.update({"selection_status": status}),
                    )
                    self._record_mutation_outcome(run, completed, generation, parent_score)
                self._record_mutation_outcome(run, cancelled, generation, parent_score)
                self.store.append_event(run_id, "warning", "candidate_cancelled", f"Variant {label} cancelled", candidate_id=cid, generation=generation)
                raise
            except Exception as exc:
                if source:
                    try:
                        self.store.add_candidate_artifacts(run_id, cid, {source_name: source})
                    except (FileExistsError, ValueError):
                        pass
                evidence = [_unverified(ScoreCategory.PRINTABILITY, "candidate generation", 25, True)]
                score = score_candidate(evidence, ["generation_failed"])
                candidate = self.store.update_candidate(run_id, cid, lambda row: row.update({"status": "failed", "score_evidence": evidence, "score": score, "failure_reason": str(exc)[:1000], "failure_reasons": [str(exc)[:1000]], "rejection_reasons": score["hard_rejection_reasons"], "completed_at": utc_ts()}))
                self.store.update_run(run_id, lambda row: row.update({"latest_failure": str(exc)[:1000]}))
                self.store.append_event(run_id, "error", "candidate_failed", f"Variant {label} failed", candidate_id=cid, generation=generation)
            candidates.append(candidate)
        run = self.store.get_run(run_id)
        selection = select_winner(candidates, float(run.get("current_best_score", 0)))
        winner_id = selection["winner_candidate_id"]
        for candidate in candidates:
            cid = candidate["candidate_id"]
            if cid == winner_id:
                self.store.update_candidate(run_id, cid, lambda row: row.update({"selection_status": "winner", "status": "complete", "selection_reasons": [selection["reason"]]}))
            else:
                self.store.update_candidate(run_id, cid, lambda row: row.update({"selection_status": "rejected" if row.get("score", {}).get("hard_rejected") else "loser", "rejection_reasons": list(set(row.get("rejection_reasons", []) + [selection["reason"]]))}))
        checkpoint_id = None
        if winner_id:
            cp = self.store.create_checkpoint(run_id, winner_id, "current_best")
            checkpoint_id = cp["checkpoint_id"]
        parent_score = float(run.get("current_best_score", 0) or 0)
        for candidate in candidates:
            finalized = self.store.get_candidate(run_id, candidate["candidate_id"])
            self._record_mutation_outcome(run, finalized, generation, parent_score)

        def update(row: dict) -> None:
            row.setdefault("lineage_edges", []).extend({"parent": parent_id, "child": c["candidate_id"]} for c in candidates)
            row.setdefault("generation_results", []).append({"generation": generation, "candidate_ids": [c["candidate_id"] for c in candidates], **selection})
            row["highest_scoring_candidate_id"] = selection["highest_scoring_candidate_id"] or row.get("highest_scoring_candidate_id")
            if winner_id:
                row["current_best_candidate_id"] = winner_id
                row["current_best_score"] = selection["highest_score"]
                row["current_best_checkpoint_id"] = checkpoint_id
                winner = next(c for c in candidates if c["candidate_id"] == winner_id)
                row["current_best_required_checks_passed"] = bool(winner.get("required_checks_passed"))
            row["next_mutation_proposals"] = self._select_mutations(row, generation + 1)
            row["next_mutation_generation"] = generation + 1
            row["active_stage"] = None
            row["active_candidate_id"] = None
            generation_failed = all(c.get("status") == "failed" for c in candidates)
            row["consecutive_generation_failures"] = int(row.get("consecutive_generation_failures", 0)) + 1 if generation_failed else 0
            row["generation_results"][-1]["generation_failed"] = generation_failed
        self.store.update_run(run_id, update)
        self.store.append_event(run_id, "info" if winner_id else "warning", "winner_selected" if winner_id else "best_preserved", selection["reason"], candidate_id=winner_id, generation=generation)
        if self.config.memory_learning_enabled:
            for candidate in candidates:
                mutation = candidate.get("mutation") or {}
                rule = self.memory.create_rule({
                    "category": "successful_patterns" if candidate["candidate_id"] == winner_id else "failed_patterns",
                    "title": f"{mutation.get('mutation_type', 'mutation')} observation",
                    "description": "Automatically captured experiment observation; remains scoped",
                    "scope": {"printer_profile": run.get("printer_profile", {}).get("name"), "material": run.get("material_profile", {}).get("material") or run.get("printer_profile", {}).get("material"), "feature_type": mutation.get("mutation_type")},
                    "trigger_conditions": mutation.get("reason", ""), "recommendation": mutation.get("expected_benefit", "review evidence"), "notes": "Not active in production",
                })
                self.memory.observe(rule["id"], {"success": candidate["candidate_id"] == winner_id, "source_model_id": run["source_model_id"], "source_candidate_id": candidate["candidate_id"], "physical": False, "major_regression": False, "note": selection["reason"]})

    def restore_candidate(self, run_id: str, candidate_id: str) -> dict:
        if self.task_active(run_id):
            raise ValueError("cancel or stop the run before restoring a candidate")
        candidate = self.store.get_candidate(run_id, candidate_id)
        run = self.store.get_run(run_id)
        if candidate.get("status") in {"failed", "rejected", "cancelled"} or candidate.get("score", {}).get("hard_rejected"):
            raise ValueError("failed or hard-rejected candidates cannot be restored")
        if candidate.get("required_checks_passed") is not True:
            raise ValueError("candidates that have not passed required checks cannot be restored")
        if has_verified_physical_failure(self.store, run, candidate):
            raise ValueError("candidates with a checksum-verified failed physical print cannot be restored")
        if candidate.get("model_format") == "cadquery-v1":
            if not slice_evidence_ready(self.store, run, candidate):
                raise ValueError("CadQuery candidates without complete slice evidence cannot be restored")
        source_name = "model.py" if candidate.get("model_format") == "cadquery-v1" else "model.scad"
        self.store.candidate_artifact(run_id, candidate_id, source_name)
        checkpoint = self.store.create_checkpoint(run_id, candidate_id, "restored_best")
        score = float(candidate.get("score", {}).get("total", 0))
        self.store.update_run(run_id, lambda row: row.update({
            "current_best_candidate_id": candidate_id,
            "current_best_score": score,
            "current_best_checkpoint_id": checkpoint["checkpoint_id"],
            "current_best_required_checks_passed": bool(candidate.get("required_checks_passed")),
            "stop_reason": None,
        }))
        self.store.update_candidate(run_id, candidate_id, lambda row: row.update({
            "selection_status": "restored_best",
        }))
        self.store.append_event(run_id, "info", "candidate_restored", "Candidate restored as current best without overwriting any version", candidate_id=candidate_id, generation=candidate.get("generation"))
        return self.snapshot(run_id)

    async def branch_candidate(self, run_id: str, candidate_id: str) -> dict:
        candidate = self.store.get_candidate(run_id, candidate_id)
        source_run = self.store.get_run(run_id)
        if candidate.get("status") in {"failed", "rejected", "cancelled"}:
            raise ValueError("failed or rejected candidates cannot be branched")
        if candidate.get("score", {}).get("hard_rejected") or candidate.get("required_checks_passed") is not True:
            raise ValueError("hard-rejected or unchecked candidates cannot be branched")
        if has_verified_physical_failure(self.store, source_run, candidate):
            raise ValueError("candidates with a checksum-verified failed physical print cannot be branched")
        model_format = candidate.get("model_format") or "openscad-legacy"
        if model_format == "cadquery-v1" and not slice_evidence_ready(self.store, source_run, candidate):
            raise ValueError("CadQuery candidates without complete slice evidence cannot be branched")
        source_name = "model.py" if model_format == "cadquery-v1" else "model.scad"
        source = self.store.candidate_artifact(run_id, candidate_id, source_name).read_text(encoding="utf-8")
        payload = {
            "run_mode": "evolve_existing",
            "source_model_id": source_run.get("source_model_id") or "isolated-candidate",
            "source_prompt": source_run.get("source_prompt", ""),
            "validated_spec": source_run["validated_spec"],
            "part_family": source_run.get("part_family", ""),
            "training_consent": source_run.get("training_consent", False),
            "training_consent_decision": source_run.get("training_consent_decision", "not_reviewed"),
            "training_consent_reviewer": source_run.get("training_consent_reviewer", ""),
            "training_consent_reviewed_at": source_run.get("training_consent_reviewed_at"),
            "provenance_status": source_run.get("provenance_status", "unknown"),
            "data_provenance": source_run.get("data_provenance", {}),
            "printer_profile": source_run.get("printer_profile", {}),
            "material_profile": source_run.get("material_profile", {}),
            "locked_constraints": source_run.get("locked_constraints", []),
            "attached_reference_roles": source_run.get("attached_reference_roles", []),
            "export_exclusions": source_run.get("export_exclusions", []),
            "active_backend": source_run.get("active_backend", ""),
            "limits": source_run.get("limits", {}),
            "initial_mutations": [],
        }
        branch = self._run_record(payload)
        branch.update({
            "source_run_id": run_id,
            "source_candidate_id": candidate_id,
            "source_model_id": source_run.get("source_model_id"),
            "model_format": model_format,
        })
        branch["adaptive_history_scope"] = adaptive_history_scope(branch)
        branch_meta = {
            "prompt": source_run.get("source_prompt", ""),
            "branched_from_run": run_id,
            "branched_from_candidate": candidate_id,
        }
        if model_format == "cadquery-v1":
            self.store.create_run(branch, {"model.py": source, "meta.json": json.dumps(branch_meta)})
            baseline_id = new_id("candidate")
            clone = {
                **candidate,
                "candidate_id": baseline_id,
                "run_id": branch["run_id"],
                "generation": 0,
                "version": 0,
                "variant_label": "BASELINE",
                "parent_candidate_id": None,
                "current_best_parent_id": None,
                "mutation": None,
                "selection_status": "baseline",
                "created_at": utc_ts(),
                "updated_at": utc_ts(),
                "artifacts": [],
            }
            self.store.create_candidate(branch["run_id"], clone)
            cloned_artifacts: dict[str, bytes] = {}
            for artifact in candidate.get("artifacts") or []:
                name = artifact.get("name")
                if isinstance(name, str):
                    cloned_artifacts[name] = self.store.candidate_artifact(run_id, candidate_id, name).read_bytes()
            cloned_artifacts[source_name] = source.encode()
            self.store.add_candidate_artifacts(branch["run_id"], baseline_id, cloned_artifacts)
            checkpoint = self.store.create_checkpoint(branch["run_id"], baseline_id, "baseline")
            score = float((candidate.get("score") or {}).get("total", 0))
            self.store.update_run(branch["run_id"], lambda row: row.update({
                "baseline_candidate_id": baseline_id,
                "current_best_candidate_id": baseline_id,
                "highest_scoring_candidate_id": baseline_id,
                "current_best_score": score,
                "baseline_score": score,
                "baseline_checkpoint_id": checkpoint["checkpoint_id"],
            }))
            snapshot = self.snapshot(branch["run_id"])
        else:
            snapshot = await self._attach_baseline(branch, source, branch_meta)
        self.store.append_event(snapshot["run_id"], "info", "run_branched", "Run branched from an isolated candidate", candidate_id=snapshot.get("baseline_candidate_id"), generation=0, data={"source_run_id": run_id, "source_candidate_id": candidate_id})
        return self.snapshot(snapshot["run_id"])

    def delete_candidate(self, run_id: str, candidate_id: str) -> dict:
        if self.task_active(run_id):
            raise ValueError("cancel or stop the run before deleting a candidate")
        run = self.store.get_run(run_id)
        protected = {run.get("baseline_candidate_id"), run.get("current_best_candidate_id"), run.get("generation_zero_candidate_id")}
        if candidate_id in protected:
            raise ValueError("the baseline, generation zero, and current best are protected")
        candidates = self.store.list_candidates(run_id)
        if any(row.get("parent_candidate_id") == candidate_id for row in candidates):
            raise ValueError("candidate has descendants; delete or branch from a leaf candidate instead")
        candidate = self.store.get_candidate(run_id, candidate_id)
        self.store.delete_candidate(run_id, candidate_id)
        self.store.append_event(run_id, "warning", "candidate_deleted", "Candidate explicitly deleted by the user", candidate_id=candidate_id, generation=candidate.get("generation"))
        return self.snapshot(run_id)

    def snapshot(self, run_id: str) -> dict:
        run = self.store.get_run(run_id)
        return {**run, "candidates": self.store.list_candidates(run_id), "events": self.store.list_events(run_id), "checkpoints": self.store.list_checkpoints(run_id)}

    def task_active(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        return bool(task and not task.done())
