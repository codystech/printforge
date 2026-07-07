---
name: printforge-diagnostics-and-tooling
description: RUN PrintForge's measurement scripts and get the NUMBERS — use when asked to measure, inspect, health-check, or numerically answer "is it printable?" / "what did the refine change?" via floating regions, watertightness, mesh diffs, library audits, /validate, /models/{id}/diff, /calibration, print_report fields, QA status, or the shipped scripts (inspect_stl, render_views, diff_meshes, healthcheck, lib_audit). To decide whether the numbers are ENOUGH evidence, use printforge-validation-and-qa; to PROVE a disputed render/LLM claim from first principles, use printforge-proof-and-analysis-toolkit.
---

# PrintForge Diagnostics And Tooling

Use measurement before visual judgment. PrintForge failures have repeatedly looked fine in a render while failing in the slicer or on the printer. A straight-on orthographic render can hide low relief, missing embossing, and buried additions. Use oblique perspective views plus mesh measurements.

Date volatile facts as of 2026-07-06. Do not change project behavior from this skill; route behavior changes through `printforge-change-control`.

## Safety first

- Work from the repo root: `cd /home/cody/projects/printforge`.
- Do not write into `library/`, `uploads/`, `.env`, `presets.txt`, `profiles.json`, or `static/`.
- Use `/tmp` for generated inspection output.
- Do not run `run.sh`, `uvicorn`, `docker`, `codex`, organic/GPU jobs, or service restarts from diagnostics work.
- Use GETs freely for read-only endpoint checks. Treat POST endpoints, including `/validate`, as live-service actions; do not call them unless the user explicitly asks and the applicable safety gate allows it.
- In restricted Codex sandboxes, prefix `UV_CACHE_DIR=/tmp/uv-cache` if `uv` cannot write `/home/cody/.cache/uv`.

Definitions:

- Watertight: every mesh edge belongs to exactly two faces. If false, volume and weight are unreliable, but slicers often still repair or print the mesh.
- Connected parts: disconnected mesh islands from `trimesh.split(only_watertight=False)`. More than one is a hard block for a one-piece print unless separate parts are intentional.
- Floating start: a cross-section island that appears in a higher slice with no supported island below it. This maps to Bambu-style "floating regions" failures.
- Buried feature: an addition placed inside an existing solid. `union()` erases the visible evidence, so the requested feature may not appear as an added mesh region.

## Script quick start

All scripts live under `.claude/skills/printforge-diagnostics-and-tooling/scripts/` and are read-only except `render_views.py`, which writes PNGs to the output directory you choose.

Use this dependency wrapper from the repo root:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/<script>.py ...
```

The `inspect_stl.py` script imports the repo's own `parts.py` by adding the repo root to `sys.path`; run it from the repo root or keep the script in its bundled path.

| Need | Tool | Command |
| --- | --- | --- |
| "Is this STL printable?" | `inspect_stl.py` | `UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/inspect_stl.py uploads/<id>.stl` |
| "Show me proper inspection renders" | `render_views.py` | `UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/render_views.py library/<id>/model.scad /tmp/printforge-views` |
| "Did the refine add/remove geometry?" | `diff_meshes.py` | `UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/diff_meshes.py /tmp/base.stl /tmp/new.stl` |
| "Is the live app healthy?" | `healthcheck.sh` | `.claude/skills/printforge-diagnostics-and-tooling/scripts/healthcheck.sh` |
| "What is in the library?" | `lib_audit.py` | `UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/lib_audit.py --limit 20` |

### `inspect_stl.py`

Reports bounding box, triangle and vertex counts, watertightness, volume and estimated solid PLA grams at density 1.24 g/cm3, connected parts, and `floating_starts()` findings from `parts.py`.

Observed smoke test:

```sh
cd /home/cody/projects/printforge
UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/inspect_stl.py uploads/1e00498f6854.stl
```

Excerpt:

```text
triangles:       225,706
bbox mm (XxYxZ): 60.0 x 31.0 x 48.0
watertight:      False   <-- volume/weight below are UNRELIABLE
connected parts: 300   <-- >1 means disconnected islands (HARD BLOCK unless intended)
floating_starts findings (>4mm^2 mid-air starts): 0
```

### `render_views.py`

Renders the oblique inspection set through `nix shell nixpkgs#openscad-unstable --command openscad`:

- `model_iso.png`
- `model_oblique_025.png`, perspective camera `0,0,0,70,0,25,340`
- `model_oblique_205.png`, perspective camera `0,0,0,70,0,205,340`
- `model_top_ortho.png`, top orthographic camera `0,0,0,0,0,0,340`

Use the top view only for footprint and layout. Do not verify embossing, engraving, raised text, or low relief with top or straight-on orthographic views. Relief verification requires the oblique perspective views because low height changes disappear in straight-on projection.

Observed smoke test:

```sh
cd /home/cody/projects/printforge
UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/render_views.py library/55bee2f75055/model.scad /tmp/printforge-views-test2
```

Excerpt:

```text
OK   iso          /tmp/printforge-views-test2/model_iso.png
OK   oblique_025  /tmp/printforge-views-test2/model_oblique_025.png
OK   oblique_205  /tmp/printforge-views-test2/model_oblique_205.png
OK   top_ortho    /tmp/printforge-views-test2/model_top_ortho.png
```

### `diff_meshes.py`

Compares triangle centroids rounded to 0.1mm, then clusters added and removed surface regions with the same 8mm voxel-grid idea used by `app.py` QA. Use it when a refine claims it added a robot, text, sail, port cutout, gasket, or other visible element.

Interpretation:

- Added region present: match every requested new element to a bbox.
- No added region: the element may be absent or buried inside another solid.
- Tiny scattered additions: inspect for scraps.
- Removed surface above 2 percent of base: acceptable only when the user requested cutting, engraving, hollowing, or creative reshaping.

Observed smoke test:

```sh
cd /home/cody/projects/printforge
UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/diff_meshes.py uploads/28ef1326bceb.stl uploads/a3d5c75ddc1d.stl
```

Excerpt:

```text
added centroids:   1,712,758 (100.0% of new)
removed centroids: 59,505 (100.0% of base)
added regions:
  1. 1,712,758 triangles; x 12.3..285.0, y -12.4..281.1, z -3.9..23.8
interpretation:
  match each requested added element to an added-region bbox; no region usually means the element is buried or absent.
```

### `healthcheck.sh`

Read-only live snapshot:

- `systemctl --user is-active printforge`
- GET `/config`
- GET `/models`, parse count and newest model with `uv run python -c`
- `pgrep -x codex` to detect a possible generation/edit process

This script was syntax-checked here, but not fully executed here because this authoring task forbids running `systemctl`. The GET and `pgrep` pieces were verified separately:

```text
GET /config -> {"bambuddy":true,"organic":true}
GET /models -> 53; newest 55bee2f75055 Planetary Spin Toy fixed x2 codex/gpt-5.5
pgrep -x codex -> absent
```

If `healthcheck.sh` reports a codex process, do not assume it is stuck. Long generations can outlive an HTTP client; poll `/models` for autosaved results.

### `lib_audit.py`

Lists library metadata newest-first with `id`, 40-character name, created date, QA status, backend, rating, parts from `meta.report.parts`, and missing metadata fields.

Observed smoke test:

```sh
cd /home/cody/projects/printforge
UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/lib_audit.py --limit 5
```

Excerpt:

```text
id           created     qa          backend              rating parts name
55bee2f75055 2026-07-06  fixed x2    codex/gpt-5.5               2     Planetary Spin Toy
f02d84478fb6 2026-07-06  fixed x2    codex/gpt-5.5        1      2     BongDock
```

## Interpretation guides

Use this triage table before asking the LLM to fix anything:

