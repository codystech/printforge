"""Additive ``cadquery-v1`` contract and sandboxed validation foundation.

This module deliberately does not import CadQuery.  The host dependency and a
pinned runtime worker are Phase 0/host concerns; unit tests inject a deterministic
worker.  What lives here is the security and data contract around that worker:

* ``PARAMETERS`` is parsed with :mod:`ast` and ``literal_eval`` without importing
  or executing generated source.
* ``build(params, assets)`` and its result manifest have one strict shape.
* Bubblewrap receives only a fresh scratch directory plus system runtime roots.
* a candidate cannot pass unless B-rep, STEP, STEP round-trip, STL tessellation,
  and existing mesh checks all produced explicit deterministic evidence.

The active production and legacy Training Lab paths remain OpenSCAD.  Callers must
opt in to this contract and inject a real worker before CadQuery source can run.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


MODEL_FORMAT = "cadquery-v1"
CONTRACT_VERSION = "printforge-cadquery-model-v1"
MANIFEST_VERSION = "printforge-cadquery-manifest-v1"
PARAMETER_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PART_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
EXPORT_ROLES = {"printable", "assembly", "reference", "fit_cutout", "negative"}
PRINTABLE_ROLES = {"printable", "assembly"}
PARAMETER_TYPES = {"float", "int", "bool", "str", "choice"}
MAX_SOURCE_BYTES = 1024 * 1024
MAX_AST_NODES = 20_000
MAX_AST_DEPTH = 128
MAX_PARAMETERS = 128
MAX_CHOICES = 256
MAX_TEXT_BYTES = 4096
MAX_PARAMETER_TEXT_BYTES = 64 * 1024
MAX_ABS_NUMERIC = 1_000_000_000.0
SAFE_RUNTIME_ROOTS = ("/usr", "/lib", "/lib64", "/nix/store")
RESERVED_ARTIFACT_NAMES = {"model.py", "model-manifest.json", "result.json"}
REQUIRED_CHECKS = (
    "brep_valid",
    "step_exported",
    "step_roundtrip_valid",
    "stl_tessellated",
    "mesh_checks_passed",
)
EXTRA_HARD_GATES = (
    "build_volume_ok",
    "hard_locks_ok",
    "reference_roles_excluded",
)


class CadQueryContractError(ValueError):
    """Generated source or worker output does not satisfy ``cadquery-v1``."""


class CadQuerySandboxError(RuntimeError):
    """The isolated worker could not run or returned an invalid result."""


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _read_bounded_text(path: Path, limit: int) -> str:
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise CadQuerySandboxError("CadQuery sandbox log exceeded byte limit")
    return raw.decode("utf-8", errors="replace")


def _validated_bwrap_executable(value: str) -> Path:
    """Return one immutable, explicitly selected Bubblewrap executable."""

    path = Path(value)
    if not path.is_absolute():
        raise ValueError("bwrap_binary must be an explicit absolute path")
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise ValueError("bwrap_binary does not exist") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise ValueError("bwrap_binary must be a canonical regular file, not a symlink")
    if metadata.st_uid != 0:
        raise ValueError("bwrap_binary must be root-owned")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ValueError("bwrap_binary must not be group- or world-writable")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError("bwrap_binary does not exist") from exc
    allowed_roots = [
        Path(root).resolve(strict=True)
        for root in SAFE_RUNTIME_ROOTS
        if Path(root).exists() and not Path(root).is_symlink()
    ]
    if not any(_path_within(resolved, root) for root in allowed_roots):
        raise ValueError("bwrap_binary must be beneath a trusted system or store root")
    return resolved


def _literal_parameters(tree: ast.Module) -> dict[str, dict[str, Any]]:
    assignments: list[ast.AST] = []
    values: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "PARAMETERS" for target in node.targets
        ):
            assignments.append(node)
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "PARAMETERS":
            assignments.append(node)
            if node.value is not None:
                values.append(node.value)
    if len(assignments) != 1 or len(values) != 1:
        raise CadQueryContractError("source must define exactly one literal PARAMETERS mapping")
    try:
        value = ast.literal_eval(values[0])
    except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError) as exc:
        raise CadQueryContractError("PARAMETERS must be a literal mapping; expressions are not allowed") from exc
    if not isinstance(value, dict):
        raise CadQueryContractError("PARAMETERS must be a mapping")
    if len(value) > MAX_PARAMETERS:
        raise CadQueryContractError(f"PARAMETERS exceeds the {MAX_PARAMETERS}-parameter limit")
    return value


def _bounded_ast(tree: ast.AST) -> None:
    """Reject oversized/deep trees before literal evaluation walks them again."""

    count = 0
    stack: list[tuple[ast.AST, int]] = [(tree, 1)]
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_AST_NODES:
            raise CadQueryContractError(f"model.py exceeds the {MAX_AST_NODES}-node AST limit")
        if depth > MAX_AST_DEPTH:
            raise CadQueryContractError(f"model.py exceeds the AST depth limit of {MAX_AST_DEPTH}")
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))


def _bounded_text(value: str, label: str) -> None:
    if len(value.encode("utf-8")) > MAX_TEXT_BYTES:
        raise CadQueryContractError(f"{label} exceeds the {MAX_TEXT_BYTES}-byte text limit")


def _finite_number(value: Any, label: str) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CadQueryContractError(f"{label} must be a finite number")
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise CadQueryContractError(f"{label} must be a finite, representable number") from exc
    if not math.isfinite(normalized):
        raise CadQueryContractError(f"{label} must be a finite number")
    if abs(normalized) > MAX_ABS_NUMERIC:
        raise CadQueryContractError(
            f"{label} exceeds the absolute numeric limit of {MAX_ABS_NUMERIC:g}"
        )


def _validate_parameter(name: Any, spec: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not PARAMETER_NAME_RE.fullmatch(name):
        raise CadQueryContractError(f"invalid parameter name: {name!r}")
    if not isinstance(spec, dict):
        raise CadQueryContractError(f"parameter {name!r} must have a literal descriptor mapping")
    unknown = set(spec) - {"type", "default", "min", "max", "step", "choices", "description", "unit"}
    if unknown:
        raise CadQueryContractError(f"parameter {name!r} has unsupported fields: {sorted(unknown)}")
    kind = spec.get("type")
    if kind not in PARAMETER_TYPES:
        raise CadQueryContractError(f"parameter {name!r} has unsupported type {kind!r}")
    if "default" not in spec:
        raise CadQueryContractError(f"parameter {name!r} is missing default")
    default = spec["default"]
    if kind == "bool" and not isinstance(default, bool):
        raise CadQueryContractError(f"parameter {name!r} default must be bool")
    if kind == "int" and (not isinstance(default, int) or isinstance(default, bool)):
        raise CadQueryContractError(f"parameter {name!r} default must be int")
    if kind == "float" and (not isinstance(default, (int, float)) or isinstance(default, bool)):
        raise CadQueryContractError(f"parameter {name!r} default must be numeric")
    if kind in {"str", "choice"} and not isinstance(default, str):
        raise CadQueryContractError(f"parameter {name!r} default must be text")
    if kind in {"int", "float"}:
        _finite_number(default, f"parameter {name!r} default")
    if isinstance(default, str):
        _bounded_text(default, f"parameter {name!r} default")
    if kind in {"int", "float"}:
        for bound in ("min", "max", "step"):
            if bound in spec and (not isinstance(spec[bound], (int, float)) or isinstance(spec[bound], bool)):
                raise CadQueryContractError(f"parameter {name!r} {bound} must be numeric")
            if bound in spec:
                _finite_number(spec[bound], f"parameter {name!r} {bound}")
        if "min" in spec and "max" in spec and float(spec["min"]) > float(spec["max"]):
            raise CadQueryContractError(f"parameter {name!r} min exceeds max")
        if "min" in spec and float(default) < float(spec["min"]):
            raise CadQueryContractError(f"parameter {name!r} default is below min")
        if "max" in spec and float(default) > float(spec["max"]):
            raise CadQueryContractError(f"parameter {name!r} default is above max")
        if "step" in spec and float(spec["step"]) <= 0:
            raise CadQueryContractError(f"parameter {name!r} step must be positive")
    if kind == "choice":
        choices = spec.get("choices")
        if not isinstance(choices, list) or not choices or any(not isinstance(item, str) for item in choices):
            raise CadQueryContractError(f"parameter {name!r} choices must be a non-empty text list")
        if len(choices) > MAX_CHOICES:
            raise CadQueryContractError(f"parameter {name!r} exceeds the {MAX_CHOICES}-choice limit")
        for item in choices:
            _bounded_text(item, f"parameter {name!r} choice")
        if default not in choices:
            raise CadQueryContractError(f"parameter {name!r} default must appear in choices")
    for text_field in ("description", "unit"):
        if text_field in spec and not isinstance(spec[text_field], str):
            raise CadQueryContractError(f"parameter {name!r} {text_field} must be text")
        if text_field in spec:
            _bounded_text(spec[text_field], f"parameter {name!r} {text_field}")
    return dict(spec)


def parse_model_contract(source: str) -> dict[str, Any]:
    """Parse and validate generated source without importing or executing it."""

    if not isinstance(source, str) or not source.strip():
        raise CadQueryContractError("model source is empty")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise CadQueryContractError(f"model.py exceeds the {MAX_SOURCE_BYTES}-byte source limit")
    try:
        tree = ast.parse(source, filename="model.py", mode="exec")
    except (RecursionError, MemoryError) as exc:
        raise CadQueryContractError("model.py exhausted parser resource limits") from exc
    except SyntaxError as exc:
        raise CadQueryContractError(f"model.py is invalid Python: {exc.msg}") from exc
    _bounded_ast(tree)
    parameters = _literal_parameters(tree)
    normalized = {name: _validate_parameter(name, spec) for name, spec in parameters.items()}
    parameter_text_bytes = sum(
        len(value.encode("utf-8"))
        for spec in normalized.values()
        for value in (
            [spec.get("default")] if isinstance(spec.get("default"), str) else []
        ) + list(spec.get("choices") or []) + [spec.get("description", ""), spec.get("unit", "")]
        if isinstance(value, str)
    )
    if parameter_text_bytes > MAX_PARAMETER_TEXT_BYTES:
        raise CadQueryContractError(
            f"PARAMETERS exceeds the {MAX_PARAMETER_TEXT_BYTES}-byte aggregate text limit"
        )
    builds = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build"]
    if len(builds) != 1 or isinstance(builds[0], ast.AsyncFunctionDef):
        raise CadQueryContractError("source must define exactly one synchronous build(params, assets)")
    build = builds[0]
    positional = [*build.args.posonlyargs, *build.args.args]
    if [arg.arg for arg in positional] != ["params", "assets"]:
        raise CadQueryContractError("build signature must be exactly build(params, assets)")
    if build.args.vararg or build.args.kwarg or build.args.kwonlyargs or build.args.defaults:
        raise CadQueryContractError("build(params, assets) cannot use defaults or variadic arguments")
    return {
        "model_format": MODEL_FORMAT,
        "contract_version": CONTRACT_VERSION,
        "parameters": normalized,
        "source_sha256": _sha256(source),
    }


def parameter_defaults(parameters: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    return {name: spec["default"] for name, spec in parameters.items()}


def validate_parameter_values(
    schema: Mapping[str, Mapping[str, Any]], values: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Merge overrides with defaults and re-check them against the literal schema."""

    supplied = dict(values or {})
    unknown = set(supplied) - set(schema)
    if unknown:
        raise CadQueryContractError(f"unknown parameter values: {sorted(unknown)}")
    merged = parameter_defaults(schema)
    merged.update(supplied)
    for name, value in merged.items():
        descriptor = dict(schema[name])
        descriptor["default"] = value
        _validate_parameter(name, descriptor)
    return merged


