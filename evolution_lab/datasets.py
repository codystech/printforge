"""Secret-safe structured dataset exports; no model-weight training occurs here."""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import PurePath
from typing import Any

from .store import EvolutionStore, new_id, utc_ts


SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|secret|token|authorization|credential|private[_-]?prompt|system[_-]?prompt|filesystem[_-]?path|absolute[_-]?path)",
    re.IGNORECASE,
)
ABSOLUTE_PATH = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def sanitize(value: Any, *, key: str = "") -> Any:
    """Remove secrets and private paths recursively from export material."""

    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item, key=key) for item in value]
    if isinstance(value, tuple):
        return [sanitize(item, key=key) for item in value]
    if isinstance(value, str) and ABSOLUTE_PATH.match(value.strip()):
        return f"[REDACTED-PATH]/{PurePath(value).name}"
    return value


def _candidate_ref(candidate: dict) -> dict:
    return {
        "candidate_id": candidate.get("candidate_id"),
        "model_artifacts": [item.get("name") for item in candidate.get("artifacts", [])],
        "mutation": candidate.get("mutation"),
        "score": candidate.get("score"),
    }


def build_examples(store: EvolutionStore, dataset_type: str, run_id: str | None = None) -> list[dict]:
    runs = [store.get_run(run_id)] if run_id else [r for r in store.list_runs(include_demo=False) if not r.get("demo")]
    if any(run.get("demo") for run in runs):
        return []
    examples: list[dict] = []

    if dataset_type in {"preference", "all"}:
        for run in runs:
            candidates = store.list_candidates(run["run_id"])
            by_generation: dict[int, list[dict]] = {}
            for candidate in candidates:
                if candidate.get("variant_label") in {"A", "B"}:
                    by_generation.setdefault(int(candidate.get("generation", 0)), []).append(candidate)
            for generation, pair in by_generation.items():
                winners = [item for item in pair if item.get("selection_status") == "winner"]
                losers = [item for item in pair if item.get("selection_status") in {"loser", "rejected", "regression"}]
                if winners and losers:
                    examples.append({
                        "example_type": "preference_pair",
                        "run_id": run["run_id"],
                        "generation": generation,
                        "prompt": run.get("source_prompt", ""),
                        "spec": run.get("validated_spec", ""),
                        "chosen_candidate": _candidate_ref(winners[0]),
                        "rejected_candidate": _candidate_ref(losers[0]),
                        "chosen_score": winners[0].get("score", {}).get("total"),
                        "rejected_score": losers[0].get("score", {}).get("total"),
                        "reasons": winners[0].get("selection_reasons", []),
                        "printer_profile": run.get("printer_profile", {}),
                        "material_profile": run.get("material_profile", {}),
                    })

    if dataset_type in {"repair", "all"}:
        for run in runs:
            for candidate in store.list_candidates(run["run_id"]):
                repair = candidate.get("repair_example")
                if repair:
                    examples.append({"example_type": "repair", "run_id": run["run_id"], **repair})

    if dataset_type in {"calibration", "all"}:
        for record in store.list_records("calibrations"):
            examples.append({
                "example_type": "calibration",
                "printer_profile": record.get("printer_profile"),
                "material": record.get("material"),
                "nozzle": record.get("nozzle"),
                "layer_height": record.get("layer_height"),
                "feature": record.get("calibration_type"),
                "tested_values": record.get("tested_values", []),
                "physical_results": record.get("physical_results", []),
                "recommended_value": record.get("recommended_value"),
            })

    if dataset_type in {"supervised", "all"}:
        for run in runs:
            for candidate in store.list_candidates(run["run_id"]):
                if candidate.get("selection_status") == "winner" and not candidate.get("score", {}).get("hard_rejected"):
                    examples.append({
                        "example_type": "supervised",
                        "input_prompt": run.get("source_prompt", ""),
                        "validated_spec": run.get("validated_spec", ""),
                        "generation_instructions": candidate.get("prompt_used", ""),
                        "successful_model": _candidate_ref(candidate),
                        "qa_result": candidate.get("qa_results", []),
                        "physical_print_result": candidate.get("physical_validation_status", "not_submitted"),
                    })

    if dataset_type in {"failure", "all"}:
        for run in runs:
            for candidate in store.list_candidates(run["run_id"]):
                failures = candidate.get("failure_reasons", []) + candidate.get("rejection_reasons", [])
                if failures:
                    examples.append({
                        "example_type": "failure",
                        "prompt": run.get("source_prompt", ""),
                        "spec": run.get("validated_spec", ""),
                        "failed_model": _candidate_ref(candidate),
                        "failure_types": failures,
                        "root_cause": candidate.get("root_cause", "unverified"),
                        "recommended_prevention": candidate.get("recommended_prevention", ""),
                    })

    return [sanitize(example) for example in examples]


def _json(examples: list[dict]) -> bytes:
    return (json.dumps(examples, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _jsonl(examples: list[dict]) -> bytes:
    return b"".join(
        json.dumps(example, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        for example in examples
    )


def _csv(examples: list[dict]) -> bytes:
    output = io.StringIO()
    fields = ["example_type", "run_id", "generation", "chosen_score", "rejected_score", "summary_json"]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for example in examples:
        writer.writerow({
            "example_type": example.get("example_type", ""),
            "run_id": example.get("run_id", ""),
            "generation": example.get("generation", ""),
            "chosen_score": example.get("chosen_score", ""),
            "rejected_score": example.get("rejected_score", ""),
            "summary_json": json.dumps(example, ensure_ascii=False, sort_keys=True),
        })
    return output.getvalue().encode("utf-8")


def render_export(examples: list[dict], fmt: str) -> tuple[str, str, bytes]:
    if fmt == "json":
        return "dataset.json", "application/json", _json(examples)
    if fmt == "jsonl":
        return "dataset.jsonl", "application/x-ndjson", _jsonl(examples)
    if fmt == "csv":
        return "dataset.csv", "text/csv", _csv(examples)
    if fmt == "zip":
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("dataset.json", _json(examples))
            archive.writestr("dataset.jsonl", _jsonl(examples))
            archive.writestr("summary.csv", _csv(examples))
            archive.writestr("MANIFEST.json", json.dumps({
                "schema": "printforge-training-dataset-v1",
                "example_count": len(examples),
                "contains_model_weights": False,
                "actual_training_performed": False,
            }, indent=2, sort_keys=True))
        return "dataset.zip", "application/zip", output.getvalue()
    raise ValueError("unsupported export format")


def create_export(store: EvolutionStore, dataset_type: str, fmt: str, run_id: str | None) -> dict:
    examples = build_examples(store, dataset_type, run_id)
    filename, media_type, content = render_export(examples, fmt)
    export_id = new_id("dataset")
    store.write_dataset_file(export_id, filename, content)
    record = {
        "id": export_id,
        "dataset_type": dataset_type,
        "format": fmt,
        "run_id": run_id,
        "filename": filename,
        "media_type": media_type,
        "example_count": len(examples),
        "created_at": utc_ts(),
        "actual_training_performed": False,
    }
    return store.create_record("datasets", record, prefix="dataset")
