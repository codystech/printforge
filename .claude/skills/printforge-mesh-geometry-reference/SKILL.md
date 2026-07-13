---
name: printforge-mesh-geometry-reference
description: >
  Mesh, format, and geometry-processing reference for PrintForge. Load when working
  with STL/3MF/STEP/STP/OBJ/GLB/GLTF/SVG files, trimesh, parts.py, organic/generate.py,
  _register_mesh, mesh_changes, _cluster_bboxes, print_report, /validate, /export,
  watertightness, manifold volume, mesh splitting, floating regions, slicing analysis,
  base-mesh fusion, STEP/cascadio conversion bugs, 3MF color/material bugs, Bambu Studio
  import/export problems, organic mesh postprocess, clearance/collision checks, or
  symptoms like "floating regions", "not watertight", "Bambu rejected the 3MF",
  "STEP imported tiny/sideways", "GLB is sideways", "export step failed", "parts did
  not split", "added feature disappeared", "buried geometry", "touching/tight fit",
  "negative parameter slider missing", or "grep -c says only one 3MF object".
---

# PrintForge mesh and geometry reference

Use this when you need to reason about mesh data as PrintForge actually handles it.
Audience assumption: you know Python, but not geometry processing. Treat all behavior
changes in `parts.py`, `app.py` mesh code, or `organic/` as high-risk and route them
through **printforge-change-control** before editing or deploying.

Ground truth verified against the repo on 2026-07-07. Volatile live-state facts are
stated as **as of 2026-07-06**. Re-run the provenance commands at the end before trusting
line numbers after any code change.

## Mesh fundamentals in 10 lines

1. A mesh is a surface made of vertices plus triangle faces; STL stores triangles only.
2. PrintForge assumes millimeters and OpenSCAD's Z-up convention: XY is the bed, +Z is up.
3. A watertight mesh has no holes or open boundary edges; it encloses a sealed volume.
4. A manifold mesh has locally sane surface topology; each edge belongs to the expected faces.
5. `trimesh.is_volume` means trimesh believes the mesh is watertight, consistently wound, and volume-capable.
6. Volume and weight estimates need watertight volume; otherwise `print_report()` returns `est_grams_pla = None`.
7. A connected component is a triangle island reachable through shared edges; disconnected bodies become separate slicer parts.
8. "Floating region" means a sliced XY island appears at a Z layer with no support under it.
9. Booleans are real solid operations; they need well-formed inputs and may fail or return empty geometry.
10. A `Scene` can contain several named meshes plus transforms; a forced `Trimesh` loses scene/body names unless extracted first.

## Trimesh APIs used here

| API | Repo call site | Use it for | Trap |
|---|---:|---|---|
| `trimesh.load_mesh(path)` | `parts.py:20`, `app.py:199`, `app.py:340-341`, `app.py:1183`, `app.py:1458` | Load a mesh when the caller expects a single `Trimesh`. | It is convenient for STL/exported meshes; do not use it when you need scene graph body names. |
| `trimesh.load(path, force="mesh")` | `app.py:612` | Canonical upload conversion to one mesh before exporting `uploads/<id>.stl`. | For STEP, this is the cascadio-converted mesh; body names are handled separately. |
| `trimesh.load(path)` as `Scene` | `app.py:620-627` | Extract named CAD bodies and transforms from STEP uploads. | Preserve `scene.graph.nodes_geometry`, apply each node transform, then apply the STEP scale/rotation fix. |
| `mesh.split(only_watertight=False)` | `parts.py:21`, `app.py:205`, `app.py:663`, `organic/generate.py:12` | Count or split connected components even when meshes are imperfect. | `only_watertight=True` would hide non-watertight but still printable bodies. |
| `section(...).to_2D().polygons_full` | `parts.py:78-83` | Bottom-up slicing for floating-region detection. | Section noise exists; this repo uses two-layer lookback and area thresholds. |
| `section(...).bounds` | `app.py:654-659` | Five horizontal cross-section extents in upload metadata. | Extents are guidance, not an exact polygon mask. |
| `trimesh.boolean.intersection(..., engine="manifold")` | `app.py:1468-1469` | Assembly collision volume in `/validate`. | Snap-fits can be intentional interference; this validator can false-positive on joints. |
| `trimesh.proximity.closest_point(mesh, points)` | `app.py:1475-1480` | Clearance check after no collision is found. | Requires proximity deps (`rtree`/scipy stack); samples only 200 points from one part. |
| `simplify_quadric_decimation(...)` | `organic/generate.py:17-21` | Reduce organic meshes over 400k faces. | API drift: code first tries `face_count=400_000`, then falls back to a 0-1 reduction fraction. |

