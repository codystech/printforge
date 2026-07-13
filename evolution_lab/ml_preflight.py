"""Read-only host checks for an explicitly scheduled Training Lab ML smoke run.

This module has no third-party imports and never downloads models, allocates CUDA
memory, unloads Ollama, or starts training.  Its JSON is evidence about the current
process environment only; an unavailable check is reported as unavailable rather
than inferred as a pass.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


REPORT_SCHEMA = "printforge-ml-preflight-v1"
# This is one compatibility set, not a collection of independent minimums.  A
# smoke run fails closed when even one installed wheel differs.  Update the set
# only after repeating the CUDA smoke test in a fresh, dedicated environment.
PINNED_DISTRIBUTIONS = {
    "torch": "2.6.0",
    "transformers": "4.51.3",
    "peft": "0.15.2",
    "trl": "0.17.0",
    "bitsandbytes": "0.45.5",
    "datasets": "3.6.0",
    "accelerate": "1.6.0",
}
REQUIRED_DISTRIBUTIONS = tuple(PINNED_DISTRIBUTIONS)
MINIMUM_GPU_MEMORY_MIB = 20 * 1024
MINIMUM_FREE_GPU_MEMORY_MIB = 18 * 1024
MINIMUM_FREE_DISK_GIB = 30


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_command(argv: Sequence[str], timeout: float = 10.0) -> dict[str, Any]:
    """Run a fixed argv without a shell and preserve failures as structured data."""

    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return {"available": False, "ok": False, "reason": "command_not_found"}
    except subprocess.TimeoutExpired:
        return {"available": True, "ok": False, "reason": "timeout"}
    return {
        "available": True,
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def package_versions() -> dict[str, dict[str, Any]]:
    versions: dict[str, dict[str, Any]] = {}
    for distribution in REQUIRED_DISTRIBUTIONS:
        try:
            versions[distribution] = {
                "installed": True,
                "version": importlib.metadata.version(distribution),
                "required_version": PINNED_DISTRIBUTIONS[distribution],
            }
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = {
                "installed": False,
                "version": None,
                "required_version": PINNED_DISTRIBUTIONS[distribution],
            }
        versions[distribution]["compatible"] = (
            versions[distribution]["version"] == versions[distribution]["required_version"]
        )
    return versions


def nvidia_devices(device_root: Path = Path("/dev")) -> dict[str, Any]:
    paths = sorted(str(path) for path in device_root.glob("nvidia*"))
    return {
        "paths": paths,
        "nvidiactl": str(device_root / "nvidiactl") in paths,
        "gpu_device_present": any(Path(path).name.removeprefix("nvidia").isdigit() for path in paths),
    }


def nvidia_smi_probe() -> dict[str, Any]:
    result = run_command(
        (
            "nvidia-smi",
            "--query-gpu=index,name,uuid,driver_version,memory.total,memory.free,temperature.gpu",
            "--format=csv,noheader,nounits",
        )
    )
    if not result.get("ok"):
        return result
    gpus = []
    for line in result.pop("stdout", "").splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 7:
            return {**result, "ok": False, "reason": "unexpected_nvidia_smi_output"}
        try:
            gpus.append(
                {
                    "index": int(fields[0]),
                    "name": fields[1],
                    "uuid": fields[2],
                    "driver_version": fields[3],
                    "memory_total_mib": int(fields[4]),
                    "memory_free_mib": int(fields[5]),
                    "temperature_c": int(fields[6]),
                }
            )
        except ValueError:
            return {**result, "ok": False, "reason": "unparseable_nvidia_smi_output"}
    return {**result, "gpus": gpus, "ok": bool(gpus)}


def torch_cuda_probe() -> dict[str, Any]:
    """Probe CUDA in a child process so a broken import cannot kill the caller."""

    code = """
import json
try:
    import torch
    payload = {
        "import_ok": True,
        "torch_version": torch.__version__,
        "torch_cuda_build": torch.version.cuda,
        "cuda_available": torch.cuda.is_available(),
        "device_count": torch.cuda.device_count(),
        "bf16_supported": bool(torch.cuda.is_bf16_supported()) if torch.cuda.is_available() else False,
        "devices": [],
    }
    for index in range(payload["device_count"]):
        props = torch.cuda.get_device_properties(index)
        payload["devices"].append({
            "index": index,
            "name": props.name,
            "total_memory_bytes": props.total_memory,
            "compute_capability": [props.major, props.minor],
        })
except Exception as exc:
    payload = {"import_ok": False, "error_type": type(exc).__name__, "error": str(exc)}
print(json.dumps(payload, sort_keys=True))
"""
    result = run_command((sys.executable, "-c", code), timeout=30.0)
    if not result.get("ok"):
        return result
    try:
        payload = json.loads(result.get("stdout", ""))
    except json.JSONDecodeError:
        return {**result, "ok": False, "reason": "invalid_torch_probe_json"}
    return {"available": True, "ok": bool(payload.get("import_ok")), **payload}


def bitsandbytes_probe() -> dict[str, Any]:
    code = """
import json
try:
    import bitsandbytes
    payload = {"import_ok": True, "version": getattr(bitsandbytes, "__version__", None)}
