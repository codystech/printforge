"""Fail-closed Bambu Studio slicing for isolated Training Lab candidates.

The adapter intentionally uses only flags documented by Bambu Lab's command-line
manual.  It accepts immutable full machine/process/filament JSON snapshots and
never reads Bambu Studio's mutable user profile directories.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import zipfile
from xml.etree import ElementTree
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping


ADAPTER_VERSION = "printforge-bambu-studio-cli-v1"
PROFILE_VERSION = "printforge-bambu-profile-bundle-v1"
SLICE_ARTIFACT = "bambu-sliced.3mf"
LOG_ARTIFACT = "bambu-slicer.log"
PROFILE_NAMES = ("machine.json", "process.json", "filament.json")
MAX_PROFILE_BYTES = 4 * 1024 * 1024
MAX_STL_BYTES = 200 * 1024 * 1024
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_3MF_BYTES = 500 * 1024 * 1024
MAX_ZIP_MEMBERS = 512
MAX_ZIP_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_SCRATCH_BYTES = 768 * 1024 * 1024
MAX_SCRATCH_FILES = 1024
MAX_MEMORY_BYTES = 8 * 1024 * 1024 * 1024


class SlicerError(ValueError):
    """Profile, sandbox, or artifact validation failure."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return None
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"
    except OSError:
        return None


def _trusted_executable(value: str, label: str) -> Path:
    """Resolve one root-owned, immutable executable below a trusted runtime root."""

    path = Path(value)
    if not path.is_absolute():
        raise SlicerError(f"{label} must be an explicit absolute path")
    try:
        resolved = path.resolve(strict=True)
        metadata = os.stat(resolved, follow_symlinks=False)
    except OSError as exc:
        raise SlicerError(f"{label} is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0:
        raise SlicerError(f"{label} must be a root-owned regular file")
    if metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise SlicerError(f"{label} must not be group- or world-writable")
    roots = []
    for value in ("/nix/store", "/usr", "/bin", "/run/current-system"):
        root = Path(value)
        try:
            roots.append(root.resolve(strict=True))
        except OSError:
            continue
    if not any(resolved == root or root in resolved.parents for root in roots):
        raise SlicerError(f"{label} is outside trusted runtime roots")
    return resolved


def _tree_usage(root: Path) -> tuple[int, int]:
    total = 0
    entries = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > MAX_SCRATCH_FILES:
                    raise SlicerError("slicer scratch file-count limit exceeded")
                metadata = child.stat(follow_symlinks=False)
                total += metadata.st_size
                if total > MAX_SCRATCH_BYTES:
                    raise SlicerError("slicer scratch byte limit exceeded")
                if child.is_dir(follow_symlinks=False):
                    pending.append(Path(child.path))
    return total, entries


def _limit_child() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_3MF_BYTES, MAX_3MF_BYTES))
    resource.setrlimit(resource.RLIMIT_AS, (MAX_MEMORY_BYTES, MAX_MEMORY_BYTES))


