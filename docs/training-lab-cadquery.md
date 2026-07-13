# Training Lab CadQuery v1 Foundation

`cadquery-v1` is the source and artifact contract for new parametric Training
Lab models. This phase adds the contract and security boundary; it does **not**
install CadQuery or Bubblewrap, wire the existing generator to CadQuery, or
change production on port 8093. Existing `.scad` candidates remain read-only,
renderable `openscad-legacy` artifacts.

## Status and enablement

The capability flag is `PRINT_FORGE_CADQUERY_ENABLED`. It defaults to `false`.
`/training-lab/api/bootstrap` reports the source contract separately from the
runtime: `cadquery_v1_contract_supported=true` and
`cadquery_v1_runtime_ready=false`. Setting the flag only records
`cadquery_v1_requested=true`; it does not make a worker available or change
`cadquery_v1=false`.
A later host/dependency phase must install a pinned CadQuery runtime and
Bubblewrap, then inject a worker that implements the report described below.

This makes the current change persistent in code but operationally dormant. No
service restart, dependency install, NixOS rebuild, model generation, or real
CadQuery execution was performed while implementing it.

## `model.py` contract

Every source file defines one literal `PARAMETERS` mapping and one synchronous
entry point with the exact signature `build(params, assets)`:

```python
import cadquery as cq

PARAMETERS = {
    "width": {
        "type": "float",
        "default": 24.0,
        "min": 10.0,
        "max": 80.0,
        "step": 0.5,
        "unit": "mm",
    },
    "label": {"type": "str", "default": "PF"},
}

def build(params, assets):
    body = cq.Workplane("XY").box(params["width"], 12, 4)
    return {"body": body}
```

The parser in `evolution_lab/cadquery.py` uses Python's AST and
`ast.literal_eval`; it never imports or runs generated code to discover
parameters. Supported parameter types are `float`, `int`, `bool`, `str`, and
`choice`. Numeric bounds and choices are validated before execution. Source
bytes, AST node count/depth, parameter count, choice count, individual text
values, and aggregate parameter text all have explicit limits. NaN, infinity,
numbers that overflow a float, and values/transforms beyond the contract's
numeric ceiling are rejected.

The isolated worker must return at least one named part. Every part explicitly
declares:

- an `export_role`: `printable`, `assembly`, `reference`, `fit_cutout`, or
  `negative`;
- `translation_mm` and `rotation_deg` three-vectors;
- unique STEP and STL artifact names.

Only `printable` and `assembly` parts are positive printable output. A model
with no printable part is invalid.

## Sandbox boundary

`BubblewrapExecutor` copies only `model.py`, validated parameter values,
explicitly supplied assets, and an injected worker into a fresh directory under
`/tmp`. Its Bubblewrap profile:

- creates new user, process, IPC, UTS, cgroup, and network namespaces through
  `--unshare-all`;
- mounts no user home, repository, `library/`, or `uploads/` path;
- exposes only the fresh scratch directory as writable at `/work`;
- exposes only fixed allowlisted immutable runtime roots (`/usr`, `/lib`,
  `/lib64`, and `/nix/store`, when present) read-only; runtime executables and
  roots must be canonical non-symlink paths, so home, repository, Library, and
  upload roots cannot be added;
- clears the environment and sets `HOME=/nonexistent`;
- applies wall-clock plus strict source/worker/asset, result JSON, stdout,
  stderr, per-artifact, aggregate-artifact, scratch-byte, and scratch-file
  limits. Symlink, hard-linked, non-regular, missing, duplicate, undeclared,
  and path-escaping output artifacts are rejected before resolution.

CPU, memory, open-file, and process-count enforcement intentionally belongs to
the future dedicated locked worker service/cgroup. The dormant web-process
foundation does not use `preexec_fn`, host-UID `RLIMIT_NPROC`, or an assumed
`/usr/bin/python3`: those are unsafe or unreliable in the threaded desktop
service. A real runtime must provide a canonical pinned executable under the
allowlist and the service-level resource controls before
`cadquery_v1_runtime_ready` may become true.

The next evidence boundary is implemented in
`docs/training-lab-bambu-slicing.md`: only a candidate that passes every trusted
check here can enter the versioned Bambu CLI adapter. CadQuery and slicer
runtime readiness both remain false until their separate real-host smoke gates
are satisfied.

The worker protocol is intentionally injected. This repository does not claim a
particular CadQuery API call worked until the pinned real worker exists and an
opt-in host smoke test proves it.

## Deterministic hard gates and manifest

The untrusted worker may declare named parts and artifact filenames, but its
`checks` or pass/fail claims are ignored. After the sandbox exits, a separately
injected trusted parent-side validator receives the captured artifact file paths
and derives every check from the actual STEP/STL files. Missing trusted evidence
is a failure, not an implied pass:

1. B-rep is valid and solid.
2. STEP export succeeded.
3. Re-imported STEP is valid (round-trip proof).
4. STL tessellation succeeded.
5. Existing mesh checks passed.
6. Build volume is within the active printer profile.
7. Hard locks remain satisfied.
8. Reference/negative/cutout roles did not leak into positive exports.

Failures map to the existing hard-rejection scorer, so a neural score can never
override them. Successful executor output produces `model.py`, named STEP/STL
artifacts, and `model-manifest.json`. The manifest records contract versions,
source hash, parameters used, named parts, checks, artifact sizes/hashes, and a
content-derived `artifact_id`.

Candidate API responses use `model_format`, `source`, `parameters`, `parts`, and
`artifact_id`. Export-role metadata is retained on each STEP/STL record. The old
`scad` and `params` aliases are emitted only for `openscad-legacy` candidates.
If source is missing, the API returns `source=null` and
`source_available=false`; it never hashes an empty source. A CadQuery candidate
also never receives a source-only fallback ID—its `artifact_id` exists only
after the complete trusted manifest has been hashed.

Candidate selection is equally fail closed: `required_checks_passed` must be
exactly `true`, and top-level hard rejection, any failure code, or a rejected
score excludes the candidate before deterministic or future neural ranking.

## Verification

Run the CPU-only contract tests:

```sh
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --offline \
  --with fastapi --with httpx --with trimesh --with numpy --with scipy \
  --with python-multipart --with networkx --with lxml --with shapely \
  --with rtree --with manifold3d --with cascadio \
  python -m unittest tests.test_cadquery_phase1 -v
```

Expected result: all tests pass. These tests inject a fake executor and mock the
subprocess; they prove parser, manifest, hard-gate, and sandbox-command behavior,
not real CadQuery geometry or Bubblewrap availability.

## Rollback

Leave `PRINT_FORGE_CADQUERY_ENABLED` unset or set it to `false`. Since no active
generation adapter uses `cadquery-v1`, no candidate or production data needs a
migration. To remove the foundation entirely, revert the CadQuery module, API
envelope fields, tests, and docs; legacy OpenSCAD artifacts remain intact.