except Exception as exc:
    payload = {"import_ok": False, "error_type": type(exc).__name__, "error": str(exc)}
print(json.dumps(payload, sort_keys=True))
"""
    result = run_command((sys.executable, "-c", code), timeout=30.0)
    if not result.get("ok"):
        return result
    try:
        payload = json.loads(result.get("stdout", ""))
    except json.JSONDecodeError:
        return {**result, "ok": False, "reason": "invalid_bitsandbytes_probe_json"}
    return {"available": True, "ok": bool(payload.get("import_ok")), **payload}


def disk_probe(path: Path) -> dict[str, Any]:
    existing = path.resolve()
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    usage = shutil.disk_usage(existing)
    return {
        "path": str(existing),
        "free_bytes": usage.free,
        "free_gib": round(usage.free / (1024**3), 2),
    }


def build_report(output_root: Path) -> dict[str, Any]:
    devices = nvidia_devices()
    smi = nvidia_smi_probe()
    packages = package_versions()
    torch_probe = torch_cuda_probe()
    bnb_probe = bitsandbytes_probe()
    disk = disk_probe(output_root)
    gpu = (smi.get("gpus") or [{}])[0]

    host_gpu_bound = bool(devices["nvidiactl"] and devices["gpu_device_present"] and smi.get("ok"))
    cuda_ready = bool(torch_probe.get("cuda_available") and torch_probe.get("device_count", 0) > 0)
    bf16_ready = bool(torch_probe.get("bf16_supported"))
    packages_ready = all(row.get("compatible") is True for row in packages.values())
    vram_ready = (
        int(gpu.get("memory_total_mib", 0)) >= MINIMUM_GPU_MEMORY_MIB
        and int(gpu.get("memory_free_mib", 0)) >= MINIMUM_FREE_GPU_MEMORY_MIB
    )
    disk_ready = disk["free_gib"] >= MINIMUM_FREE_DISK_GIB
    bnb_ready = bool(bnb_probe.get("import_ok"))
    ready = all((host_gpu_bound, cuda_ready, bf16_ready, packages_ready, vram_ready, disk_ready, bnb_ready))

    return {
        "schema": REPORT_SCHEMA,
        "observed_at": utc_now(),
        "status": "ready_for_scheduled_smoke" if ready else "not_ready_for_smoke",
        "ready_for_smoke": ready,
        "scope": "read_only_preflight",
        "thresholds": {
            "minimum_gpu_memory_mib": MINIMUM_GPU_MEMORY_MIB,
            "minimum_free_gpu_memory_mib": MINIMUM_FREE_GPU_MEMORY_MIB,
            "minimum_free_disk_gib": MINIMUM_FREE_DISK_GIB,
        },
        "checks": {
            "nvidia_devices": devices,
            "nvidia_smi": smi,
            "torch_cuda": torch_probe,
            "bitsandbytes": bnb_probe,
            "packages": packages,
            "output_disk": disk,
        },
        "proof": {
            "host_gpu_bound": host_gpu_bound,
            "cuda_available": cuda_ready,
            "bf16_supported": bf16_ready,
            "bitsandbytes_imported": bnb_ready,
            "qlora_forward_backward_completed": False,
            "actual_training": False,
            "evaluated": False,
            "deployed": False,
        },
        "notes": [
            "host_gpu_bound is hardware evidence; it does not identify a named NixOS specialization",
            "preflight imports torch and bitsandbytes in child processes but allocates no model",
            "packages_ready requires the complete pinned compatibility set; version drift fails closed",
            "only a completed smoke report may prove a QLoRA forward/backward pass",
        ],
    }


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def lab_path(value: str | Path, repo_root: Path | None = None, *, allow_root: bool = False) -> Path:
    """Resolve an output path and reject anything outside Training Lab storage."""

    root = (repo_root or repository_root()).resolve()
    lab_root = (root / "training_lab_data").resolve()
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if path != lab_root and lab_root not in path.parents:
        raise argparse.ArgumentTypeError("path must be beneath training_lab_data")
    if path == lab_root and not allow_root:
        raise argparse.ArgumentTypeError("path must be a child of training_lab_data")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json_new(path: Path, payload: dict[str, Any], repo_root: Path | None = None) -> None:
    """Create a JSON evidence file atomically without ever replacing one."""

    path = lab_path(path, repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        # A hard link is an atomic create-if-absent operation.  Unlike replace(),
        # it cannot silently overwrite immutable evidence from an earlier run.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=lab_path,
        default=lab_path("training_lab_data/ml-smoke"),
        help="disk target to inspect; no directory is created unless --report is used",
    )
    parser.add_argument("--report", type=lab_path, help="new JSON report beneath training_lab_data")
    parser.add_argument("--require-ready", action="store_true", help="exit 1 when a smoke run is unsafe")
    args = parser.parse_args(argv)

    report = build_report(args.output_root)
    if args.report:
        write_json_new(args.report, report)
    print(json.dumps(report, indent=2, sort_keys=True))
    return int(args.require_ready and not report["ready_for_smoke"])


if __name__ == "__main__":
    raise SystemExit(main())