def _validated_profile(content: bytes, name: str, expected_type: str) -> bytes:
    if not isinstance(content, bytes) or not content or len(content) > MAX_PROFILE_BYTES:
        raise SlicerError(f"{name} must be a non-empty bounded JSON profile")
    try:
        parsed = json.loads(content.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise SlicerError(f"{name} is not valid JSON") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise SlicerError(f"{name} must contain a full JSON profile object")
    if parsed.get("type") != expected_type or str(parsed.get("instantiation", "")).casefold() != "true":
        raise SlicerError(f"{name} must be a full instantiated {expected_type} profile")
    if not str(parsed.get("name") or "").strip() or not str(parsed.get("from") or "").strip():
        raise SlicerError(f"{name} is missing full-profile identity fields")
    return content


@dataclass(frozen=True)
class BambuProfileBundle:
    """Byte-immutable full Bambu machine/process/filament profile snapshots."""

    machine: bytes
    process: bytes
    filament: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "machine", _validated_profile(self.machine, "machine profile", "machine"))
        object.__setattr__(self, "process", _validated_profile(self.process, "process profile", "process"))
        object.__setattr__(self, "filament", _validated_profile(self.filament, "filament profile", "filament"))

    @classmethod
    def from_paths(cls, machine: Path, process: Path, filament: Path) -> "BambuProfileBundle":
        contents = []
        for path, label in zip((machine, process, filament), PROFILE_NAMES):
            path = Path(path)
            try:
                metadata = os.lstat(path)
            except OSError as exc:
                raise SlicerError(f"{label} is unavailable") from exc
            if not path.is_file() or path.is_symlink() or metadata.st_nlink != 1:
                raise SlicerError(f"{label} must be an independent regular file")
            contents.append(path.read_bytes())
        return cls(*contents)

    @property
    def fingerprint(self) -> str:
        manifest = {
            "profile_version": PROFILE_VERSION,
            "adapter_version": ADAPTER_VERSION,
            "machine_sha256": _sha256(self.machine),
            "process_sha256": _sha256(self.process),
            "filament_sha256": _sha256(self.filament),
        }
        return f"sha256:{_sha256(_canonical(manifest))}"

    def manifest(self) -> dict[str, str]:
        return {
            "profile_version": PROFILE_VERSION,
            "machine_sha256": f"sha256:{_sha256(self.machine)}",
            "process_sha256": f"sha256:{_sha256(self.process)}",
            "filament_sha256": f"sha256:{_sha256(self.filament)}",
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class SliceResult:
    results: dict
    artifacts: dict[str, bytes]


@dataclass(frozen=True)
class BambuBinaryIdentity:
    """Pinned identity captured by host packaging/readiness, never guessed by a job."""

    version: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise SlicerError("Bambu Studio version identity is required")
        normalized = self.sha256.removeprefix("sha256:").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", normalized):
            raise SlicerError("Bambu Studio binary SHA-256 identity is required")
        object.__setattr__(self, "sha256", f"sha256:{normalized}")


def _duration_seconds(value: str) -> int | None:
    matches = re.findall(r"(?i)(\d+(?:\.\d+)?)\s*([dhms])", value)
    if not matches:
        return None
    scale = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return int(round(sum(float(number) * scale[unit.lower()] for number, unit in matches)))


def _metric_text(output: bytes, log: str) -> str:
    chunks = [log]
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(output)) as archive:
            for name in archive.namelist():
                lowered = name.casefold()
                if lowered.endswith((".gcode", ".config", ".json", ".txt")):
                    info = archive.getinfo(name)
                    if info.file_size <= MAX_LOG_BYTES:
                        chunks.append(archive.read(name).decode("utf-8", errors="replace"))
    except (OSError, ValueError, zipfile.BadZipFile):
        pass
    return "\n".join(chunks)


def parse_slice_metrics(output: bytes, log: str) -> tuple[dict, list[str]]:
    """Extract auditable metrics from the exported sliced 3MF and CLI log."""

    text = _metric_text(output, log)
    time_match = re.search(r"(?im)(?:total estimated time|estimated print time|model printing time)\s*[:=]\s*([^\r\n;]+)", text)
    filament_match = re.search(r"(?im)(?:total filament used\s*\[g\]|filament(?:_used)?_grams)\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", text)
    layer_match = re.search(r"(?im)(?:total layer number|layer_count)\s*[:=]\s*(\d+)", text)
    support_match = re.search(r"(?im)(?:support_used|support material)\s*[:=]\s*(true|false|yes|no|0|1)", text)
    warnings = []
    for line in log.splitlines():
        if re.search(r"(?i)\bwarn(?:ing)?\b", line):
            normalized = line.strip()[:500]
            if normalized and normalized not in warnings:
                warnings.append(normalized)
    metrics = {
        "estimated_time_seconds": _duration_seconds(time_match.group(1)) if time_match else None,
        "filament_grams": float(filament_match.group(1)) if filament_match else None,
        "layer_count": int(layer_match.group(1)) if layer_match else None,
        "support_used": (
            support_match.group(1).casefold() in {"true", "yes", "1"}
            if support_match else None
        ),
    }
    return metrics, warnings