def validate_parts(parts: Any) -> list[dict[str, Any]]:
    """Validate the worker's named-part, transform and export-role declaration."""

    if not isinstance(parts, list) or not parts:
        raise CadQueryContractError("build result must contain at least one named part")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    seen_artifacts: set[str] = set()
    for raw in parts:
        if not isinstance(raw, dict):
            raise CadQueryContractError("every part descriptor must be a mapping")
        name = raw.get("name")
        if not isinstance(name, str) or not PART_NAME_RE.fullmatch(name) or name in seen:
            raise CadQueryContractError(f"invalid or duplicate part name: {name!r}")
        seen.add(name)
        role = raw.get("export_role")
        if role not in EXPORT_ROLES:
            raise CadQueryContractError(f"part {name!r} has invalid export_role {role!r}")
        transform = raw.get("transform")
        if not isinstance(transform, dict) or set(transform) != {"translation_mm", "rotation_deg"}:
            raise CadQueryContractError(
                f"part {name!r} transform must declare translation_mm and rotation_deg"
            )
        for key in ("translation_mm", "rotation_deg"):
            vector = transform[key]
            if not isinstance(vector, list) or len(vector) != 3 or any(
                not isinstance(value, (int, float)) or isinstance(value, bool) for value in vector
            ):
                raise CadQueryContractError(f"part {name!r} {key} must be a three-number list")
            for value in vector:
                _finite_number(value, f"part {name!r} {key}")
        step = raw.get("step_artifact")
        stl = raw.get("stl_artifact")
        for label, artifact in (("step_artifact", step), ("stl_artifact", stl)):
            if not isinstance(artifact, str) or not ASSET_NAME_RE.fullmatch(artifact):
                raise CadQueryContractError(f"part {name!r} has invalid {label}")
            if artifact in seen_artifacts:
                raise CadQueryContractError(f"part {name!r} reuses artifact {artifact!r}")
            seen_artifacts.add(artifact)
        if not step.casefold().endswith((".step", ".stp")) or not stl.casefold().endswith(".stl"):
            raise CadQueryContractError(
                f"part {name!r} must use STEP and STL artifact extensions"
            )
        normalized.append({
            "name": name,
            "export_role": role,
            "printable": role in PRINTABLE_ROLES,
            "transform": {
                "translation_mm": [float(v) for v in transform["translation_mm"]],
                "rotation_deg": [float(v) for v in transform["rotation_deg"]],
            },
            "step_artifact": step,
            "stl_artifact": stl,
        })
    if not any(item["printable"] for item in normalized):
        raise CadQueryContractError("build result has no printable or assembly part")
    return normalized