## 3MF anatomy as written by `parts.py`

PrintForge writes a minimal 3MF itself, not a slicer project file.

| Piece | Source | Meaning |
|---|---:|---|
| ZIP members | `parts.py:60-63` | Exactly `[Content_Types].xml`, `_rels/.rels`, and `3D/3dmodel.model`. |
| `CONTENT_TYPES` | `parts.py:7-11` | Package content-type declarations. |
| `RELS` | `parts.py:13-16` | Relationship pointing to `/3D/3dmodel.model`. |
| `PALETTE` | `parts.py:29-30` | Six default colors: red, white, blue, yellow, green, dark gray. |
| `<basematerials id="100">` | `parts.py:35-38` | One material table with one `<base displaycolor=...>` per palette color. |
| `<object ... pid="100" pindex="...">` | `parts.py:46-49` | Assigns each connected component a material palette index. |
| `<build><item objectid="..."/></build>` | `parts.py:51-57` | Places each object in the 3MF build. |

Use `write_3mf(split_parts(stl), out)` indirectly through `_build_3mf()` (`app.py:1170-1173`)
and `GET /export/{stl_id}?fmt=3mf` (`app.py:1176-1180`). `split_parts()` orders biggest
component first (`parts.py:24-26`).

**Do not count 3MF objects with `grep -c`.** `parts.py` writes model XML mostly on one line,
so `grep -c '<object '` reports matching lines, not object count. Use token counting:

```sh
cd /home/cody/projects/printforge
tmp="$(mktemp -d /tmp/printforge-3mf.XXXXXX)"
unzip -p /path/to/model.3mf 3D/3dmodel.model > "$tmp/model.xml"
grep -o '<object ' "$tmp/model.xml" | wc -l
```

Open question: **Bambu may ignore 3MF `basematerials` colors.** This is unconfirmed here;
do not promise Bambu color import behavior without testing a real exported 3MF in Bambu
Studio.

## Format conversion and traps

| Format/path | Code | Current behavior | Maintainer trap |
|---|---:|---|---|
| STL/3MF/OBJ/GLB/GLTF upload | `app.py:589-598`, `app.py:612`, `app.py:643-647` | Accept extension, load with `trimesh.load(..., force="mesh")`, export canonical STL to `uploads/<id>.stl`; non-STEP originals are deleted. | Direct GLB/GLTF uploads are not given the STEP rotation fix. If a GLB is Y-up and appears sideways, verify with a test asset before changing behavior. |
| STEP/STP upload | `app.py:601-616` | Requires `cascadio`; loads as mesh, scales by `1000`, rotates +90 deg about X. | cascadio emits GLB-like meters and Y-up; STEP dimensions are intended as mm. Forgetting either fix gives tiny or sideways uploads. |
| STEP named bodies | `app.py:617-635`, `app.py:833-845` | Loads the STEP scene graph, applies node transforms, scale, and rotation, then stores up to 28 body bboxes in `bodies_detail`. | Body extraction exists so port/cutout locations come from real CAD body names and positions, not guessed standard layouts. |
| STEP export | `app.py:1185-1188` | Refused with "mesh-only" error. | Do not fake STEP export from mesh output; route any CAD/BRep export request through change control. |
| CadQuery STEP/STL + Bambu slice (dormant Lab contract) | `evolution_lab/cadquery.py`, `evolution_lab/slicer.py` | An injected worker exports each named part to STEP and STL. Its gate claims are ignored; a trusted parent validator derives B-rep, STEP round-trip, tessellation and existing mesh checks. Only then may the versioned Bambu adapter slice positive-role STLs with immutable full profiles and persist sliced 3MF/log/metrics. Missing/false geometry or slice evidence is a hard rejection. | This is an off-by-default boundary tested with mocks, not proof that CadQuery, Bambu Studio, or Bubblewrap currently runs on the host. Production mesh-only STEP refusal remains unchanged. |
| SVG upload | `app.py:554-573`, `app.py:799-809` | Stores SVG and gives the LLM `linear_extrude(height) import("<path>", center=true)`. | SVG is 2D outline input, not mesh. Use `resize([target_w, target_h, 0], auto=true)` in SCAD if dimensions matter. |
| Bitmap trace to SVG | `app.py:576-586`, `app.py:686-696` | `magick` grayscales/thresholds, `potrace` emits SVG, then SVG path above applies. | Trace writes only scratch files in `WORK_DIR`; do not run as a repo write. |
| Inches/tiny heuristic | `app.py:666-672` | If max dimension `<5`, warn "units may be inches" and suggest scale by 25.4. | This is a warning, not an automatic scale. |
| Huge model heuristic | `app.py:671-672` | If max dimension `>400`, warn that it is bigger than the bed. | This is bed/units triage, not a hard upload failure. |
| Negative parameter default | `app.py:46-49` | `PARAM_RE` default value alternation is `[\d.]+|"[^"]*"`, with no minus sign. | A parameter like `x = -5; // [-20:20]` silently fails to become a slider. This is a customizer regex issue; load architecture/config skills before changing it. |

