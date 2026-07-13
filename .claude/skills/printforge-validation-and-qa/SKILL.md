---
name: printforge-validation-and-qa
description: Decide what evidence is ENOUGH — acceptance thresholds, the evidence hierarchy, the golden/certified inventory, and test discipline. Use before claiming any PrintForge generation, refine, pipeline, prompts.py, or QA change "works"; when adding tests, validating prompt-contract edits, or interpreting "QA passed", "fixed xN", "skipped", "COLLISION", "TOUCHING", "TIGHT FIT", floating-region warnings, golden boat entries, or calibration coupon results. To RUN the measurements themselves, use printforge-diagnostics-and-tooling; to PROVE a disputed geometric claim, use printforge-proof-and-analysis-toolkit.
---

# PrintForge Validation and QA

Use this skill to decide what evidence is enough. Treat PrintForge validation as print-physics evidence first, visual evidence second, model judgment last.

Definitions:

- Deterministic geometry check: code-computed evidence from STL/mesh/SCAD, such as `floating_starts`, `lock_violations`, `/validate`, or `print_report`.
- Oblique render: a perspective render from an angled camera. It can reveal low relief and buried features better than a straight-on orthographic render.
- LLM/vision judgment: the `vision_qa` review performed by codex/gpt-5.5 from rendered images plus injected notes.
- Golden entry: a known library artifact used as a regression anchor, not a universal proof that unrelated changes work.

## When NOT to use this skill

Use sibling skills instead:

| Task | Use |
|---|---|
| Deploying, restarting, modifying service behavior, changing validation thresholds, or bulk regenerating golden models | `printforge-change-control` |
| Debugging a failing command, endpoint, render, network, service, or environment issue | `printforge-debugging-playbook` |
| Understanding why an incident happened historically | `printforge-failure-archaeology` |
| Working on OpenSCAD recipes, base-mesh import, CSG, text orientation, or prompt law | `printforge-openscad-reference` |
| Working on trimesh, 3MF, STEP, slicing, collisions, or mesh math internals | `printforge-mesh-geometry-reference` |
| Finding scripts and measurement tools beyond this runbook | `printforge-diagnostics-and-tooling` |
| Rebuilding host/docker/organic dependencies | `printforge-build-and-env` |
| Operating the live system and API surface safely | `printforge-run-and-operate` |

## Evidence Hierarchy

Apply the highest applicable level. Do not downgrade proof because a lower level is easier.

| Rank | Evidence | Accept as proof for |
|---:|---|---|
| 1 | Deterministic geometry checks: `floating_starts`, `lock_violations`, boolean collision volumes, clearance checks, `print_report` numbers | Printability, locked-part integrity, part count, bed fit, watertightness, collisions, clearances |
| 2 | Oblique render inspection and mesh-diff closeups | Presence, placement, proportion, emboss/deboss visibility, low-relief features |
| 3 | LLM/vision judgment from `vision_qa` | Advisory review, possible missed intent, candidate fixes |
| 4 | "The code looks right" | Last resort only; not enough for a "works" claim |

Never use straight-on renders as evidence for relief or small feature presence. The ortho-relief incident proved straight-on orthographic images can make absent or malformed low relief look present. "QA passed" from the LLM is advisory, not proof.

## Acceptance Thresholds

Use calibrated thresholds as gates. Route changes to these thresholds through `printforge-change-control`.