def validate_slice_archive(output: bytes) -> list[str]:
    """Validate bounded, nonempty model and plate payloads before accepting a slice."""

    failures: list[str] = []
    try:
        with zipfile.ZipFile(__import__("io").BytesIO(output)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ZIP_MEMBERS:
                return ["slice_output_oversized"]
            names = [info.filename for info in infos]
            if len(names) != len(set(names)):
                return ["slice_empty"]
            aggregate = 0
            for info in infos:
                parts = Path(info.filename).parts
                if info.filename.startswith(("/", "\\")) or ".." in parts:
                    return ["slice_empty"]
                aggregate += int(info.file_size)
                if aggregate > MAX_ZIP_UNCOMPRESSED_BYTES:
                    return ["slice_output_oversized"]
            model_info = next(
                (info for info in infos if info.filename.casefold() == "3d/3dmodel.model"), None
            )
            plates = [
                info for info in infos
                if "plate" in info.filename.casefold() and info.filename.casefold().endswith(".gcode")
            ]
            if model_info is None or model_info.file_size <= 0 or model_info.file_size > MAX_LOG_BYTES:
                failures.append("slice_empty")
            else:
                model_bytes = archive.read(model_info)
                try:
                    root = ElementTree.fromstring(model_bytes)
                    build_items = [
                        node for node in root.iter()
                        if node.tag.rsplit("}", 1)[-1] == "item"
                        and any(parent.tag.rsplit("}", 1)[-1] == "build" and node in list(parent) for parent in root.iter())
                    ]
                    if root.tag.rsplit("}", 1)[-1] != "model" or not build_items:
                        failures.append("slice_empty")
                except ElementTree.ParseError:
                    failures.append("slice_empty")
            if not plates or any(info.file_size <= 0 for info in plates):
                failures.append("slice_empty")
    except (OSError, ValueError, RuntimeError, zipfile.BadZipFile, zipfile.LargeZipFile):
        failures.append("slice_empty")
    return sorted(set(failures))


class BambuStudioCLIAdapter:
    """Slice STL bytes in a networkless Bubblewrap scratch directory."""

    def __init__(
        self,
        profiles: BambuProfileBundle,
        *,
        bambu_binary: str = "bambu-studio",
        bwrap_binary: str = "bwrap",
        binary_identity: BambuBinaryIdentity | None = None,
        bwrap_identity: BambuBinaryIdentity | None = None,
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
        timeout_seconds: int = 300,
    ) -> None:
        self.profiles = profiles
        self.bambu_binary = bambu_binary
        self.bwrap_binary = bwrap_binary
        self.binary_identity = binary_identity
        self.bwrap_identity = bwrap_identity
        self.runner = runner
        self.timeout_seconds = min(max(int(timeout_seconds), 10), 1800)

    def command(
        self,
        stl_names: list[str],
        *,
        bambu_binary: str | None = None,
        bwrap_binary: str | None = None,
    ) -> list[str]:
        # Bambu flags copied from the documented CLI manual:
        # https://github.com/bambulab/BambuStudio/wiki/Command-Line-Usage
        inner = [
            bambu_binary or self.bambu_binary,
            "--debug", "3",
            "--outputdir", "/work",
            "--arrange", "0",
            "--load-settings", "/work/profiles/machine.json;/work/profiles/process.json",
            "--load-filaments", "/work/profiles/filament.json",
            "--slice", "0",
            "--export-3mf", f"/work/{SLICE_ARTIFACT}",
            *[f"/work/input/{name}" for name in stl_names],
        ]
        return [
            bwrap_binary or self.bwrap_binary, "--die-with-parent", "--new-session", "--unshare-all",
            "--clearenv",
            "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
            "--ro-bind", "/nix/store", "/nix/store",
            "--ro-bind-try", "/run/current-system", "/run/current-system",
            "--bind", "__SCRATCH__", "/work", "--chdir", "/work",
            "--setenv", "HOME", "/tmp", "--setenv", "XDG_CONFIG_HOME", "/tmp/config",
            "--", *inner,
        ]

    def _resolved_runtime(self) -> tuple[Path, Path]:
        if self.binary_identity is None or self.bwrap_identity is None:
            raise SlicerError("Bambu Studio and Bubblewrap identities must both be pinned")
        bambu = _trusted_executable(self.bambu_binary, "bambu-studio")
        bwrap = _trusted_executable(self.bwrap_binary, "bwrap")
        if _file_sha256(bambu) != self.binary_identity.sha256:
            raise SlicerError("resolved Bambu Studio binary does not match the pinned checksum")
        if _file_sha256(bwrap) != self.bwrap_identity.sha256:
            raise SlicerError("resolved Bubblewrap binary does not match the pinned checksum")
        return bambu, bwrap

    def _run_process(self, command: list[str], scratch: Path) -> subprocess.CompletedProcess:
        stdout_path = scratch / ".slicer-stdout"
        stderr_path = scratch / ".slicer-stderr"
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            if self.runner is not None:
                completed = self.runner(
                    command, cwd=scratch, stdin=subprocess.DEVNULL,
                    stdout=stdout_file, stderr=stderr_file, text=False,
                    check=False, timeout=self.timeout_seconds,
                )
                extra_stdout = completed.stdout or b""
                extra_stderr = completed.stderr or b""
                if isinstance(extra_stdout, str):
                    extra_stdout = extra_stdout.encode()
                if isinstance(extra_stderr, str):
                    extra_stderr = extra_stderr.encode()
                if extra_stdout:
                    stdout_file.write(extra_stdout[:MAX_LOG_BYTES + 1])
                if extra_stderr:
                    stderr_file.write(extra_stderr[:MAX_LOG_BYTES + 1])
                stdout_file.flush()
                stderr_file.flush()
                returncode = completed.returncode
            else:
                process = subprocess.Popen(
                    command, cwd=scratch, stdin=subprocess.DEVNULL,
                    stdout=stdout_file, stderr=stderr_file,
                    start_new_session=True, preexec_fn=_limit_child,
                )
                started = time.monotonic()
                failure: str | None = None
                while process.poll() is None:
                    if time.monotonic() - started > self.timeout_seconds:
                        failure = f"slicer exceeded {self.timeout_seconds}s wall-clock limit"
                        break
                    try:
                        _tree_usage(scratch)
                    except (OSError, SlicerError) as exc:
                        failure = str(exc)
                        break
                    if stdout_path.stat().st_size > MAX_LOG_BYTES or stderr_path.stat().st_size > MAX_LOG_BYTES:
                        failure = "slicer log byte limit exceeded"
                        break
                    time.sleep(0.05)
                if failure:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    raise SlicerError(failure)
                returncode = process.wait()
        stdout = stdout_path.read_bytes()
        stderr = stderr_path.read_bytes()
        if len(stdout) > MAX_LOG_BYTES or len(stderr) > MAX_LOG_BYTES:
            raise SlicerError("slicer log byte limit exceeded")
        _tree_usage(scratch)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    def slice(self, stls: Mapping[str, bytes]) -> SliceResult:
        if self.binary_identity is None or self.bwrap_identity is None:
            return SliceResult({
                "status": "failed", "adapter_version": ADAPTER_VERSION,
                "profile_fingerprint": None,
                "failure_codes": ["slicer_binary_unpinned"],
                "failure_reason": "Bambu Studio and Bubblewrap identities were not both pinned",
            }, {})
        try:
            resolved_bambu, resolved_bwrap = self._resolved_runtime()
        except SlicerError as exc:
            code = "slicer_bwrap_untrusted" if "bwrap" in str(exc).casefold() or "bubblewrap" in str(exc).casefold() else "slicer_binary_mismatch"
            return SliceResult({
                "status": "failed", "adapter_version": ADAPTER_VERSION,
                "profile_fingerprint": self.evaluator_fingerprint,
                "failure_codes": [code],
                "failure_reason": str(exc),
            }, {})
        if not stls:
            return SliceResult({
                "status": "failed", "adapter_version": ADAPTER_VERSION,
                "profile_fingerprint": self.evaluator_fingerprint,
                "failure_codes": ["slice_no_printable_parts"],
                "failure_reason": "candidate has no printable STL parts",
            }, {})
        normalized: dict[str, bytes] = {}
        for name, content in stls.items():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\.stl", name, re.IGNORECASE):
                raise SlicerError(f"invalid STL artifact name: {name!r}")
            if not isinstance(content, bytes) or not content or len(content) > MAX_STL_BYTES:
                raise SlicerError(f"STL artifact is empty or exceeds its byte limit: {name}")
            normalized[name] = content

        with tempfile.TemporaryDirectory(prefix="printforge-bambu-slice-", dir="/tmp") as tempdir:
            scratch = Path(tempdir)
            inputs = scratch / "input"
            profiles = scratch / "profiles"
            inputs.mkdir(mode=0o700)
            profiles.mkdir(mode=0o700)
            for name, content in normalized.items():
                (inputs / name).write_bytes(content)
            for name, content in zip(PROFILE_NAMES, (self.profiles.machine, self.profiles.process, self.profiles.filament)):
                path = profiles / name
                path.write_bytes(content)
                path.chmod(0o400)
            command = [
                str(scratch) if item == "__SCRATCH__" else item
                for item in self.command(
                    sorted(normalized),
                    bambu_binary=str(resolved_bambu),
                    bwrap_binary=str(resolved_bwrap),
                )
            ]
            try:
                completed = self._run_process(command, scratch)
            except subprocess.TimeoutExpired:
                log = f"slicer exceeded {self.timeout_seconds}s wall-clock limit\n".encode()
                return SliceResult({
                    "status": "failed", "adapter_version": ADAPTER_VERSION,
                    "profile_fingerprint": self.evaluator_fingerprint,
                    "profile_manifest": self.profiles.manifest(),
                    "failure_codes": ["slice_failed"], "failure_reason": log.decode().strip(),
                }, {LOG_ARTIFACT: log})
            except (OSError, SlicerError) as exc:
                log = (f"slicer execution failed: {exc}\n").encode()[:MAX_LOG_BYTES]
                return SliceResult({
                    "status": "failed", "adapter_version": ADAPTER_VERSION,
                    "profile_fingerprint": self.evaluator_fingerprint,
                    "profile_manifest": self.profiles.manifest(),
                    "failure_codes": ["slice_failed"], "failure_reason": str(exc)[:1000],
                    "log_artifact": LOG_ARTIFACT,
                }, {LOG_ARTIFACT: log})
            stdout = completed.stdout or b""
            stderr = completed.stderr or b""
            if isinstance(stdout, str):
                stdout = stdout.encode()
            if isinstance(stderr, str):
                stderr = stderr.encode()
            log_bytes = (stdout + (b"\n" if stdout and stderr else b"") + stderr)[:MAX_LOG_BYTES]
            log = log_bytes.decode("utf-8", errors="replace")
            output_path = scratch / SLICE_ARTIFACT
            try:
                output_metadata = os.lstat(output_path)
                output_valid_file = bool(
                    stat.S_ISREG(output_metadata.st_mode)
                    and not stat.S_ISLNK(output_metadata.st_mode)
                    and output_metadata.st_nlink == 1
                    and 0 < output_metadata.st_size <= MAX_3MF_BYTES
                    and output_path.resolve(strict=True).parent == scratch.resolve(strict=True)
                )
            except OSError:
                output_valid_file = False
            output = output_path.read_bytes() if output_valid_file else b""
            artifacts = {LOG_ARTIFACT: log_bytes}
            failure_codes = []
            if completed.returncode != 0:
                failure_codes.append("slice_failed")
            if not log.strip():
                failure_codes.append("slice_log_empty")
            if not output:
                failure_codes.append(
                    "slice_output_oversized"
                    if 'output_metadata' in locals() and output_metadata.st_size > MAX_3MF_BYTES
                    else "slice_empty"
                )
            else:
                artifacts[SLICE_ARTIFACT] = output
                failure_codes.extend(validate_slice_archive(output))
            metrics, warnings = parse_slice_metrics(output, log)
            metric_types_valid = bool(
                isinstance(metrics["estimated_time_seconds"], int)
                and not isinstance(metrics["estimated_time_seconds"], bool)
                and metrics["estimated_time_seconds"] > 0
                and isinstance(metrics["filament_grams"], float)
                and metrics["filament_grams"] > 0
                and isinstance(metrics["layer_count"], int)
                and not isinstance(metrics["layer_count"], bool)
                and metrics["layer_count"] > 0
                and isinstance(metrics["support_used"], bool)
                and isinstance(warnings, list)
                and all(isinstance(item, str) and item for item in warnings)
            )
            if output and not metric_types_valid:
                failure_codes.append("slice_metrics_incomplete")
            failure_codes = sorted(set(failure_codes))
            return SliceResult({
                "status": "failed" if failure_codes else "complete",
                "adapter_version": ADAPTER_VERSION,
                "profile_fingerprint": self.evaluator_fingerprint,
                "profile_manifest": self.profiles.manifest(),
                **metrics,
                "warnings": warnings,
                "failure_codes": failure_codes,
                "failure_reason": "; ".join(failure_codes) if failure_codes else None,
                "sliced_3mf_artifact": SLICE_ARTIFACT if output else None,
                "log_artifact": LOG_ARTIFACT,
            }, artifacts)

    @property
    def evaluator_fingerprint(self) -> str:
        if self.binary_identity is None or self.bwrap_identity is None:
            raise SlicerError("Bambu Studio and Bubblewrap identities are unavailable")
        contract = {
            "adapter_version": ADAPTER_VERSION,
            "bambu_binary": {
                "path": self.bambu_binary,
                "version": self.binary_identity.version,
                "sha256": self.binary_identity.sha256,
            },
            "bubblewrap": {
                "path": self.bwrap_binary,
                "version": self.bwrap_identity.version,
                "sha256": self.bwrap_identity.sha256,
            },
            "profile_bundle": self.profiles.manifest(),
            "executed_argv_contract": self.command(["<input-stl>.stl"]),
            "resource_contract": {
                "timeout_seconds": self.timeout_seconds,
                "max_log_bytes": MAX_LOG_BYTES,
                "max_3mf_bytes": MAX_3MF_BYTES,
                "max_zip_members": MAX_ZIP_MEMBERS,
                "max_zip_uncompressed_bytes": MAX_ZIP_UNCOMPRESSED_BYTES,
                "max_scratch_bytes": MAX_SCRATCH_BYTES,
                "max_scratch_files": MAX_SCRATCH_FILES,
                "max_memory_bytes": MAX_MEMORY_BYTES,
            },
        }
        return f"sha256:{_sha256(_canonical(contract))}"


def runtime_readiness(
    *,
    profiles: BambuProfileBundle | None,
    smoke_evidence: Mapping[str, str] | None = None,
    bambu_binary: str = "bambu-studio",
    bwrap_binary: str = "bwrap",
    binary_identity: BambuBinaryIdentity | None = None,
    bwrap_identity: BambuBinaryIdentity | None = None,
) -> dict:
    """Read-only readiness: binaries + profiles + matching explicit smoke evidence."""

    bambu = shutil.which(bambu_binary) if not Path(bambu_binary).is_absolute() else bambu_binary
    bwrap = shutil.which(bwrap_binary) if not Path(bwrap_binary).is_absolute() else bwrap_binary
    readiness_error = None
    resolved_bambu = resolved_bwrap = None
    try:
        if bambu:
            resolved_bambu = _trusted_executable(str(bambu), "bambu-studio")
        if bwrap:
            resolved_bwrap = _trusted_executable(str(bwrap), "bwrap")
    except (OSError, SlicerError) as exc:
        readiness_error = str(exc)
    try:
        binary_checksum_matches = bool(
            resolved_bambu and binary_identity
            and _file_sha256(resolved_bambu) == binary_identity.sha256
        )
        bwrap_checksum_matches = bool(
            resolved_bwrap and bwrap_identity
            and _file_sha256(resolved_bwrap) == bwrap_identity.sha256
        )
    except OSError as exc:
        readiness_error = str(exc)
        binary_checksum_matches = False
        bwrap_checksum_matches = False
    evidence = dict(smoke_evidence or {})
    smoke_ok = bool(
        profiles
        and evidence.get("adapter_version") == ADAPTER_VERSION
        and binary_identity is not None
        and evidence.get("binary_sha256") == binary_identity.sha256
        and evidence.get("binary_version") == binary_identity.version
        and bwrap_identity is not None
        and evidence.get("bwrap_sha256") == bwrap_identity.sha256
        and evidence.get("bwrap_version") == bwrap_identity.version
        and evidence.get("profile_bundle_fingerprint") == profiles.fingerprint
        and evidence.get("status") == "passed"
    )
    ready = bool(
        resolved_bambu and resolved_bwrap and profiles and binary_identity and bwrap_identity
        and binary_checksum_matches and bwrap_checksum_matches and smoke_ok
    )
    return {
        "runtime_ready": ready,
        "bambu_studio_available": bool(bambu),
        "bubblewrap_available": bool(bwrap),
        "profiles_available": profiles is not None,
        "binary_identity_pinned": binary_identity is not None,
        "binary_checksum_matches": binary_checksum_matches,
        "bwrap_identity_pinned": bwrap_identity is not None,
        "bwrap_checksum_matches": bwrap_checksum_matches,
        "smoke_proven": smoke_ok,
        "adapter_version": ADAPTER_VERSION,
        "profile_bundle_fingerprint": profiles.fingerprint if profiles else None,
        "reason": readiness_error,
    }