## Analytical detectors

### `floating_starts(stl_path, step=1.0, min_area=0.3, report_area=4.0, tol=0.8)`

Call sites: `parts.py:67-97`, QA at `app.py:406-418`, refine prompt note at
`app.py:1019-1030`, response warnings at `app.py:1133-1145`.

Algorithm:

1. Load mesh with `trimesh.load_mesh()` (`parts.py:73`).
2. Sweep Z from `z_min + step/2` to `z_max` in 1 mm layers (`parts.py:74-78`).
3. Intersect the mesh with each horizontal plane (`section`, `parts.py:78`).
4. Convert section curves to 2D polygons (`to_2D`, `polygons_full`, `parts.py:82-83`).
5. Ignore polygon specks under `min_area = 0.3 mm^2`.
6. Compare current polygons against buffered support from the previous two layers
   (`tol = 0.8 mm`, `parts.py:84-86`).
7. Two-layer lookback exists because thin cylinders and noisy sections can miss one layer.
8. Report only islands over `report_area = 4.0 mm^2`; smaller islands are treated as
   spot-support territory.
9. Map the 2D centroid back to 3D with the section transform (`parts.py:90`).
10. Dedupe by XY column within 4 mm so one floating mast or flag reports once
    (`parts.py:91-95`).

Calibration story to preserve: this detector was tuned against a real boat failure where
mast features hovered roughly 0.5-2 mm above the deck. Disconnected/floating parts are a
hard fix target; small overhangs are usually a slicer/support decision. After about two
LLM fix rounds, stop and repair the `.scad` deterministically by hand.

### `mesh_changes(base_stl, new_stl)`

Call sites: `app.py:337-353`, fed into `vision_qa()` at `app.py:371-405`.

Algorithm:

1. Load base and new meshes (`app.py:340-341`).
2. Round triangle centroids to `0.1 mm` (`app.py:342-343`).
3. Build centroid sets and classify new triangles whose rounded centroid is absent from
   the base as `added` (`app.py:344-346`).
4. Classify base triangles absent from the new mesh as `removed` (`app.py:346`).
5. Cluster added centroids with `_cluster_bboxes()` so QA can render close-ups of changed
   regions before whole-model views (`app.py:347`, `app.py:377-387`).
6. If removed triangles exceed 2% of the base surface, cluster the largest removal and
   pass it to QA as possible damage (`app.py:348-353`, `app.py:396-405`).

Why centroid diff works here: historical incident evidence says OpenSCAD manifold union
generally keeps untouched imported mesh faces in the output, so a 0.1 mm centroid-set diff
cleanly isolates added geometry. That invariant was not re-executed here; re-verify on a
real imported OpenSCAD render before changing diff logic. The property is why buried
features can be detected: if a requested addition is unioned entirely inside an existing
solid, it has no visible added-region cluster, so QA is told the feature is buried or
missing (`app.py:389-394`).

Do not turn the `>2% removed surface` heuristic into a hard block. Cuts, engraving,
hollowing, and creative reshaping can be user intent; QA must decide from the request
(`app.py:399-404`).

### `_cluster_bboxes(pts, cell=8.0, max_regions=3)`

Call site: `app.py:318-334`.

Algorithm:

1. Shift points to a local minimum (`mn = pts.min(axis=0)`).
2. Voxelize points into 8 mm cells (`floor((pts - mn) / cell)`, `app.py:322-324`).
3. Mark occupied cells in a boolean grid with a one-cell border (`app.py:324-325`).
4. Label connected occupied voxels with `scipy.ndimage.label()` and a 3x3x3 structure
   (`app.py:326`).