| Check | Source | Threshold | Action |
|---|---|---:|---|
| Floating starts | `parts.py:floating_starts` | Reported findings are already filtered to area `>= 4.0mm2`; sub-`4.0mm2` islands are skipped as spot-support territory | Seat every reported `>4mm2` finding into the surface or add a self-supporting underside. Treat sub-`4mm2` as support/reviewer judgment unless another check proves a failure. |
| Disconnected parts | `print_report()["parts"]`, `split_parts` | More parts than intended | Hard block when a model should be one connected print. Fix before claiming printable. |
| Pairwise collision | `/validate` | `>0.5mm3` becomes `COLLISION` | Flag and investigate. Known open false positive: snap-fits can legitimately interfere; one live snap-fit example was about `12mm3` (operator-reported, not re-measured), so do not auto-reject intentional joints solely on this number. |
| Touching fit | `/validate` | `<0.15mm` becomes `TOUCHING` | Reviewer decision: intentional joint or fused mistake. |
| Tight clearance | `/validate` | `<0.4mm` becomes `TIGHT FIT` | Warn; below `0.4mm` risks fused prints. |
| Base-surface removal | `mesh_changes` | Removed triangles `>2%` of base surface | Reviewer judgment, not auto-block. Creative cuts, engraving, and reshapes are legitimate when requested. |
| Fit tolerance | `GET /calibration` coupon | Coupon labels `[0.1, 0.15, 0.2, 0.3, 0.4]` | Use physical coupon results as fit-tolerance ground truth for that printer/material. |
| CadQuery candidate | `evolution_lab/cadquery.py` | Every trusted parent-derived B-rep, STEP export, STEP round-trip, STL tessellation, existing mesh, build-volume, hard-lock and reference-role check must be exactly `true` | Generated worker claims are ignored. Any missing/false trusted evidence is a hard rejection. Mocked CPU tests prove orchestration only; a real runtime claim requires an opt-in host smoke test plus dedicated worker resource controls. |
| Bambu slice evidence | `evolution_lab/slicer.py` | Pinned adapter/binary/profile fingerprint, zero exit, non-empty valid sliced 3MF with plate payload, captured log, estimated time, filament grams, layer count, and explicit support usage | Missing output or metrics, unpinned identity, timeout, non-zero exit, invalid 3MF, or geometry-gate blockage hard-rejects the candidate. Mocked fixtures prove orchestration only; runtime readiness needs a matching real smoke. |
| Dataset v2 example | `evolution_lab/dataset_v2.py` | Store-hashed approval/reviewer/timezone/source/revision/license-rights audit for every included source, run-derived family split, immutable source hash, deterministic and slicer evidence present and passed, and matching evaluator/slicer fingerprints | Exclude demos, failed/cancelled and hard-rejected candidates. Rendering alone is never SFT evidence; an exact verified physical failure permanently blocks that candidate/artifact from SFT, and a repair needs a new source/artifact identity. Physical rows and preference authority are trusted only after the deterministic printable tuple and candidate/mutation/memory backlinks all verify; decisive opposite physical evidence vetoes a preference row. |

Origins verified in code: `floating_starts(report_area=4.0)` in `parts.py:67`; collision `>0.5`, touching `<0.15`, tight fit `<0.4` in `app.py:1472-1480`; base removal `>0.02` in `app.py:349`; profile defaults include `snap_clearance: 0.15` and `loose_clearance: 0.4` in `app.py:223-224`; calibration clearances are in `app.py:894-926`. Ownership: the floating_starts algorithm/thresholds are canonical in printforge-mesh-geometry-reference; clearance constants in printforge-config-and-flags. On any disagreement, those skills (and the cited code lines) win.

Slider caveat: `PARAM_RE` at `app.py:46-48` matches numeric defaults with `[\d.]+`, so negative numeric defaults do not become sliders. Treat a negative default slider claim as unvalidated until `parse_params` output proves it.

## Golden and Certified Inventory

As of 2026-07-06, these are anchors, not full coverage:

| Anchor | Evidence | What it certifies |
|---|---|---|
| Library `27348cced127` | `meta.name`: `boat SHIP-IT: one piece, pennant up, print w/ tree supports`; `qa` key is absent; `rating: 1`; rules include `minimum wall thickness 2mm` and `the pennant flag always says Cody` | Final accepted boat lineage with cabin removed, mast/pennant, robot/wheel, chest, bow notch, raised CODY text, raised pennant, and bow support strut preserved as intent. |
| Library `3e7accab949c` | `meta.name`: `boat FIXED: flag+chest restored, one piece`; `qa` key is absent; `prompt`: deterministic fix removed broken footprint clip that collapsed features into slivers; `rating: 1` | Deterministic recovery from the base-mesh z-scale footprint failure; a one-piece Bambu-ready boat before the later ship-it support pass. |
| Junction-box 4-part benchmark | Historic record: 4 parts, base/lid/2 slit gaskets, produced in 2 prompts | Candidate/historic multi-part pipeline benchmark. Not re-run here; re-verify before using as a current pass/fail gate. |
| Bambuddy archive `#51` physical print | Historic record: custom Benchy boat, `8.58g`, `99.9%` print-time accuracy | Closed-loop physical anchor that the pipeline produced a real printed object. Not queried here; re-verify against Bambuddy before citing as current archive state. |
| Calibration coupon | `GET /calibration` and `CALIBRATION_SCAD` | Deterministic fit-tolerance ground truth for the owner's actual printer/material after physical testing. |