| Finding | Meaning | Action |
| --- | --- | --- |
| `watertight=false` | Volume and weight are unreliable. Slicers often still repair or print the mesh. | Do not block solely on this unless the slicer fails or weight matters. |
| `connected parts > 1` on a one-piece design | Disconnected islands or fragments. | Hard block unless separate parts are intentional. |
| `floating_starts` finding above 4mm2 | A mid-air feature appears with nothing beneath it. | Must seat it into the model, add a self-supporting underside, or intentionally support it. |
| Sub-4mm2 overhang/island | Below PrintForge's report threshold. | Usually spot-support territory, not an auto-repair trigger. |
| Added feature has no added mesh region | It may be buried by `union()` or missing. | Inspect with `diff_meshes.py` and oblique renders; ask for a minimal deterministic fix if confirmed. |
| Removed surface above 2 percent of base | Base mesh changed materially. | Accept only if the prompt requested cuts, engraving, hollowing, or creative reshaping. |

Base-mesh prompt rules matter during diagnosis:

- Rule 14b bans the old z-scale footprint recipe: `scale([1,1,big]) import(...)` stretches the bottom slice, not the outline.
- Rule 14f gives the verified vertical-side emboss recipe. Do not let an LLM re-derive that orientation algebra.
- `PARAM_RE` in `app.py` accepts numeric defaults with `[\d.]+`, so negative parameter defaults silently do not become sliders even though negative values are allowed inside range comments.

## Built-in diagnostics

Use these when the live service is the source of truth.

| Built-in | Endpoint | What it means |
| --- | --- | --- |
| Config | `GET /config` | Returns booleans for Bambuddy and organic readiness. Verified live as of 2026-07-06: both true. |
| Library list | `GET /models` | Returns metadata sorted newest-first by `created`; as of 2026-07-06, live count was 53. |
| Version compare | `GET /models/{id}/diff` | For child models, returns parent name, QA transition, report before/after, and parameter changes. |
| Calibration coupon | `GET /calibration` | Returns coupon SCAD, params, and an STL id. Print it, test which peg/hole clearance fits snugly, then record the measured slip fit in My presets. |
| Assembly validator | `POST /validate` | Renders each `<part>_enabled` module in assembled position when `assembled_preview` exists, then checks collisions and clearances. Not executed here because POSTs to the live service were forbidden. |

`/validate` thresholds from `app.py`:

- Collision: boolean intersection volume above `0.5mm3`.
- Known false positive: snap-fits intentionally interfere. Incident history reports a correct snap-fit around `12mm3` of overlap; not executed here — re-verify with an actual snap-fit before changing validator behavior. Treat snap-fit collision output as a prompt for engineering review, not automatic failure.
- Touching: nearest sampled gap below `0.15mm`.
- Tight fit: gap below `0.4mm`; the app warns this can print fused.

Copy-paste only when allowed to POST to the live service:

```sh
# NOT EXECUTED HERE — re-verify before trusting in a live session.
curl -fsS http://localhost:8093/validate \
  -H 'content-type: application/json' \
  --data-binary @/tmp/validate-body.json
```

## `print_report` fields

`print_report()` writes these into `meta.report` after generation:

| Field | Meaning |
| --- | --- |
| `bbox_mm` | Mesh extents rounded to 0.1mm. |
| `watertight` | `trimesh.is_watertight`. |
| `parts` | Connected components from `split(only_watertight=False)`. |
| `est_grams_pla` | Solid-volume estimate using the active profile density. Null when the mesh is not a closed volume. |
| `profile` | Active printer profile name, when a profile is supplied. |
| `material` | Active profile material. |
| `bed_fit` | `ok` or an exceeds-bed warning. |
| `measured` | `print layout` by default, or `assembled` when `assembled_preview` renders successfully for the report. |

QA status vocabulary:

- `passed`: `vision_qa()` made no edits after reviewing renders.
- `fixed xN`: the QA loop applied N successful fix rounds; default maximum is `QA_ROUNDS=2`.
- `skipped`: QA did not run, failed before producing images, or the backend/config made vision QA unavailable.

Backend vocabulary:

- `codex/gpt-5.5`: primary generation backend.
- `codex/gpt-5.5 (edit)`: refine path using file-editing in a scratch workspace.
- `local/claude-brain-coder`: LiteLLM/qwen fallback.

## Checklist

For "is it printable?":

- Run `inspect_stl.py`.
- Treat disconnected unintended parts as a hard block.
- Treat floating starts above 4mm2 as must-fix.
- Treat `watertight=false` as a weight/volume warning, not by itself a slicer failure.
- Use `render_views.py` for relief or text. Ignore straight-on views for relief proof.

For "did the refine change X?":

- Get or render the previous and new STL into `/tmp`.
- Run `diff_meshes.py`.
- Match every requested addition to an added-region bbox.
- Check removed-region output against the user request before calling damage.
- If the LLM has already had about two fix rounds, stop asking it to whack-a-mole and fix the `.scad` deterministically by hand under the change-control workflow.

For "is the service healthy?":

- Prefer `healthcheck.sh` when allowed to run `systemctl`.
- If `systemctl` is off-limits, manually run GET `/config`, GET `/models`, and `pgrep -x codex`.
- Do not use `pgrep -f 'codex exec'`; it can match the watcher itself.

## When NOT to use this skill

- Use `printforge-change-control` for edits, deployments, restarts, data mutation, or any behavior change.
- Use `printforge-debugging-playbook` for symptom-to-triage debugging across the app.
- Use `printforge-validation-and-qa` for evidence standards, golden inventory, and test discipline.
- Use `printforge-mesh-geometry-reference` for deeper trimesh, 3MF, STEP, slicing, and geometry semantics.
- Use `printforge-openscad-reference` for OpenSCAD and CSG authoring rules.
- Use `printforge-run-and-operate` for live service operations beyond read-only diagnostics.
- Use `printforge-organic-quality-campaign` for Hunyuan3D/Sparc3D/TRELLIS-style organic quality work.

## Provenance and maintenance

Re-verify drift-prone claims with these one-liners:

```sh
cd /home/cody/projects/printforge && nl -ba app.py | sed -n '46,90p;197,216p;303,353p;356,440p;922,948p;1081,1128p;1312,1333p;1438,1481p'
cd /home/cody/projects/printforge && nl -ba parts.py | sed -n '67,97p'
cd /home/cody/projects/printforge && nl -ba prompts.py | sed -n '34,80p'
cd /home/cody/projects/printforge && rg -n "validate|calibration|presets|est_grams_pla|assembled_check|TOUCHING|TIGHT FIT|COLLISION|PARAM_RE|QA_ROUNDS" app.py static/index.html README.md
cd /home/cody/projects/printforge && curl -fsS http://localhost:8093/config
cd /home/cody/projects/printforge && curl -fsS http://localhost:8093/models | UV_CACHE_DIR=/tmp/uv-cache uv run python -c 'import json,sys; rows=json.load(sys.stdin); print(len(rows)); print(rows[0].get("id"), rows[0].get("name"), rows[0].get("qa"), rows[0].get("backend"))'
cd /home/cody/projects/printforge && curl -fsS http://localhost:8093/calibration | UV_CACHE_DIR=/tmp/uv-cache uv run python -c 'import json,sys; d=json.load(sys.stdin); print(sorted(d), len(d.get("params", [])), d.get("stl_id", "")[:8])'
cd /home/cody/projects/printforge && UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/inspect_stl.py uploads/1e00498f6854.stl
cd /home/cody/projects/printforge && UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/render_views.py library/55bee2f75055/model.scad /tmp/printforge-views-reverify
cd /home/cody/projects/printforge && UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/diff_meshes.py uploads/28ef1326bceb.stl uploads/a3d5c75ddc1d.stl
cd /home/cody/projects/printforge && UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python .claude/skills/printforge-diagnostics-and-tooling/scripts/lib_audit.py --limit 5
cd /home/cody/projects/printforge && bash -n .claude/skills/printforge-diagnostics-and-tooling/scripts/healthcheck.sh
```