5. For each label with at least 8 points, emit the point-count and bbox (`app.py:329-332`).
6. Return the largest clusters first, limited to `max_regions` (`app.py:333-334`).

Modify `cell`, `len(p) >= 8`, and `max_regions` only with real before/after meshes; these
numbers control what QA can see.

## Organic postprocess pipeline

Source: `organic/generate.py:9-33`, invoked by `/organic` at `app.py:1278-1309`.
Do not run organic generation on the shared GPU from this skill. For environment setup,
use **printforge-build-and-env**.

Pipeline:

1. Rotate Hunyuan output from Y-up to PrintForge Z-up: +90 deg about X
   (`organic/generate.py:10-11`).
2. Split connected components with `only_watertight=False` and keep the largest by face
   count (`organic/generate.py:12-14`).
3. Fill holes and fix normals (`organic/generate.py:15-16`).
4. If over 400k faces, simplify to 400k faces; handle trimesh API drift with the
   `face_count=` try and fraction fallback (`organic/generate.py:17-21`).
5. Scale so the largest dimension equals `target_mm` (`organic/generate.py:22`).
6. Center XY, floor to `z=0`, then try a `0.4 mm` capped slice to shave a flat first
   layer (`organic/generate.py:23-31`).
7. If flat-base slicing fails, ship the mesh anyway; the comment treats a raft as an
   acceptable soft fallback (`organic/generate.py:31-32`).

`/organic` clamps `target_mm` to 10-250 before calling the script (`app.py:1296-1299`),
registers the STL with `_register_mesh()`, and returns an OpenSCAD wrapper that imports
the stored mesh (`app.py:1302-1308`).

## Assembly validation

Use `/validate` when a generated model has `*_enabled` toggles and optional
`assembled_preview` (`app.py:1438-1481`).

Checklist:

- Render each part alone by disabling every toggle except one (`app.py:1450-1458`).
- If `assembled_preview` exists, force it to `1` so parts render in assembled position
  (`app.py:1448`, `app.py:1454-1455`).
- Skip pair checks whose bboxes are more than 5 mm apart (`app.py:1463-1466`).
- Treat boolean intersection volume `>0.5 mm^3` as `COLLISION` (`app.py:1467-1473`).
- If no collision, sample 200 points and check closest-point gap (`app.py:1475-1480`).
- Report `<0.15 mm` as `TOUCHING`; report `<0.4 mm` as `TIGHT FIT`.

Known open issue as of 2026-07-06: snap-fits can be intentionally interfering and may be
flagged as collision/touching. Do not "fix" correct snap-fit geometry solely because
`/validate` complains.

## Safe investigation commands

Use these read-only or `/tmp`-only commands while debugging mesh behavior:

```sh
cd /home/cody/projects/printforge

# Run the shipped parts.py self-check. Put uv cache in /tmp if the sandbox cannot write ~/.cache.
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run \
  --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx \
  python parts.py

# Print mesh basics for an existing uploaded STL without modifying uploads/.
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run \
  --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx \
  python - /home/cody/projects/printforge/uploads/<id>.stl <<'PY'
import sys, trimesh
m = trimesh.load_mesh(sys.argv[1])
print("faces", len(m.faces))
print("bounds", m.bounds.tolist())
print("extents", m.extents.tolist())
print("watertight", m.is_watertight, "is_volume", m.is_volume, "volume", float(m.volume))
print("components", len(m.split(only_watertight=False)))
PY

# Count objects in a 3MF correctly.
tmp="$(mktemp -d /tmp/printforge-3mf.XXXXXX)"
unzip -p /path/to/model.3mf 3D/3dmodel.model > "$tmp/model.xml"
grep -o '<object ' "$tmp/model.xml" | wc -l
```

Never run `run.sh`, `uvicorn`, `systemctl`, Docker, `organic/generate.py`, or any GPU job
from this skill. Never POST/PUT/PATCH/DELETE to the live service while investigating mesh
format behavior. GET endpoints are operational checks; use **printforge-run-and-operate**
for those.

## When NOT to use this skill