Library hygiene: validation generations must use a `test: ` name prefix or be deleted after review. Never destructively validate against another user's real library entries. Do not modify `library/`, `uploads/`, `.env`, `presets.txt`, `profiles.json`, or `static/` during validation.

## Test Discipline

The core geometry self-check remains the assert-based `parts.py` `__main__`
check. The experimental Training Lab also has isolated `unittest` coverage under
`tests/`; those tests use temporary stores and fake generation/evaluation
adapters, never model quota, `library/`, or `uploads/`.

Run it from the repo:

```sh
cd /home/cody/projects/printforge
uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python parts.py
```

Expected output:

```text
parts.py self-check OK
```

Run the Training Lab contract suite with the existing dependency environment:

```sh
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --offline \
  --with fastapi --with httpx --with trimesh --with numpy --with scipy \
  --with python-multipart --with networkx --with lxml --with shapely \
  --with rtree --with manifold3d --with cascadio \
  python -m unittest discover -s tests -v
```

Sandbox note from this authoring pass: the exact command first failed here because `uv` tried to initialize `/home/cody/.cache/uv` on a read-only filesystem. The same self-check passed with `UV_CACHE_DIR=/tmp/printforge-uv-cache` prepended. If your environment is writable, use the plain command above; if it fails with `Failed to initialize cache`, re-run with:

```sh
cd /home/cody/projects/printforge
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python parts.py
```

House style for new tests:

- Prefer small assert-based `if __name__ == "__main__":` self-checks next to the code under test.
- Use a single `test_*.py` only when a standalone file is clearer.
- Do not introduce frameworks, fixtures, or CI unless the owner routes the behavior change through `printforge-change-control`.
- Keep test artifacts in tempdirs or `/tmp`, never in `library/` or `uploads/`.

## Validate a `prompts.py` Change

Prompt rules are executable law. A bad recipe can poison every future generation.

Checklist:

- Read the exact rule being changed. The base-mesh fusion rules are rule `14`, with `14b` banning `scale([1,1,big]) import(...)` footprint clipping and `14f` carrying the verified vertical-side raised-text recipe.
- Regenerate only the minimum reference prompts needed to exercise the changed rule.
- Get owner sign-off before bulk regeneration: generations burn metered codex quota and often take 5-10 minutes each.
- Compare before/after `print_report` numbers: bounding box, parts count, watertightness, weight estimate, bed fit, and measured mode.
- Compare `floating_starts` counts and coordinates.
- Inspect oblique renders and mesh-diff closeups, never only straight-on views.
- Preserve accepted intent and rules from parent metadata.
- Route behavior-changing prompt-law edits through `printforge-change-control`.

Do not claim a prompt change works because the generated SCAD looks plausible.

## How In-App QA Works

The in-app QA loop is useful but advisory.

| Code path | Behavior |
|---|---|
| `QA_CHECK` and `QA_ROUNDS` | `app.py:28-29` enables codex-only vision self-check and defaults to 2 look-fix-rerender iterations. |
| `/generate` setup | `app.py:987-1079` renders/refines SCAD, injects profile, presets, mesh notes, design history, rules, part state, and known floating findings. Refines use codex edit-in-place, not full-file rewrite. |
| Lock verification | `app.py:1043-1054` runs `lock_violations`; if locked modules changed, one correction round attempts to restore them, then remaining violations are returned. Trust this diff over an LLM promise. |
| QA base choice | `app.py:1081-1091` diffs refines against the previous accepted state when possible, not the pristine upload. This prevents QA from reverting intentional changes. |
| QA loop | `app.py:1093-1109` calls `vision_qa` up to `QA_ROUNDS`. Status becomes `fixed xN`, `passed`, or `skipped`. |
| Metadata and response | `app.py:1125-1146` stores `qa`, `backend`, `report`, profile snapshot, lock violations, print warning count, and warning details. |