@dataclass(frozen=True)
class SandboxLimits:
    timeout_seconds: int = 120
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_worker_bytes: int = 512 * 1024
    max_asset_count: int = 32
    max_asset_bytes: int = 128 * 1024 * 1024
    max_total_asset_bytes: int = 192 * 1024 * 1024
    max_input_bytes: int = 256 * 1024 * 1024
    max_report_bytes: int = 2 * 1024 * 1024
    max_artifact_count: int = 128
    max_artifact_bytes: int = 512 * 1024 * 1024
    max_total_artifact_bytes: int = 640 * 1024 * 1024
    max_scratch_bytes: int = 768 * 1024 * 1024
    max_scratch_files: int = 512
    max_log_bytes: int = 2 * 1024 * 1024


@dataclass
class SandboxResult:
    report: dict[str, Any]
    artifacts: dict[str, bytes]
    stdout: str = ""
    stderr: str = ""
    trusted_evidence: bool = False


class CadQueryExecutor(Protocol):
    def execute(
        self,
        source: str,
        parameters: Mapping[str, Any],
        assets: Mapping[str, bytes],
    ) -> SandboxResult: ...


class TrustedArtifactValidator(Protocol):
    """Parent-side validator whose checks inspect captured filesystem artifacts."""

    def __call__(
        self,
        *,
        parts: list[dict[str, Any]],
        artifact_paths: Mapping[str, Path],
    ) -> Mapping[str, bool]: ...


