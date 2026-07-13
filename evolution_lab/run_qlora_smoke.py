"""Bounded, opt-in QLoRA forward/backward smoke test for the Training Lab.

Dry-run is the default.  Execution requires an immutable model revision, an
explicitly scheduled GPU window, a completed license review, and a new output
directory beneath ``training_lab_data``.  Model downloads remain disabled unless
``--allow-download`` is supplied.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
import re
import resource
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Sequence

if __package__:
    from .ml_preflight import PINNED_DISTRIBUTIONS, build_report, lab_path, sha256_file, utc_now, write_json_new
else:  # Direct execution keeps the dry-run independent of the app environment.
    from ml_preflight import PINNED_DISTRIBUTIONS, build_report, lab_path, sha256_file, utc_now, write_json_new


MODEL_ID = "Qwen/Qwen2.5-Coder-7B-Instruct"
IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MODEL_REVIEW_SCHEMA = "printforge-model-review-v1"
MODEL_LICENSE = "Apache-2.0"
MIN_STEPS = 10
MAX_STEPS = 50


def bounded_steps(value: str) -> int:
    steps = int(value)
    if not MIN_STEPS <= steps <= MAX_STEPS:
        raise argparse.ArgumentTypeError(f"steps must be between {MIN_STEPS} and {MAX_STEPS}")
    return steps


def bounded_sequence_length(value: str) -> int:
    length = int(value)
    if not 128 <= length <= 4096:
        raise argparse.ArgumentTypeError("max sequence length must be between 128 and 4096")
    return length


def lab_output_path(value: str, repo_root: Path | None = None) -> Path:
    return lab_path(value, repo_root)


def validate_review_manifest(
    path: Path,
    expected_checksum: str,
    model_revision: str,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    """Validate the human model/license review and its archived evidence."""

    path = lab_path(path, repo_root)
    if not SHA256.fullmatch(expected_checksum):
        raise ValueError("--review-manifest-sha256 must be a lowercase SHA-256 digest")
    actual_checksum = sha256_file(path)
    if actual_checksum != expected_checksum:
        raise ValueError("model review manifest checksum does not match")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("model review manifest is not valid JSON") from exc
    required = {
        "schema": MODEL_REVIEW_SCHEMA,
        "decision": "approved",
        "model_id": MODEL_ID,
        "model_revision": model_revision,
        "license_spdx": MODEL_LICENSE,
    }
    for key, expected in required.items():
        if manifest.get(key) != expected:
            raise ValueError(f"model review manifest {key} must equal {expected!r}")
    expected_url = f"https://huggingface.co/{MODEL_ID}/tree/{model_revision}"
    if manifest.get("source_url") != expected_url:
        raise ValueError("model review source_url must pin the reviewed Hugging Face revision")
    for key in ("reviewed_by", "reviewed_at", "license_artifact", "model_card_artifact"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValueError(f"model review manifest requires {key}")
    try:
        reviewed_at = datetime.fromisoformat(manifest["reviewed_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("model review reviewed_at must be an RFC 3339 datetime with timezone") from exc
    if reviewed_at.tzinfo is None or reviewed_at.utcoffset() is None:
        raise ValueError("model review reviewed_at must be an RFC 3339 datetime with timezone")
    evidence: dict[str, str] = {}
    for artifact_key, checksum_key in (
        ("license_artifact", "license_sha256"),
        ("model_card_artifact", "model_card_sha256"),
    ):
        checksum = manifest.get(checksum_key)
        if not isinstance(checksum, str) or not SHA256.fullmatch(checksum):
            raise ValueError(f"model review manifest requires lowercase {checksum_key}")
        artifact = lab_path(path.parent / manifest[artifact_key], repo_root)
        if path.parent.resolve() not in artifact.parents:
            raise ValueError(f"{artifact_key} must remain beside the review manifest")
        if sha256_file(artifact) != checksum:
            raise ValueError(f"{artifact_key} checksum does not match")
        evidence[artifact_key] = str(artifact)
        evidence[checksum_key] = checksum
    return {
        "manifest_path": str(path),
        "manifest_sha256": actual_checksum,
        "model_id": MODEL_ID,
        "model_revision": model_revision,
        "license_spdx": MODEL_LICENSE,
        **evidence,
    }


def active_compute_processes() -> list[dict[str, str]]:
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-compute-apps=pid,process_name,used_memory",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [{"pid": "unknown", "process_name": "nvidia-smi probe unavailable", "used_memory_mib": "unknown"}]
    if result.returncode != 0:
        return [{"pid": "unknown", "process_name": "nvidia-smi probe failed", "used_memory_mib": "unknown"}]
    rows = []
    for line in result.stdout.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 3:
            rows.append(
                {
                    "pid": "unknown",
                    "process_name": "malformed nvidia-smi compute row",
                    "used_memory_mib": "unknown",
                    "raw": line,
                }
            )
            continue
        pid, process_name, used_memory = fields
        try:
            valid = bool(pid.isdigit() and process_name and int(used_memory) >= 0)
        except ValueError:
            valid = False
        if not valid:
            rows.append(
                {
                    "pid": "unknown",
                    "process_name": "unparseable nvidia-smi compute row",
                    "used_memory_mib": "unknown",
                    "raw": line,
                }
            )
            continue
        rows.append({"pid": pid, "process_name": process_name, "used_memory_mib": used_memory})
    return rows


def directory_size(path: Path) -> int:
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def isolated_cache_environment(output_dir: Path, allow_download: bool) -> dict[str, str]:
    cache = output_dir / "cache"
    return {
        "HF_HOME": str(cache / "huggingface"),
        "HF_HUB_CACHE": str(cache / "huggingface" / "hub"),
        "HF_ASSETS_CACHE": str(cache / "huggingface" / "assets"),
        "HF_DATASETS_CACHE": str(cache / "huggingface" / "datasets"),
        "TRANSFORMERS_CACHE": str(cache / "huggingface" / "transformers"),
        "TORCH_HOME": str(cache / "torch"),
        "XDG_CACHE_HOME": str(cache / "xdg"),
        "CUDA_CACHE_PATH": str(cache / "cuda"),
        "TMPDIR": str(output_dir / "tmp"),
        "HF_TOKEN_PATH": str(cache / "huggingface" / "token"),
        "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "TRANSFORMERS_OFFLINE": "0" if allow_download else "1",
        "HF_DATASETS_OFFLINE": "0" if allow_download else "1",
        "HF_HUB_OFFLINE": "0" if allow_download else "1",
    }


@contextmanager
def apply_isolated_cache_environment(output_dir: Path, allow_download: bool):
    values = isolated_cache_environment(output_dir, allow_download)
    for key, value in values.items():
        if value.startswith(str(output_dir)):
            target = Path(value)
            (target.parent if key == "HF_TOKEN_PATH" else target).mkdir(parents=True, exist_ok=True)
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield values
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def gpu_telemetry_sample(gpu_index: int = 0) -> dict[str, int]:
    result = subprocess.run(
        [
            "nvidia-smi",
            f"--id={gpu_index}",
            "--query-gpu=memory.used,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("nvidia-smi telemetry probe failed")
    fields = [field.strip() for field in result.stdout.strip().split(",")]
    if len(fields) != 2:
        raise RuntimeError("malformed nvidia-smi telemetry row")
    try:
        memory_used_mib, temperature_c = (int(field) for field in fields)
    except ValueError as exc:
        raise RuntimeError("unparseable nvidia-smi telemetry row") from exc
    if memory_used_mib < 0 or temperature_c < 0:
        raise RuntimeError("invalid negative nvidia-smi telemetry value")
    return {"memory_used_mib": memory_used_mib, "temperature_c": temperature_c}


class GpuTelemetryPoller:
    def __init__(self, interval_seconds: float = 0.5):
        self.interval_seconds = interval_seconds
        self.samples: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample(self) -> None:
        try:
            self.samples.append({"observed_at": utc_now(), **gpu_telemetry_sample()})
        except Exception as exc:  # Evidence keeps the exact probe failure without hiding it.
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def start(self) -> None:
        self._sample()
        if not self.samples:
            raise RuntimeError(f"GPU telemetry could not start: {self.errors[-1]}")

        def poll() -> None:
            while not self._stop.wait(self.interval_seconds):
                self._sample()

        self._thread = threading.Thread(target=poll, name="printforge-gpu-telemetry", daemon=True)
        self._thread.start()

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(2.0, self.interval_seconds * 4))
        self._sample()
        return self.summary()

    def summary(self) -> dict[str, Any]:
        if not self.samples:
            return {"sample_count": 0, "errors": self.errors}
        return {
            "sample_count": len(self.samples),
            "peak_gpu_memory_used_mib": max(row["memory_used_mib"] for row in self.samples),
            "peak_temperature_c": max(row["temperature_c"] for row in self.samples),
            "first_sample": self.samples[0],
            "last_sample": self.samples[-1],
            "errors": self.errors,
        }


SMOKE_PROMPT = """### Instruction
Return only a cadquery-v1 model.py for a printable calibration block. The file
must contain a literal PARAMETERS schema and build(params, assets).
### Completion
"""
SMOKE_COMPLETION = """import cadquery as cq

