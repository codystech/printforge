"""Atomic, filesystem-backed storage isolated from PrintForge's model library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable


ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ARTIFACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MUTATION_OUTCOME_MANIFEST = "recent.json"
MUTATION_OUTCOME_WINDOW = 1000


def safe_id(value: str, label: str = "id") -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def new_id(prefix: str) -> str:
    safe_id(prefix, "prefix")
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def utc_ts() -> float:
    return time.time()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


class EvolutionStore:
    """Small durable store with immutable artifact/checkpoint directories.

    All paths are server-derived after strict identifier validation.  Run writes
    use atomic replacement.  Event streams are append-only, locked, flushed and
    fsynced.  No method writes to ``library/`` or ``uploads/``.
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self._lock = threading.RLock()
        for directory in (
            self.root / "runs",
            self.root / "demo_runs",
            self.root / "memory",
            self.root / "physical",
            self.root / "calibrations",
            self.root / "benchmarks",
            self.root / "proposals",
            self.root / "datasets",
            self.root / "mutation_outcomes",
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def _inside_root(self, path: Path) -> Path:
        """Resolve a caller-supplied write path and keep it inside the lab store."""

        resolved = Path(path).expanduser().resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("store path must remain inside the evolution data root") from exc
        return resolved

    @staticmethod
    def _atomic_write(path: Path, data: bytes, *, exclusive: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if exclusive and path.exists():
            raise FileExistsError(path.name)
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if exclusive and path.exists():
                raise FileExistsError(path.name)
            os.replace(tmp, path)
            try:
                dfd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        finally:
            if tmp.exists():
                tmp.unlink()

    def write_json(self, path: Path, value: Any, *, exclusive: bool = False) -> None:
        path = self._inside_root(path)
        with self._lock:
            self._atomic_write(path, _json_bytes(value), exclusive=exclusive)

    @staticmethod
    def read_json(path: Path) -> Any:
        if not path.exists():
            raise FileNotFoundError(path.name)
        return json.loads(path.read_text(encoding="utf-8"))

    def run_dir(self, run_id: str, *, demo: bool = False) -> Path:
        safe_id(run_id, "run id")
        return self.root / ("demo_runs" if demo else "runs") / run_id

    def find_run_dir(self, run_id: str) -> tuple[Path, bool]:
        safe_id(run_id, "run id")
        normal = self.root / "runs" / run_id
        demo = self.root / "demo_runs" / run_id
        if normal.exists():
            return normal, False
        if demo.exists():
            return demo, True
        raise FileNotFoundError("run not found")

    def create_run(self, run: dict, baseline_artifacts: dict[str, bytes | str]) -> dict:
        run_id = safe_id(run["run_id"], "run id")
        demo = bool(run.get("demo"))
        rdir = self.run_dir(run_id, demo=demo)
        with self._lock:
            rdir.mkdir(parents=True, exist_ok=False)
            for sub in ("candidates", "checkpoints", "baseline"):
                (rdir / sub).mkdir()
            for name, content in baseline_artifacts.items():
                self.write_artifact(rdir / "baseline", name, content, immutable=True)
            self.write_json(rdir / "run.json", run, exclusive=True)
            self.append_event(run_id, "info", "run_created", "Run created", demo=demo)
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        rdir, _ = self.find_run_dir(run_id)
        return self.read_json(rdir / "run.json")

    def update_run(self, run_id: str, mutate: Callable[[dict], dict | None]) -> dict:
        rdir, _ = self.find_run_dir(run_id)
        with self._lock:
            run = self.read_json(rdir / "run.json")
            replacement = mutate(run)
            if replacement is not None:
                run = replacement
            run["updated_at"] = utc_ts()
            self.write_json(rdir / "run.json", run)
            return run

    def list_runs(self, *, include_demo: bool = True) -> list[dict]:
        records: list[dict] = []
        roots = [self.root / "runs"]
        if include_demo:
            roots.append(self.root / "demo_runs")
        for root in roots:
            for path in root.iterdir():
                manifest = path / "run.json"
                if manifest.exists():
                    try:
                        records.append(self.read_json(manifest))
                    except (OSError, ValueError, json.JSONDecodeError):
                        continue
        return sorted(records, key=lambda item: item.get("created_at", 0), reverse=True)

    @staticmethod
    def _artifact_name(name: str) -> str:
        if not isinstance(name, str) or not ARTIFACT_RE.fullmatch(name) or name in {".", ".."}:
            raise ValueError("invalid artifact name")
        return name

    def write_artifact(
        self, directory: Path, name: str, content: bytes | str, *, immutable: bool = False
    ) -> dict:
        name = self._artifact_name(name)
        directory = self._inside_root(directory)
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        path = directory / name
        with self._lock:
            self._atomic_write(path, raw, exclusive=immutable)
        return {"name": name, "sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}

    def candidate_dir(self, run_id: str, candidate_id: str) -> Path:
        rdir, _ = self.find_run_dir(run_id)
        safe_id(candidate_id, "candidate id")
        return rdir / "candidates" / candidate_id

    def create_candidate(self, run_id: str, candidate: dict) -> dict:
        candidate_id = safe_id(candidate["candidate_id"], "candidate id")
        cdir = self.candidate_dir(run_id, candidate_id)
        with self._lock:
            cdir.mkdir(parents=False, exist_ok=False)
            (cdir / "artifacts").mkdir()
            candidate.setdefault("artifacts", [])
            self.write_json(cdir / "candidate.json", candidate, exclusive=True)
        return candidate

    def get_candidate(self, run_id: str, candidate_id: str) -> dict:
        return self.read_json(self.candidate_dir(run_id, candidate_id) / "candidate.json")

    def update_candidate(self, run_id: str, candidate_id: str, mutate: Callable[[dict], dict | None]) -> dict:
        cdir = self.candidate_dir(run_id, candidate_id)
        with self._lock:
            candidate = self.read_json(cdir / "candidate.json")
            replacement = mutate(candidate)
            if replacement is not None:
                candidate = replacement
            candidate["updated_at"] = utc_ts()
            self.write_json(cdir / "candidate.json", candidate)
            return candidate

    def list_candidates(self, run_id: str) -> list[dict]:
        rdir, _ = self.find_run_dir(run_id)
        out = []
        for path in (rdir / "candidates").iterdir():
            manifest = path / "candidate.json"
            if manifest.exists():
                out.append(self.read_json(manifest))
        return sorted(out, key=lambda item: (item.get("generation", 0), item.get("variant_label", "")))

    def delete_candidate(self, run_id: str, candidate_id: str) -> None:
        """Delete one explicitly selected candidate and its isolated artifacts.

        Protection of baselines/current-best/parents belongs to the engine, which
        has the complete lineage context. The store only performs the validated,
        root-confined filesystem operation.
        """
        cdir = self.candidate_dir(run_id, candidate_id)
        with self._lock:
            if not cdir.is_dir() or cdir.is_symlink():
                raise FileNotFoundError("candidate not found")
            shutil.rmtree(cdir)

    def add_candidate_artifacts(
        self, run_id: str, candidate_id: str, artifacts: dict[str, bytes | str]
    ) -> list[dict]:
        cdir = self.candidate_dir(run_id, candidate_id)
        records = []
        for name, content in artifacts.items():
            records.append(self.write_artifact(cdir / "artifacts", name, content, immutable=True))

        def apply(candidate: dict) -> None:
            existing = {item["name"] for item in candidate.get("artifacts", [])}
            if existing.intersection(item["name"] for item in records):
                raise ValueError("artifact already recorded")
            candidate.setdefault("artifacts", []).extend(records)

        self.update_candidate(run_id, candidate_id, apply)
        return records

    def candidate_artifact(self, run_id: str, candidate_id: str, name: str) -> Path:
        path = self.candidate_dir(run_id, candidate_id) / "artifacts" / self._artifact_name(name)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("artifact not found")
        return path

    def create_checkpoint(self, run_id: str, candidate_id: str, checkpoint_type: str) -> dict:
        safe_id(checkpoint_type, "checkpoint type")
        candidate = self.get_candidate(run_id, candidate_id)
        rdir, _ = self.find_run_dir(run_id)
        checkpoint_id = new_id("checkpoint")
        cpdir = rdir / "checkpoints" / checkpoint_id
        source = self.candidate_dir(run_id, candidate_id) / "artifacts"
        with self._lock:
            cpdir.mkdir(parents=False, exist_ok=False)
            manifest_files = []
            for record in candidate.get("artifacts", []):
                name = self._artifact_name(record["name"])
                src = source / name
                if not src.is_file() or src.is_symlink():
                    continue
                raw = src.read_bytes()
                manifest_files.append(self.write_artifact(cpdir, name, raw, immutable=True))
            manifest = {
                "checkpoint_id": checkpoint_id,
                "checkpoint_type": checkpoint_type,
                "run_id": run_id,
                "candidate_id": candidate_id,
                "created_at": utc_ts(),
                "immutable": True,
                "candidate_sha256": hashlib.sha256(_json_bytes(candidate)).hexdigest(),
                "files": manifest_files,
                "score": candidate.get("score"),
                "lineage": {
                    "parent_candidate_id": candidate.get("parent_candidate_id"),
                    "current_best_parent_id": candidate.get("current_best_parent_id"),
                    "generation": candidate.get("generation"),
                    "variant_label": candidate.get("variant_label"),
                },
            }
            self.write_json(cpdir / "manifest.json", manifest, exclusive=True)
        return manifest

    def list_checkpoints(self, run_id: str) -> list[dict]:
        rdir, _ = self.find_run_dir(run_id)
        out = []
        for path in (rdir / "checkpoints").iterdir():
            manifest = path / "manifest.json"
            if manifest.exists():
                out.append(self.read_json(manifest))
        return sorted(out, key=lambda item: item.get("created_at", 0))

    def append_event(
        self,
        run_id: str,
        severity: str,
        event_type: str,
        message: str,
        *,
        candidate_id: str | None = None,
        generation: int | None = None,
        data: dict | None = None,
        demo: bool | None = None,
    ) -> dict:
        safe_id(run_id, "run id")
        if demo is None:
            rdir, demo = self.find_run_dir(run_id)
        else:
            rdir = self.run_dir(run_id, demo=demo)
        with self._lock:
            counter = rdir / "event-seq"
            seq = int(counter.read_text()) + 1 if counter.exists() else 1
            self._atomic_write(counter, f"{seq}\n".encode("ascii"))
            event = {
                "seq": seq,
                "timestamp": utc_ts(),
                "severity": severity,
                "event_type": event_type,
                "message": message[:2000],
                "candidate_id": candidate_id,
                "generation": generation,
                "data": data or {},
                "demo": bool(demo),
            }
            with (rdir / "events.jsonl").open("ab") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event

    def list_events(self, run_id: str, *, after: int = 0, limit: int = 1000) -> list[dict]:
        rdir, _ = self.find_run_dir(run_id)
        path = rdir / "events.jsonl"
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(event.get("seq", 0)) > after:
                out.append(event)
            if len(out) >= min(max(limit, 1), 5000):
                break
        return out

    def _collection_dir(self, collection: str) -> Path:
        allowed = {
            "memory",
            "physical",
            "calibrations",
            "benchmarks",
            "proposals",
            "datasets",
            "mutation_outcomes",
        }
        if collection not in allowed:
            raise ValueError("invalid collection")
        return self.root / collection

    def create_record(self, collection: str, record: dict, *, prefix: str) -> dict:
        rid = safe_id(record.get("id") or new_id(prefix))
        record = dict(record)
        record["id"] = rid
        record.setdefault("created_at", utc_ts())
        record.setdefault("updated_at", record["created_at"])
        self.write_json(self._collection_dir(collection) / f"{rid}.json", record, exclusive=True)
        return record

    def get_record(self, collection: str, record_id: str) -> dict:
        safe_id(record_id)
        return self.read_json(self._collection_dir(collection) / f"{record_id}.json")

    def update_record(self, collection: str, record_id: str, mutate: Callable[[dict], dict | None]) -> dict:
        path = self._collection_dir(collection) / f"{safe_id(record_id)}.json"
        with self._lock:
            record = self.read_json(path)
            replacement = mutate(record)
            if replacement is not None:
                record = replacement
            record["updated_at"] = utc_ts()
            self.write_json(path, record)
            return record

    def list_records(self, collection: str) -> list[dict]:
        def created_at(record: dict) -> float:
            try:
                value = float(record.get("created_at", 0) or 0)
            except (TypeError, ValueError):
                return 0
            return value if value == value else 0

        out = []
        for path in self._collection_dir(collection).glob("*.json"):
            try:
                record = self.read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                out.append(record)
        records = sorted(out, key=created_at, reverse=True)
        return records

    @staticmethod
    def _mutation_outcome_id(outcome: dict) -> str:
        identity = "\0".join((
            str(outcome.get("run_id") or ""),
            str(outcome.get("generation") if outcome.get("generation") is not None else ""),
            str(outcome.get("candidate_id") or ""),
        ))
        return f"mutation_outcome_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]}"

    def _mutation_manifest_ids(self) -> list[str]:
        path = self.root / "mutation_outcomes" / MUTATION_OUTCOME_MANIFEST
        try:
            manifest = self.read_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(manifest, dict) or not isinstance(manifest.get("ids"), list):
            return []
        ids = []
        for value in manifest["ids"][:MUTATION_OUTCOME_WINDOW]:
            if isinstance(value, str) and ID_RE.fullmatch(value):
                ids.append(value)
        return ids

    def _write_mutation_manifest(self, ids: list[str]) -> None:
        path = self.root / "mutation_outcomes" / MUTATION_OUTCOME_MANIFEST
        self.write_json(path, {
            "version": 1,
            "ids": ids[:MUTATION_OUTCOME_WINDOW],
            "updated_at": utc_ts(),
        })

    def create_mutation_outcome(self, outcome: dict) -> dict:
        """Persist one immutable, retry-idempotent lab-only mutation result."""

        outcome = dict(outcome)
        rid = self._mutation_outcome_id(outcome)
        outcome["id"] = rid
        outcome.setdefault("created_at", utc_ts())
        outcome.setdefault("updated_at", outcome["created_at"])
        path = self.root / "mutation_outcomes" / f"{rid}.json"
        with self._lock:
            if path.exists():
                existing = self.read_json(path)
                if not isinstance(existing, dict):
                    raise ValueError("existing mutation outcome is malformed")
                identity = ("run_id", "candidate_id", "generation")
                if any(existing.get(key) != outcome.get(key) for key in identity):
                    raise ValueError("deterministic mutation outcome ID collision")
                persisted = existing
            else:
                self.write_json(path, outcome, exclusive=True)
                persisted = outcome
            ids = [rid] + [item for item in self._mutation_manifest_ids() if item != rid]
            self._write_mutation_manifest(ids)
            return persisted

    def list_mutation_outcomes(
        self,
        adaptive_scope: dict | None = None,
        *,
        limit: int = 200,
    ) -> list[dict]:
        """Return a manifest-bounded recent window of lab-only mutation results.

        Passing a scope uses exact matching and intentionally excludes legacy
        unscoped rows. Stale, corrupt, or malformed manifest entries fail closed;
        this path never scans the lifetime outcome directory.
        """

        bounded_limit = min(max(int(limit), 0), 1000)
        records = []
        for rid in self._mutation_manifest_ids():
            path = self.root / "mutation_outcomes" / f"{rid}.json"
            try:
                record = self.read_json(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(record, dict) or record.get("id") != rid:
                continue
            if adaptive_scope is not None and record.get("adaptive_scope") != adaptive_scope:
                continue
            records.append(record)
            if len(records) >= bounded_limit:
                break
        return records

    def write_dataset_file(self, export_id: str, name: str, content: bytes) -> Path:
        safe_id(export_id, "export id")
        directory = self.root / "datasets" / export_id
        directory.mkdir(parents=True, exist_ok=True)
        self.write_artifact(directory, name, content, immutable=True)
        return directory / self._artifact_name(name)

    def dataset_file(self, export_id: str, name: str) -> Path:
        path = self.root / "datasets" / safe_id(export_id) / self._artifact_name(name)
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError("dataset file not found")
        return path

    def reset_for_selfcheck(self) -> None:
        """Test-only cleanup; never called by the application router."""

        shutil.rmtree(self.root, ignore_errors=True)