| Need | Use instead |
|---|---|
| Change safety, deploy gates, restart protocol, or whether a mesh change is allowed | `printforge-change-control` |
| OpenSCAD CSG syntax, prompt rule 14 base-mesh fusion recipes, emboss orientation details | `printforge-openscad-reference` |
| End-to-end test discipline, golden evidence, QA standards | `printforge-validation-and-qa` |
| Service operations, `/config`, logs, systemd, artifact map | `printforge-run-and-operate` |
| Dependencies, Nix/uv/Docker/organic setup, Hunyuan cache paths | `printforge-build-and-env` |
| Environment variables, profile constants, `PARAM_RE` policy | `printforge-config-and-flags` |
| Symptom triage across the whole app | `printforge-debugging-playbook` |
| Historical incident narrative | `printforge-failure-archaeology` |
| Organic model-quality campaign or alternative 3D backends | `printforge-organic-quality-campaign` |
| Research claims and open-problem framing | `printforge-research-frontier` or `printforge-research-methodology` |

## Provenance and maintenance

Re-verify drift-prone claims with these one-liners:

```sh
cd /home/cody/projects/printforge

# Line numbers and call sites.
nl -ba parts.py | sed -n '1,130p'
nl -ba app.py | sed -n '197,216p;318,353p;554,683p;792,856p;1170,1188p;1278,1309p;1438,1481p'
nl -ba organic/generate.py | sed -n '1,80p'
nl -ba prompts.py | sed -n '43,80p'

# Dependency declarations for mesh APIs.
rg -n 'trimesh|networkx|lxml|shapely|rtree|manifold3d|cascadio|python-multipart|fast-simplification' run.sh Dockerfile organic/setup.sh

# Confirm PARAM_RE still rejects negative numeric defaults.
nl -ba app.py | sed -n '44,91p'

# Confirm allowed upload/export formats and STEP refusal.
rg -n 'MESH_EXTS|cascadio|apply_scale\\(1000\\)|GLB Y-up|fmt == "step"|linear_extrude\\(height\\) import' app.py

# Execute the shipped parts.py self-check.
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python parts.py

# Reproduce 3MF package members and grep-count trap using only /tmp.
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python - <<'PY'
import tempfile, zipfile, subprocess
from pathlib import Path
import trimesh
from parts import split_parts, write_3mf
with tempfile.TemporaryDirectory(dir="/tmp") as d0:
    d = Path(d0)
    a = trimesh.creation.box((10,10,10))
    b = trimesh.creation.box((5,5,5)); b.apply_translation((20,0,0))
    stl = d / "two.stl"; (a + b).export(stl)
    out = write_3mf(split_parts(stl), d / "two.3mf")
    with zipfile.ZipFile(out) as z:
        print(sorted(z.namelist()))
        xml = z.read("3D/3dmodel.model").decode()
    p = d / "model.xml"; p.write_text(xml)
    print("grep -c", subprocess.check_output(["grep","-c","<object ",str(p)], text=True).strip())
    print("grep -o|wc", subprocess.check_output(f"grep -o '<object ' {p} | wc -l", shell=True, text=True).strip())
PY

# Re-test mesh_changes, manifold boolean intersection, and closest_point without touching repo data.
UV_CACHE_DIR=/tmp/printforge-uv-cache uv run --with fastapi --with httpx --with pydantic --with python-multipart --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx --with manifold3d python - <<'PY'
import tempfile
from pathlib import Path
import trimesh
from app import mesh_changes
with tempfile.TemporaryDirectory(dir="/tmp") as d0:
    d = Path(d0)
    base = trimesh.creation.box((10,10,10))
    add = trimesh.creation.box((2,2,2)); add.apply_translation((8,0,0))
    bp, np = d/"base.stl", d/"new.stl"
    base.export(bp); (base + add).export(np)
    print(mesh_changes(bp, np))
    a = trimesh.creation.box((10,10,10))
    b = trimesh.creation.box((10,10,10)); b.apply_translation((9,0,0))
    print(round(float(trimesh.boolean.intersection([a,b], engine="manifold").volume), 2))
    far = trimesh.creation.box((10,10,10)); far.apply_translation((11,0,0))
    print(round(float(trimesh.proximity.closest_point(far, a.sample(50))[1].min()), 2))
PY
```

Unconfirmed/open items to keep labeled until retested:

- Bambu Studio may ignore the minimal 3MF `basematerials` color assignments.
- Direct GLB/GLTF orientation behavior for non-STEP uploads needs a representative test
  asset before adding any automatic rotation fix.
- Snap-fit intent is not modeled by `/validate`; collisions/touching may be correct by
  design.