Inside `vision_qa`:

- It writes render requests from the current SCAD/STL context (`app.py:356-362`).
- It injects design history and project rules into the review (`app.py:365-368`).
- It computes mesh-diff closeups before wide views when a base STL exists (`app.py:371-395`).
- It flags `>2%` base-surface removal as suspicious but asks whether the user requested the cut (`app.py:396-405`).
- It runs `floating_starts` and tells the fixer to embed features 2-3mm or make them self-supporting (`app.py:406-418`).
- It always includes oblique perspective review images; straight-on top views are fallback context, not relief proof (`app.py:419-425`).
- It returns `skipped` if no images render (`app.py:426-428`), `passed` if codex leaves SCAD unchanged (`app.py:429-435`), `passed` if codex's fix fails to render (`app.py:436-439`), and `fixed` only after an edited SCAD renders (`app.py:440`).

Interpret QA vocabulary:

| Status | Meaning | Proof level |
|---|---|---|
| `passed` | The LLM reviewer made no change, or its attempted change failed to render and the original shipped | Advisory only |
| `fixed xN` | One or more QA edits rendered and replaced the prior STL | Requires deterministic re-checks |
| `skipped` | QA disabled, non-codex backend, no images, or QA exception before any fix | No visual/LLM evidence |

## Deterministic Checks to Prefer

Use these commands and endpoints when applicable:

```sh
cd /home/cody/projects/printforge
rg -n "def floating_starts|def print_report|def lock_violations|def validate|QA_ROUNDS|CALIBRATION_SCAD" app.py parts.py
```

```sh
cd /home/cody/projects/printforge
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python parts.py
```

```sh
curl -fsS http://127.0.0.1:8093/calibration | sed -n '1,3p'
```

For `/validate`, use only when you already have SCAD and permission to POST to the service. In restricted validation sessions that forbid POST, read `app.py:1438-1481` and mark runtime validation "not executed here - re-verify".

## Provenance and maintenance

Re-verify drift-prone claims with one-liners:

- Evidence functions and QA constants: `cd /home/cody/projects/printforge && rg -n "QA_CHECK|QA_ROUNDS|def vision_qa|def lock_violations|def print_report|def validate|CALIBRATION_SCAD" app.py parts.py`
- Floating threshold and self-check: `cd /home/cody/projects/printforge && nl -ba parts.py | sed -n '67,120p'`
- `/validate` collision and clearance gates: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '1438,1481p'`
- `print_report` fields: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '197,215p'`
- QA status assignment: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '356,440p;1081,1110p;1125,1146p'`
- Prompt rule numbering and base-mesh recipes: `cd /home/cody/projects/printforge && nl -ba prompts.py | sed -n '44,80p'`
- Parameter regex negative-number caveat: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '44,49p'`
- Parts self-check: `cd /home/cody/projects/printforge && uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python parts.py`
- Golden metadata: `cd /home/cody/projects/printforge && sed -n '1p' library/27348cced127/meta.json && sed -n '1p' library/3e7accab949c/meta.json`
- Library count and `test: ` hygiene as of 2026-07-06: `cd /home/cody/projects/printforge && find library -mindepth 1 -maxdepth 1 -type d | wc -l && grep -o '"name"[[:space:]]*:[[:space:]]*"test: [^"]*"' library/*/meta.json | wc -l`
- Calibration endpoint runtime: `curl -fsS --max-time 20 http://127.0.0.1:8093/calibration | sed -n '1,3p'`
- Live feature flags if service access is available: `curl -fsS --max-time 5 http://127.0.0.1:8093/config`
- Historic Bambuddy archive `#51` claim: query Bambuddy archive directly with the owner's approved credentials; not re-verified here.
- Historic junction-box benchmark claim: locate the benchmark artifact or session note before using it as a current regression gate; not re-verified here.
