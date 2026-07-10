---
name: printforge-dependency-and-supply-chain-review
description: >
  Audit PrintForge's dependencies and build/supply-chain for known-vulnerable packages,
  unpinned versions, and risky build steps. Load when asked "are dependencies
  vulnerable", "CVE in a package", "audit dependencies", "supply chain", "is trimesh/
  lxml/pillow safe", "unpinned versions", "npx/nix supply chain", or "build-time risk".
  KEY FACTS: there is NO lockfile — deps are declared inline in run.sh (uv --with ...)
  and the Dockerfile (pip install ...) with no version pins, and the app also pulls
  runtime tools via `nix shell nixpkgs#...` and the codex CLI. So "which version is
  actually installed" must be resolved live, not read from a manifest. Grounded in
  run.sh, Dockerfile.
---

# PrintForge dependency & supply-chain review

Read `printforge-security-scope-and-rules` first. This skill answers "is a component
version vulnerable, and how did it get here". PrintForge has **no `requirements.txt`,
no lockfile, no `pyproject.toml`** — dependencies are declared inline and unpinned, so
you must resolve the *actually installed* version live.

Working directory: `~/projects/printforge`.

## Where dependencies come from (three channels)

1. **Host run (`run.sh`)** — `uv run --with fastapi --with uvicorn --with httpx --with
   trimesh --with numpy --with scipy --with python-multipart --with networkx --with lxml
   --with shapely --with rtree --with manifold3d --with cascadio`. **No versions pinned**
   → uv resolves latest compatible at run time.
2. **Docker (`Dockerfile`)** — `pip install --no-cache-dir fastapi uvicorn httpx trimesh
   numpy scipy python-multipart networkx lxml shapely rtree manifold3d cascadio` +
   `apt-get install openscad`. **No versions pinned**; base image `python:3.12-slim`.
3. **Runtime tools pulled on demand** — `nix shell nixpkgs#openscad-unstable` (`run.sh`),
   `nix shell nixpkgs#potrace` (`app.py:582`), `nix build nixpkgs#libglvnd` (`run.sh`),
   `magick` (imagemagick, from host), and the **`codex` CLI** (npm, gpt-5.5 backend).

## What to actually check

### Security-relevant parsers (highest priority — they eat attacker bytes)
`trimesh`, `lxml`, `cascadio`/OpenCascade, `manifold3d`, `shapely`, `Pillow`-via-`magick`
/imagemagick. These parse untrusted uploads (see `printforge-file-upload-download-review`)
so a parser CVE here is directly reachable. Resolve installed versions and check advisories:

```sh
# what uv/pip would actually install / has installed (read-only)
uv pip list 2>/dev/null | grep -iE 'trimesh|lxml|cascadio|manifold3d|shapely|numpy|fastapi|httpx|pillow'
pip show trimesh lxml cascadio manifold3d shapely 2>/dev/null | grep -E '^Name|^Version'
# audit against the advisory DB if the tool is available (do not add a new dep to run it)
pip-audit 2>/dev/null || echo "pip-audit not installed — resolve versions manually"
```

Map each installed version to known advisories (OSV/GHSA). Report a version-matched CVE
as a finding; a package with *no* advisory at that version is **not** a finding.

### Unpinned versions (hardening)
No lockfile means a build today and a build next month can pull different, possibly newly-
vulnerable versions with no review. Report **once** as a supply-chain hardening item
(recommend a lockfile: `uv lock` / `requirements.txt` with hashes). This is **Low/Medium**
— it's a process gap, not an exploitable bug.

### Supply-chain of the fetch-and-run tools
- **`codex` CLI (npm)** runs LLM output in a sandbox; it's a trusted local tool but note
  it's an npm package on the host — its own supply chain is out of PrintForge's control.
  Informational.
- **`nix shell nixpkgs#...`** pins to the flake registry's nixpkgs; content-addressed, so
  low supply-chain risk. Not a finding.
- **imagemagick (`magick`)** has a heavy CVE history (coder/delegate abuse). If bitmap
  tracing (`trace=true`) is used, confirm the installed version and whether a
  `policy.xml` restricts coders. Rate per the version.

## When NOT to use this skill

- The bug is in PrintForge's *own code* calling a parser → `printforge-input-and-injection-
  review` or `printforge-file-upload-download-review`. This skill is only for *the library
  version* being vulnerable.
- The bug is a leaked token / build secret → `printforge-secrets-and-config-review`.

## False-positive traps

- **"Package X is vulnerable"** without matching the *installed* version to a *specific*
  advisory → not a finding. Always resolve the live version first (there's no manifest to
  read it from).
- **"No lockfile = Critical."** It's a hardening gap (Low/Medium), not an exploit.
- **Dev/transitive-only advisories** that aren't reachable from PrintForge's code paths —
  downgrade; note reachability.
- **`nixpkgs#` tools** are content-addressed — don't report them as "unpinned supply-chain
  risk" the way you would an unpinned pip dep.

## Evidence checklist

- [ ] The installed version (from `uv pip list`/`pip show`, run live) — not a guessed one.
- [ ] The specific advisory (GHSA/CVE id) that applies to *that* version.
- [ ] Proof the vulnerable code path is reachable in PrintForge (which route/parser).
- [ ] Suggested fix: pin to the fixed version + add a lockfile; suggested regression:
      a CI `pip-audit`/`uv lock --check` gate.

## Severity guidance

- Version-matched RCE/parser CVE reachable via upload: **High**.
- Version-matched DoS CVE: **Medium**.
- Unpinned deps / no lockfile / no audit gate: **Low/Medium** hardening.
- `nixpkgs`/content-addressed tools: **Informational**.

## Reporting template

Use `printforge-finding-report-template`. Always include the exact installed version and
the advisory id — a dependency finding without both is incomplete.

## Provenance and maintenance

Supported by `run.sh` (`uv --with ...`, unpinned), `Dockerfile` (`pip install ...`,
unpinned; `apt-get install openscad`), `app.py:582` (`nix shell nixpkgs#potrace`), and the
absence of any lockfile/manifest.

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -n 'with ' run.sh | head; grep -n 'pip install' Dockerfile   # declared deps (unpinned)
ls requirements*.txt pyproject.toml uv.lock poetry.lock 2>/dev/null || echo "no lockfile"
uv pip list 2>/dev/null | grep -iE 'trimesh|lxml|cascadio|manifold3d|shapely'  # live versions
```

Remaining uncertainty:
- The **actual** installed versions depend on when `uv`/`pip` last resolved; always
  resolve live. The lists above are declarations, not installed versions.
- Whether `pip-audit` is available in the environment — if not, do the OSV lookup manually.