PARAMETERS = {
    "width": {"type": "number", "default": 20.0, "minimum": 10.0, "maximum": 40.0},
    "depth": {"type": "number", "default": 20.0, "minimum": 10.0, "maximum": 40.0},
    "height": {"type": "number", "default": 20.0, "minimum": 10.0, "maximum": 40.0},
}

def build(params, assets):
    width = float(params["width"])
    depth = float(params["depth"])
    height = float(params["height"])
    body = cq.Workplane("XY").box(width, depth, height)
    return {"parts": {"body": body}, "roles": {"body": "printable"}}
"""


def fixed_length_smoke_tokens(tokenizer: Any, sequence_length: int) -> dict[str, Any]:
    """Build an exact-length synthetic prompt/completion without hidden truncation."""

    prompt_ids = tokenizer.encode(SMOKE_PROMPT, add_special_tokens=True)
    completion_ids = tokenizer.encode(SMOKE_COMPLETION, add_special_tokens=False)
    if not completion_ids or len(prompt_ids) >= sequence_length:
        raise RuntimeError("tokenizer cannot build the fixed-length smoke fixture")
    repeated_completion = (completion_ids * ((sequence_length // len(completion_ids)) + 1))[
        : sequence_length - len(prompt_ids)
    ]
    input_ids = prompt_ids + repeated_completion
    labels = ([-100] * len(prompt_ids)) + repeated_completion.copy()
    if len(input_ids) != sequence_length or len(labels) != sequence_length:
        raise AssertionError("fixed-length smoke fixture construction failed")
    return {
        "input_ids": input_ids,
        "labels": labels,
        "prompt_tokens": len(prompt_ids),
        "completion_tokens": len(repeated_completion),
        "sequence_length": len(input_ids),
        "synthetic_fixture": "cadquery-v1-prompt-completion-v1",
    }


def adapter_file_hashes(adapter_dir: Path) -> dict[str, str]:
    return {
        str(path.relative_to(adapter_dir)): sha256_file(path)
        for path in sorted(adapter_dir.rglob("*"))
        if path.is_file()
    }


def optimizer_update_count(output_dir: Path) -> int:
    return len(list(output_dir.glob("optimizer-step-*.json"))) if output_dir.is_dir() else 0


def training_proof_after_failure(output_dir: Path | None) -> tuple[int, dict[str, bool]]:
    updates = optimizer_update_count(output_dir) if output_dir else 0
    return updates, {
        "qlora_forward_backward_completed": False,
        "actual_training": updates > 0,
        "evaluated": False,
        "deployed": False,
    }


def smoke_plan(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema": "printforge-qlora-smoke-plan-v1",
        "model_id": args.model_id,
        "model_revision": args.revision,
        "review_manifest": str(args.review_manifest) if args.review_manifest else None,
        "review_manifest_sha256": args.review_manifest_sha256,
        "steps": args.steps,
        "max_sequence_length": args.max_sequence_length,
        "output_dir": str(args.output_dir) if args.output_dir else None,
        "network_download_allowed": args.allow_download,
        "required_package_versions": PINNED_DISTRIBUTIONS,
        "cache_policy": "isolated_beneath_output_dir",
        "quantization": {
            "load_in_4bit": True,
            "bnb_4bit_quant_type": "nf4",
            "bnb_4bit_use_double_quant": True,
            "bnb_4bit_compute_dtype": "bfloat16",
        },
        "lora": {
            "r": 32,
            "lora_alpha": 16,
            "lora_dropout": 0.05,
            "target_modules": "all-linear",
            "task_type": "CAUSAL_LM",
        },
        "proof": {
            "qlora_forward_backward_completed": False,
            "actual_training": False,
            "evaluated": False,
            "deployed": False,
        },
    }


def run_smoke(args: argparse.Namespace) -> dict[str, Any]:
    # The CLI failure handler may write only after this invocation successfully
    # created the immutable run directory.  In particular, a rejected existing
    # directory must remain byte-for-byte untouched.
    args._output_owned = False
    if args.model_id != MODEL_ID:
        raise ValueError(f"only {MODEL_ID} is allowlisted")
    if not args.revision or not IMMUTABLE_REVISION.fullmatch(args.revision):
        raise ValueError("--revision must be the model's immutable 40-character commit hash")
    if not args.confirm_gpu_window:
        raise ValueError("--confirm-gpu-window is required; this tool never unloads Ollama")
    if args.output_dir is None:
        raise ValueError("--output-dir beneath training_lab_data is required")
    args.output_dir = lab_path(args.output_dir)
    if args.output_dir.exists():
        raise FileExistsError("smoke output is immutable; choose a new output directory")
    if args.review_manifest is None or args.review_manifest_sha256 is None:
        raise ValueError("--review-manifest and --review-manifest-sha256 are required")
    review = validate_review_manifest(
        args.review_manifest, args.review_manifest_sha256, args.revision
    )

    preflight = build_report(args.output_dir)
    if not preflight["ready_for_smoke"]:
        raise RuntimeError("preflight is not ready; inspect its failed checks before scheduling GPU work")
    busy = active_compute_processes()
    if busy:
        raise RuntimeError(f"GPU has active or unverifiable compute processes: {busy}")

    args.output_dir.mkdir(parents=True, exist_ok=False)
    args._output_owned = True
    write_json_new(args.output_dir / "preflight.json", preflight)
    write_json_new(args.output_dir / "review-evidence.json", review)
    started_at = utc_now()
    started = time.monotonic()

    telemetry: GpuTelemetryPoller | None = None
    telemetry_summary: dict[str, Any] = {"sample_count": 0, "errors": ["not_started"]}
    with apply_isolated_cache_environment(args.output_dir, args.allow_download) as cache_environment:
        try:
            # Imports are intentionally delayed until every safety gate and cache
            # boundary has passed.
            import torch
            from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training
            from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

            telemetry = GpuTelemetryPoller()
            telemetry.start()
            torch.manual_seed(args.seed)
            torch.cuda.manual_seed_all(args.seed)
            torch.cuda.reset_peak_memory_stats()
            quantization = BitsAndBytesConfig(
                load_in_4bit=True,
                bnb_4bit_quant_type="nf4",
                bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16,
            )
            cache_dir = str(args.output_dir / "cache" / "huggingface" / "hub")
            load_kwargs = {
                "revision": args.revision,
                "local_files_only": not args.allow_download,
                "cache_dir": cache_dir,
            }
            tokenizer = AutoTokenizer.from_pretrained(args.model_id, **load_kwargs)
            if tokenizer.pad_token is None:
                tokenizer.pad_token = tokenizer.eos_token
            model = AutoModelForCausalLM.from_pretrained(
                args.model_id, quantization_config=quantization, device_map={"": 0}, **load_kwargs
            )
            model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
            model = get_peft_model(
                model,
                LoraConfig(r=32, lora_alpha=16, lora_dropout=0.05,
                           target_modules="all-linear", bias="none", task_type="CAUSAL_LM"),
            )
            model.config.use_cache = False
            model.train()
            fixture = fixed_length_smoke_tokens(tokenizer, args.max_sequence_length)
            encoded = {
                "input_ids": torch.tensor([fixture["input_ids"]], device="cuda:0"),
                "attention_mask": torch.ones((1, fixture["sequence_length"]), dtype=torch.long, device="cuda:0"),
                "labels": torch.tensor([fixture["labels"]], device="cuda:0"),
            }
            optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=2e-4)
            losses = []
            for step in range(1, args.steps + 1):
                optimizer.zero_grad(set_to_none=True)
                loss = model(**encoded).loss
                loss.backward()
                optimizer.step()
                losses.append(float(loss.detach().cpu()))
                write_json_new(args.output_dir / f"optimizer-step-{step:06d}.json", {
                    "schema": "printforge-optimizer-update-v1", "step": step,
                    "observed_at": utc_now(), "actual_training": True,
                })
            torch.cuda.synchronize()
            adapter_dir = args.output_dir / "adapter"
            model.save_pretrained(adapter_dir, safe_serialization=True)
            tokenizer.save_pretrained(adapter_dir)
            adapter_hashes = adapter_file_hashes(adapter_dir)
            adapter_manifest = {
                "schema": "printforge-peft-adapter-manifest-v1", "immutable": True,
                "created_at": utc_now(), "model_id": MODEL_ID, "model_revision": args.revision,
                "review_manifest_sha256": review["manifest_sha256"],
                "files_sha256": adapter_hashes, "package_versions": PINNED_DISTRIBUTIONS,
            }
            adapter_manifest_path = args.output_dir / "adapter-manifest.json"
            write_json_new(adapter_manifest_path, adapter_manifest)
            adapter_manifest_sha256 = sha256_file(adapter_manifest_path)
            adapter_manifest_path.chmod(0o444)
            for path in adapter_dir.rglob("*"):
                if path.is_file():
                    path.chmod(0o444)
        finally:
            if telemetry is not None:
                telemetry_summary = telemetry.stop()
            write_json_new(args.output_dir / "gpu-telemetry.json", telemetry_summary)
    elapsed = time.monotonic() - started
    report = {
        "schema": "printforge-qlora-smoke-report-v1",
        "started_at": started_at,
        "finished_at": utc_now(),
        "status": "completed",
        "configuration": {key: value for key, value in smoke_plan(args).items() if key != "proof"},
        "metrics": {
            "elapsed_seconds": round(elapsed, 3),
            "steps": args.steps,
            "tokens_per_step": fixture["sequence_length"],
            "prompt_tokens": fixture["prompt_tokens"],
            "completion_tokens": fixture["completion_tokens"],
            "synthetic_fixture": fixture["synthetic_fixture"],
            "end_to_end_tokens_per_second": round(
                (fixture["sequence_length"] * args.steps) / elapsed, 3
            ),
            "initial_loss": losses[0],
            "final_loss": losses[-1],
            "peak_vram_allocated_bytes": torch.cuda.max_memory_allocated(),
            "peak_vram_reserved_bytes": torch.cuda.max_memory_reserved(),
            "peak_process_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
            "peak_gpu_memory_used_mib": telemetry_summary.get("peak_gpu_memory_used_mib"),
            "peak_temperature_c": telemetry_summary.get("peak_temperature_c"),
            "telemetry_sample_count": telemetry_summary.get("sample_count", 0),
            "adapter_size_bytes": directory_size(adapter_dir),
        },
        "cache_environment": cache_environment,
        "model_review": review,
        "adapter_manifest": {
            "path": str(adapter_manifest_path), "sha256": adapter_manifest_sha256,
            "file_count": len(adapter_hashes),
        },
        "package_versions": preflight["checks"]["packages"],
        "proof": {
            "qlora_forward_backward_completed": True,
            "actual_training": True,
            "evaluated": False,
            "deployed": False,
        },
        "warning": "A synthetic smoke loss is environment proof, not evidence of improved model or print quality.",
    }
    write_json_new(args.output_dir / "report.json", report)
    return report


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--execute", action="store_true", help="run instead of printing the dry-run plan")
    result.add_argument("--model-id", choices=(MODEL_ID,), default=MODEL_ID)
    result.add_argument("--revision")
    result.add_argument("--review-manifest", type=lab_output_path)
    result.add_argument("--review-manifest-sha256")
    result.add_argument("--steps", type=bounded_steps, default=10)
    result.add_argument("--max-sequence-length", type=bounded_sequence_length, default=512)
    result.add_argument("--seed", type=int, default=42)
    result.add_argument("--output-dir", type=lab_output_path)
    result.add_argument("--allow-download", action="store_true")
    result.add_argument("--confirm-gpu-window", action="store_true")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    plan = smoke_plan(args)
    if not args.execute:
        print(json.dumps(plan, indent=2, sort_keys=True))
        return 0
    try:
        report = run_smoke(args)
    except Exception as exc:
        updates, failure_proof = training_proof_after_failure(args.output_dir)
        failure = {
            **plan,
            "schema": "printforge-qlora-smoke-report-v1",
            "status": "failed",
            "finished_at": utc_now(),
            "failure": {"type": type(exc).__name__, "message": str(exc)},
            "optimizer_updates_completed": updates,
            "proof": failure_proof,
        }
        if getattr(args, "_output_owned", False) and args.output_dir and args.output_dir.is_dir():
            write_json_new(args.output_dir / "failure.json", failure)
        print(json.dumps(failure, indent=2, sort_keys=True))
        return 1
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