class BubblewrapExecutor:
    """Run an injected *untrusted generation* worker in Bubblewrap.

    ``worker_source`` is copied into the fresh scratch directory.  It must read
    ``/work/request.json`` and write ``/work/result.json`` plus declared artifacts
    below ``/work/output``.  The repository, user home, PrintForge library and
    upload directories are never mounted.  Its report is never deterministic
    evidence by itself: a separately injected trusted validator must derive the
    final parts/checks report from the captured bytes.

    CPU, memory and process-count enforcement belongs to the future dedicated
    worker service/cgroup.  Applying low host-UID rlimits in ``preexec_fn`` is both
    thread-unsafe and nonfunctional on a normal desktop with many user processes,
    so this dormant foundation deliberately does not claim those limits here.
    """

    def __init__(
        self,
        worker_source: str,
        *,
        bwrap_binary: str,
        runtime_command: tuple[str, ...],
        runtime_roots: tuple[str, ...] = SAFE_RUNTIME_ROOTS,
        limits: SandboxLimits | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        trusted_validator: TrustedArtifactValidator | None = None,
    ):
        if not worker_source.strip():
            raise ValueError("worker_source is required")
        if not runtime_command or not Path(runtime_command[0]).is_absolute():
            raise ValueError("runtime_command must begin with an absolute pinned executable")
        invalid_roots = set(runtime_roots) - set(SAFE_RUNTIME_ROOTS)
        if invalid_roots:
            raise ValueError(f"runtime roots are not allowlisted: {sorted(invalid_roots)}")
        runtime_path = Path(runtime_command[0])
        if runtime_path.is_symlink():
            raise ValueError("runtime executable must be a canonical file, not a symlink")
        try:
            resolved_executable = runtime_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError("runtime executable does not exist") from exc
        if not resolved_executable.is_file():
            raise ValueError("runtime executable must be a regular file")
        allowed_existing: list[Path] = []
        for root_name in runtime_roots:
            root = Path(root_name)
            if root.is_symlink():
                raise ValueError(f"runtime root must not be a symlink: {root_name}")
            if root.exists():
                allowed_existing.append(root.resolve(strict=True))
        if not any(_path_within(resolved_executable, root) for root in allowed_existing):
            raise ValueError("runtime executable must resolve beneath an allowlisted runtime root")
        self.worker_source = worker_source
        self.bwrap_binary = str(_validated_bwrap_executable(bwrap_binary))
        self.runtime_command = (str(resolved_executable), *runtime_command[1:])
        self.runtime_roots = tuple(str(path) for path in allowed_existing)
        self.limits = limits or SandboxLimits()
        self.runner = runner
        self.trusted_validator = trusted_validator

    def command(self, scratch: Path) -> list[str]:
        command = [
            self.bwrap_binary,
            "--die-with-parent",
            "--new-session",
            "--unshare-all",
            "--clearenv",
            "--proc", "/proc",
            "--dev", "/dev",
            "--dir", "/work",
            "--bind", str(scratch), "/work",
            "--symlink", "/work/tmp", "/tmp",
            "--chdir", "/work",
            "--setenv", "HOME", "/nonexistent",
            "--setenv", "TMPDIR", "/work/tmp",
            "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
        ]
        for root in self.runtime_roots:
            if Path(root).exists():
                command.extend(["--ro-bind", root, root])
        command.extend([*self.runtime_command, "/work/worker.py"])
        return command

    @staticmethod
    def _tree_usage(
        root: Path,
        *,
        max_bytes: int | None = None,
        max_entries: int | None = None,
    ) -> tuple[int, int]:
        total = 0
        count = 0
        try:
            root_metadata = os.lstat(root)
        except OSError as exc:
            raise CadQuerySandboxError("CadQuery sandbox scratch tree could not be accounted") from exc
        if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
            raise CadQuerySandboxError("CadQuery sandbox scratch root is not a directory")
        stack = [(root, root_metadata.st_dev, root_metadata.st_ino)]
        while stack:
            directory, expected_dev, expected_ino = stack.pop()
            try:
                current = os.lstat(directory)
                if (
                    stat.S_ISLNK(current.st_mode)
                    or not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != (expected_dev, expected_ino)
                ):
                    raise CadQuerySandboxError("CadQuery sandbox scratch directory changed during accounting")
                with os.scandir(directory) as entries:
                    for entry in entries:
                        metadata = os.lstat(entry.path)
                        count += 1
                        total += metadata.st_size
                        if max_entries is not None and count > max_entries:
                            raise CadQuerySandboxError(
                                "CadQuery sandbox exceeded aggregate scratch file-count limit"
                            )
                        if max_bytes is not None and total > max_bytes:
                            raise CadQuerySandboxError(
                                "CadQuery sandbox exceeded aggregate scratch byte limit"
                            )
                        if stat.S_ISDIR(metadata.st_mode):
                            stack.append((Path(entry.path), metadata.st_dev, metadata.st_ino))
            except CadQuerySandboxError:
                raise
            except OSError as exc:
                raise CadQuerySandboxError(
                    "CadQuery sandbox scratch tree could not be accounted"
                ) from exc
        return total, count

    def _run_bounded(self, command: list[str], *, cwd: Path, work: Path) -> subprocess.CompletedProcess[str]:
        """Capture logs outside `/work` and stop on wall, byte, or file-count limits."""

        stdout_path = cwd / "sandbox.stdout"
        stderr_path = cwd / "sandbox.stderr"
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
            started = time.monotonic()
            limit_reason: str | None = None
            while process.poll() is None:
                if time.monotonic() - started > self.limits.timeout_seconds:
                    limit_reason = f"exceeded {self.limits.timeout_seconds}s wall-clock limit"
                    break
                try:
                    self._tree_usage(
                        work,
                        max_bytes=self.limits.max_scratch_bytes,
                        max_entries=self.limits.max_scratch_files,
                    )
                except CadQuerySandboxError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise
                if stdout_path.stat().st_size > self.limits.max_log_bytes or stderr_path.stat().st_size > self.limits.max_log_bytes:
                    limit_reason = "exceeded captured log byte limit"
                    break
                time.sleep(0.05)
            if limit_reason:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise CadQuerySandboxError(f"CadQuery sandbox {limit_reason}")
            returncode = process.wait()
        stdout = _read_bounded_text(stdout_path, self.limits.max_log_bytes)
        stderr = _read_bounded_text(stderr_path, self.limits.max_log_bytes)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def execute(
        self,
        source: str,
        parameters: Mapping[str, Any],
        assets: Mapping[str, bytes],
    ) -> SandboxResult:
        source_bytes = source.encode("utf-8")
        worker_bytes = self.worker_source.encode("utf-8")
        if len(source_bytes) > min(self.limits.max_source_bytes, MAX_SOURCE_BYTES):
            raise CadQueryContractError("model source exceeds the sandbox input limit")
        if len(worker_bytes) > self.limits.max_worker_bytes:
            raise CadQuerySandboxError("trusted worker source exceeds the sandbox input limit")
        if len(assets) > self.limits.max_asset_count:
            raise CadQueryContractError("too many model assets")
        normalized_assets: dict[str, bytes] = {}
        total_input = len(source_bytes) + len(worker_bytes)
        total_asset_bytes = 0
        for name, content in assets.items():
            if not ASSET_NAME_RE.fullmatch(name):
                raise CadQueryContractError(f"invalid asset name: {name!r}")
            if not isinstance(content, (bytes, bytearray)):
                raise CadQueryContractError(f"asset must be bytes: {name}")
            size = len(content)
            if size > self.limits.max_asset_bytes:
                raise CadQueryContractError(f"asset exceeds byte limit: {name}")
            total_asset_bytes += size
            if total_asset_bytes > self.limits.max_total_asset_bytes:
                raise CadQueryContractError("model assets exceed aggregate byte limit")
            total_input += size
            normalized_assets[name] = bytes(content)
        if total_input > self.limits.max_input_bytes:
            raise CadQueryContractError("aggregate sandbox input exceeds byte limit")
        with tempfile.TemporaryDirectory(prefix="printforge-cadquery-", dir="/tmp") as tempdir:
            parent = Path(tempdir)
            scratch = parent / "work"
            scratch.mkdir(mode=0o700)
            (scratch / "assets").mkdir(mode=0o700)
            output_dir = scratch / "output"
            output_dir.mkdir(mode=0o700)
            output_identity = os.lstat(output_dir)
            (scratch / "tmp").mkdir(mode=0o700)
            (scratch / "model.py").write_text(source, encoding="utf-8")
            (scratch / "worker.py").write_text(self.worker_source, encoding="utf-8")
            (scratch / "request.json").write_bytes(_canonical_json({
                "contract_version": CONTRACT_VERSION,
                "model_path": "/work/model.py",
                "parameters": dict(parameters),
                "assets": {name: f"/work/assets/{name}" for name in normalized_assets},
                "output_dir": "/work/output",
            }))
            for name, content in normalized_assets.items():
                (scratch / "assets" / name).write_bytes(content)
            try:
                if self.runner is None:
                    completed = self._run_bounded(self.command(scratch), cwd=parent, work=scratch)
                else:
                    completed = self.runner(
                        self.command(scratch),
                        cwd=scratch,
                        text=True,
                        capture_output=False,
                        timeout=self.limits.timeout_seconds,
                        check=False,
                    )
            except subprocess.TimeoutExpired as exc:
                raise CadQuerySandboxError(
                    f"CadQuery sandbox exceeded {self.limits.timeout_seconds}s wall-clock limit"
                ) from exc
            if completed.returncode != 0:
                raise CadQuerySandboxError(
                    f"CadQuery sandbox failed with exit {completed.returncode}: {completed.stderr[-1000:]}"
                )
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            if len(stdout.encode("utf-8")) > self.limits.max_log_bytes:
                raise CadQuerySandboxError("CadQuery sandbox stdout exceeded byte limit")
            if len(stderr.encode("utf-8")) > self.limits.max_log_bytes:
                raise CadQuerySandboxError("CadQuery sandbox stderr exceeded byte limit")
            self._tree_usage(
                scratch,
                max_bytes=self.limits.max_scratch_bytes,
                max_entries=self.limits.max_scratch_files,
            )
            result_path = scratch / "result.json"
            try:
                result_metadata = os.lstat(result_path)
            except FileNotFoundError as exc:
                raise CadQuerySandboxError("CadQuery worker did not create result.json") from exc
            if (
                stat.S_ISLNK(result_metadata.st_mode)
                or not stat.S_ISREG(result_metadata.st_mode)
                or result_metadata.st_nlink != 1
            ):
                raise CadQuerySandboxError("CadQuery worker result.json is not an independent regular file")
            if result_metadata.st_size > self.limits.max_report_bytes:
                raise CadQuerySandboxError("CadQuery worker result.json exceeds byte limit")
            try:
                report_raw = result_path.read_bytes()
                if len(report_raw) > self.limits.max_report_bytes:
                    raise CadQuerySandboxError("CadQuery worker result.json exceeds byte limit")
                report = json.loads(
                    report_raw.decode("utf-8"),
                    parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise CadQuerySandboxError("CadQuery worker returned invalid result.json") from exc
            names = report.get("artifacts") if isinstance(report, dict) else None
            if not isinstance(names, list):
                raise CadQuerySandboxError("CadQuery worker did not declare its artifacts")
            if len(names) > self.limits.max_artifact_count:
                raise CadQuerySandboxError("CadQuery worker declared too many artifacts")
            if any(not isinstance(name, str) for name in names) or len(set(names)) != len(names):
                raise CadQuerySandboxError("CadQuery worker declared invalid or duplicate artifacts")
            if any(name in RESERVED_ARTIFACT_NAMES for name in names):
                raise CadQuerySandboxError("CadQuery worker declared a reserved artifact name")
            try:
                current_output = os.lstat(output_dir)
            except OSError as exc:
                raise CadQuerySandboxError("CadQuery worker replaced its output directory") from exc
            if (
                stat.S_ISLNK(current_output.st_mode)
                or not stat.S_ISDIR(current_output.st_mode)
                or (current_output.st_dev, current_output.st_ino)
                != (output_identity.st_dev, output_identity.st_ino)
            ):
                raise CadQuerySandboxError("CadQuery worker replaced its output directory")
            output = output_dir.resolve(strict=True)
            try:
                output.relative_to(scratch.resolve(strict=True))
            except ValueError as exc:
                raise CadQuerySandboxError("CadQuery worker output directory escaped scratch") from exc
            artifact_bytes: dict[str, bytes] = {}
            artifact_paths: dict[str, Path] = {}
            artifact_identities: dict[str, tuple[int, int, int, int, int]] = {}
            total_artifact_bytes = 0
            for name in names:
                if not isinstance(name, str) or not ASSET_NAME_RE.fullmatch(name):
                    raise CadQuerySandboxError("CadQuery worker declared an invalid artifact name")
                unresolved = output / name
                try:
                    metadata = os.lstat(unresolved)
                except FileNotFoundError as exc:
                    raise CadQuerySandboxError(f"CadQuery artifact is missing: {name}") from exc
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise CadQuerySandboxError(f"CadQuery artifact is not an independent regular file: {name}")
                resolved_artifact = unresolved.resolve()
                try:
                    resolved_artifact.relative_to(output)
                except ValueError as exc:
                    raise CadQuerySandboxError("CadQuery artifact escaped output directory") from exc
                if metadata.st_size > self.limits.max_artifact_bytes:
                    raise CadQuerySandboxError(f"CadQuery artifact exceeds byte limit: {name}")
                total_artifact_bytes += metadata.st_size
                if total_artifact_bytes > self.limits.max_total_artifact_bytes:
                    raise CadQuerySandboxError("CadQuery artifacts exceed aggregate byte limit")
                artifact_paths[name] = unresolved
                artifact_identities[name] = (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            undeclared = {
                path.name for path in output.iterdir()
                if path.name not in artifact_paths
            }
            if undeclared:
                raise CadQuerySandboxError("CadQuery worker left undeclared output files")
            declared_parts = validate_parts(report.get("parts") if isinstance(report, dict) else None)
            with ExitStack() as open_artifacts:
                for name, path in artifact_paths.items():
                    try:
                        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                        handle = open_artifacts.enter_context(os.fdopen(descriptor, "rb"))
                        metadata = os.fstat(handle.fileno())
                        identity = (
                            metadata.st_dev,
                            metadata.st_ino,
                            metadata.st_size,
                            metadata.st_mtime_ns,
                            metadata.st_ctime_ns,
                        )
                        content = handle.read(self.limits.max_artifact_bytes + 1)
                    except OSError as exc:
                        raise CadQuerySandboxError(
                            f"CadQuery artifact changed before trusted validation: {name}"
                        ) from exc
                    if identity != artifact_identities[name] or len(content) != identity[2]:
                        raise CadQuerySandboxError(
                            f"CadQuery artifact changed before trusted validation: {name}"
                        )
                    artifact_bytes[name] = content
                if self.trusted_validator is None:
                    trusted_checks: dict[str, bool] = {}
                else:
                    raw_checks = self.trusted_validator(parts=declared_parts, artifact_paths=artifact_paths)
                    if not isinstance(raw_checks, Mapping):
                        raise CadQuerySandboxError("trusted artifact validator returned invalid checks")
                    trusted_checks = {
                        name: raw_checks.get(name) is True
                        for name in (*REQUIRED_CHECKS, *EXTRA_HARD_GATES)
                    }
                for name, path in artifact_paths.items():
                    try:
                        metadata = os.lstat(path)
                        unchanged = (
                            stat.S_ISREG(metadata.st_mode)
                            and not stat.S_ISLNK(metadata.st_mode)
                            and metadata.st_nlink == 1
                            and (
                                metadata.st_dev,
                                metadata.st_ino,
                                metadata.st_size,
                                metadata.st_mtime_ns,
                                metadata.st_ctime_ns,
                            )
                            == artifact_identities[name]
                            and path.read_bytes() == artifact_bytes[name]
                        )
                    except OSError:
                        unchanged = False
                    if not unchanged:
                        raise CadQuerySandboxError(
                            f"CadQuery artifact changed during trusted validation: {name}"
                        )
                return SandboxResult(
                    report={"parts": declared_parts, "checks": trusted_checks},
                    artifacts=artifact_bytes,
                    stdout=stdout,
                    stderr=stderr,
                    trusted_evidence=self.trusted_validator is not None,
                )


CHECK_FAILURE_CODES = {
    "brep_valid": "invalid_brep",
    "step_exported": "step_export_failed",
    "step_roundtrip_valid": "step_roundtrip_failed",
    "stl_tessellated": "stl_tessellation_failed",
    "mesh_checks_passed": "mesh_validation_failed",
}


@dataclass
class CadQueryPipeline:
    """Enforce contract and deterministic hard gates around an injected executor."""

    executor: CadQueryExecutor
    extra_hard_gates: tuple[str, ...] = EXTRA_HARD_GATES
    slicer: Any | None = None

    def evaluate(
        self,
        source: str,
        *,
        parameter_values: Mapping[str, Any] | None = None,
        assets: Mapping[str, bytes] | None = None,
    ) -> dict[str, Any]:
        contract = parse_model_contract(source)
        values = validate_parameter_values(contract["parameters"], parameter_values)
        result = self.executor.execute(source, values, assets or {})
        if result.trusted_evidence is not True:
            raise CadQuerySandboxError(
                "CadQuery output lacks independent trusted validation; candidate cannot be evaluated"
            )
        if not isinstance(result.report, dict):
            raise CadQuerySandboxError("CadQuery worker report must be a mapping")
        parts = validate_parts(result.report.get("parts"))
        checks = result.report.get("checks")
        if not isinstance(checks, dict):
            raise CadQueryContractError("worker report must contain deterministic checks")
        failure_codes = [code for key, code in CHECK_FAILURE_CODES.items() if checks.get(key) is not True]
        optional_gate_codes = {
            "build_volume_ok": "build_volume_overflow",
            "hard_locks_ok": "broken_hard_lock",
            "reference_roles_excluded": "reference_export_leakage",
        }
        for gate in self.extra_hard_gates:
            if checks.get(gate) is not True:
                failure_codes.append(optional_gate_codes[gate])
        expected_artifacts = {item[key] for item in parts for key in ("step_artifact", "stl_artifact")}
        reserved = sorted(RESERVED_ARTIFACT_NAMES & set(result.artifacts))
        if reserved:
            raise CadQuerySandboxError(
                f"CadQuery worker returned reserved artifacts: {', '.join(reserved)}"
            )
        missing = sorted(expected_artifacts - set(result.artifacts))
        empty = sorted(name for name, content in result.artifacts.items() if not content)
        if missing or empty:
            failure_codes.append("not_exportable")
        slicer_results: dict[str, Any] = {"status": "unavailable"}
        slicer_artifacts: dict[str, bytes] = {}
        if failure_codes:
            slicer_results = {
                "status": "blocked",
                "failure_codes": ["slice_blocked_by_geometry"],
                "failure_reason": "deterministic geometry gates failed before slicing",
            }
        elif self.slicer is None:
            failure_codes.append("slicer_unavailable")
            slicer_results = {
                "status": "failed",
                "failure_codes": ["slicer_unavailable"],
                "failure_reason": "required Bambu Studio slicer adapter is unavailable",
            }
        elif self.slicer is not None:
            positive_roles = {"printable", "assembly"}
            positive_parts = [part for part in parts if part["export_role"] in positive_roles]
            identity_transform = {
                "translation_mm": [0.0, 0.0, 0.0],
                "rotation_deg": [0.0, 0.0, 0.0],
            }
            if len(positive_parts) != 1 or positive_parts[0]["transform"] != identity_transform:
                failure_codes.append("slice_assembly_transform_unsupported")
                slicer_results = {
                    "status": "failed",
                    "failure_codes": ["slice_assembly_transform_unsupported"],
                    "failure_reason": (
                        "slicing requires one identity-transformed positive part until a "
                        "controlled transformed plate-input builder is implemented"
                    ),
                }
                positive_parts = []
            printable_stls = {
                part["stl_artifact"]: result.artifacts[part["stl_artifact"]]
                for part in positive_parts
                if part["stl_artifact"] in result.artifacts
            }
            if positive_parts:
                sliced = self.slicer.slice(printable_stls)
                slicer_results = dict(sliced.results)
                slicer_artifacts = dict(sliced.artifacts)
            collisions = sorted(set(slicer_artifacts) & (set(result.artifacts) | RESERVED_ARTIFACT_NAMES))
            if collisions:
                raise CadQuerySandboxError(
                    f"slicer returned reserved or duplicate artifacts: {', '.join(collisions)}"
                )
            if str(slicer_results.get("status") or "").casefold() != "complete":
                codes = slicer_results.get("failure_codes")
                failure_codes.extend(codes if isinstance(codes, list) and codes else ["slice_failed"])
        combined_artifacts = {**result.artifacts, **slicer_artifacts}
        role_by_artifact = {
            part[key]: part["export_role"]
            for part in parts
            for key in ("step_artifact", "stl_artifact")
        }
        if slicer_artifacts:
            role_by_artifact.update({
                name: (
                    "sliced-printable"
                    if name == slicer_results.get("sliced_3mf_artifact")
                    else "slicer-evidence"
                )
                for name in slicer_artifacts
            })
        artifact_records = [
            {
                "name": name,
                "sha256": _sha256(content),
                "size": len(content),
                "role": role_by_artifact.get(name, "metadata"),
            }
            for name, content in sorted(combined_artifacts.items())
        ]
        manifest = {
            "manifest_version": MANIFEST_VERSION,
            **contract,
            "parameters_used": values,
            "parts": parts,
            "checks": {key: bool(checks.get(key)) for key in (*REQUIRED_CHECKS, *self.extra_hard_gates)},
            "failure_codes": sorted(set(failure_codes)),
            "slicer_results": slicer_results,
            "slicer_profile_fingerprint": slicer_results.get("profile_fingerprint"),
            "artifacts": artifact_records,
        }
        artifact_id = _sha256(_canonical_json(manifest))
        manifest["artifact_id"] = artifact_id
        artifacts = {"model.py": source, **combined_artifacts, "model-manifest.json": _canonical_json(manifest)}
        return {
            "model_format": MODEL_FORMAT,
            "source": source,
            "parameters": contract["parameters"],
            "parameter_values": values,
            "parts": parts,
            "artifact_id": artifact_id,
            "manifest": manifest,
            "artifacts": artifacts,
            "failure_codes": sorted(set(failure_codes)),
            "hard_rejected": bool(failure_codes),
            "slicer_results": slicer_results,
            "slicer_profile_fingerprint": slicer_results.get("profile_fingerprint"),
            "promotion_blocked": bool(failure_codes),
            "bambuddy_send_blocked": bool(failure_codes),
            "runtime_evidence_source": "trusted-artifact-validator",
        }


def model_envelope(
    *,
    model_format: str,
    source: str | None,
    parameters: Mapping[str, Any] | None = None,
    parts: list[dict[str, Any]] | None = None,
    artifact_id: str | None = None,
) -> dict[str, Any]:
    """Return generic API fields, with SCAD aliases only for legacy artifacts."""

    envelope = {
        "model_format": model_format,
        "source": source,
        "source_available": bool(source),
        "parameters": dict(parameters or {}),
        "parts": list(parts or []),
        "artifact_id": (
            artifact_id
            if model_format == MODEL_FORMAT
            else artifact_id or (_sha256(source) if source else None)
        ),
    }
    if model_format == "openscad-legacy":
        envelope["scad"] = source
        envelope["params"] = envelope["parameters"]
    return envelope
