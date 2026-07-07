---
name: printforge-proof-and-analysis-toolkit
description: >
  First-principles proof recipes for PrintForge geometry and behavior. Load when you must
  PROVE a claim instead of trusting a render, screenshot, slicer preview, or LLM answer:
  "is the feature really there", "is it placed right", "did the refine preserve X",
  "is this recipe correct", "does this fit", "is the export correct", "why did this
  print fail", "floating regions", "Bambu rejected the 3MF", "missing feature",
  "buried sail", "10mm robot smudge", "z-scale footprint", mesh_changes,
  floating_starts, lock_violations, PARAM_RE, /models/{id}/diff, 3MF object count,
  pid/pindex, prompt-contract recipe validation. To RUN routine measurements use
  printforge-diagnostics-and-tooling; to decide thresholds/what evidence suffices
  use printforge-validation-and-qa.
---

# PrintForge proof and analysis toolkit

Use this skill when the question is not "does it look okay?" but "can we prove it?" A
render is evidence, not proof. For PrintForge, geometric claims are proven by mesh
analysis, section analysis, code diffs, XML inspection, and physical calibration.

Definitions:

- **Mesh**: triangles describing a 3D solid. STL and 3MF ultimately carry meshes.
- **Triangle centroid**: the center point of one mesh triangle. PrintForge uses rounded
  triangle centroids to find added and removed geometry.
- **Cluster**: nearby changed triangles grouped into one spatial region.
- **Section**: a 2D slice through a mesh at a fixed Z height.
- **Floating start**: a section island that appears with no material under it in the last
  two lower slices. Slicers report these as floating regions.
- **Param-set diff**: compare customizer variables parsed by `PARAM_RE`, not free-form
  text.
- **Round-trip**: write an artifact, load it back through an independent parser, then
  compare expected object counts and metadata.

Before changing project behavior, route through **printforge-change-control**. This skill
proves facts; it does not authorize edits.

Use these commands from the repo root:

```sh
cd /home/cody/projects/printforge
```

If a restricted sandbox cannot write `uv` or `nix` caches under `$HOME`, prefix commands
with `UV_CACHE_DIR=/tmp/uv-cache` and, for `nix`, `HOME=/tmp/nix-home XDG_CACHE_HOME=/tmp/nix-cache`.

---

## 1. Prove a requested feature exists and is placed right

**When to use**

Use this for base-mesh refines where a new sail, flag, robot, text, bracket, boss, or cut
may be buried, too small, misplaced, or missing. Do not judge from a whole-model render.

**Exact procedure**

Render the candidate model to `/tmp` if you only have SCAD:

```sh
mkdir -p /tmp/printforge-proof
HOME=/tmp/nix-home XDG_CACHE_HOME=/tmp/nix-cache \
nix shell nixpkgs#openscad-unstable --command openscad \
  --enable=textmetrics --enable=manifold \
  -o /tmp/printforge-proof/new.stl --export-format binstl \
  library/<model-id>/model.scad
```

Then run the triangle-centroid set diff. This mirrors `mesh_changes()` in `app.py`:

```sh
BASE_STL=uploads/<base-mesh-id>.stl   # replace <base-mesh-id> with a real 12-hex id from `ls uploads/*.stl`
NEW_STL=/tmp/printforge-proof/new.stl
export BASE_STL NEW_STL

UV_CACHE_DIR=/tmp/uv-cache uv run \
  --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx \
  python - <<'PY'
import os
import numpy as np
import trimesh
from scipy import ndimage

def cluster_bboxes(pts, cell=8.0, max_regions=8):
    if len(pts) == 0:
        return []
    mn = pts.min(axis=0)
    idx = np.floor((pts - mn) / cell).astype(int)
    grid = np.zeros(idx.max(axis=0) + 3, dtype=bool)
    grid[tuple((idx + 1).T)] = True
    lab, n = ndimage.label(grid, structure=np.ones((3, 3, 3)))
    labels = lab[tuple((idx + 1).T)]
    out = []
    for i in range(1, n + 1):
        p = pts[labels == i]
        if len(p) >= 8:
            out.append((len(p), p.min(axis=0), p.max(axis=0)))
    out.sort(key=lambda t: -t[0])
    return out[:max_regions]

base = trimesh.load_mesh(os.environ["BASE_STL"])
new = trimesh.load_mesh(os.environ["NEW_STL"])
bc = np.round(base.triangles_center, 1)
nc = np.round(new.triangles_center, 1)
bset, nset = set(map(tuple, bc)), set(map(tuple, nc))
added = new.triangles_center[[tuple(c) not in bset for c in nc]]
removed = base.triangles_center[[tuple(c) not in nset for c in bc]]

print(f"base_faces={len(base.faces)} new_faces={len(new.faces)}")
print(f"added_triangles={len(added)} removed_triangles={len(removed)}")
for i, (count, mn, mx) in enumerate(cluster_bboxes(added), 1):
    c = (mn + mx) / 2
    print(
        f"added_region_{i}: triangles={count} "
        f"bbox_min={[round(float(x), 1) for x in mn]} "
        f"bbox_max={[round(float(x), 1) for x in mx]} "
        f"center={[round(float(x), 1) for x in c]}"
    )
print(f"components={len(new.split(only_watertight=False))} watertight={new.is_watertight}")
PY
```

Match every requested element to an added region. A requested element with no region is
missing or buried inside existing solid. A region at the wrong coordinate proves
misplacement. Significant removed triangles prove base damage; decide from the user's
request whether the damage was intentional.

**What the numbers mean**

| Number | Interpretation |
|---|---|
| `added_triangles=0` | No real added surface survived the union. The feature is missing or fully buried. |
| One cluster covering all additions | Large edit or additions connected through existing geometry; inspect sub-bboxes manually. |
| Cluster bbox Z below surface | Addition started inside the base, often correct for fusion. Confirm it exits into open air. |
| `removed_triangles > 2% of base faces` | Same threshold as `app.py`: possible base damage unless the user asked for a cut/reshape. |
| `components > 1` | Hard printability concern for a one-piece model; run recipe 2. |

**Worked examples**

- Buried sail: a sail extruded into a cabin produced no visible changed region for the
  sail, because `union()` erases geometry fully inside another solid. The fix became the
  rule that thin additions must project into open air, and QA now asks the reviewer to
  match every requested element to a changed region.
- 10mm robot smudge: full-model renders made the robot unreadable. The mesh diff produced
  a small changed bbox, which drove close-up render cameras around that region.
- Current repo check executed here: rendering `library/3e7accab949c/model.scad` against
  `uploads/1e00498f6854.stl` produced `base_faces=225706`, `new_faces=185700`,
  `added_triangles=28064`, `removed_triangles=68089`, one added region with bbox
  `[-18.0,-15.4,8.7] -> [27.9,15.4,41.9]`, `components=1`, `watertight=True`.

---

## 2. Prove printability

**When to use**

Use this when Bambu Studio reports "floating regions", when a print fails, when a feature
may start above the deck, or before shipping a "print-ready" model. Always pair floating
analysis with a disconnected-component check.

**Exact procedure**

```sh
STL=/tmp/printforge-proof/new.stl
export STL

UV_CACHE_DIR=/tmp/uv-cache uv run \
  --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx \
  python - <<'PY'
import os
import trimesh
from parts import floating_starts

stl = os.environ["STL"]
m = trimesh.load_mesh(stl)
floats = floating_starts(stl)
print(f"components={len(m.split(only_watertight=False))}")
print(f"watertight={m.is_watertight}")
print(f"floating_findings={len(floats)}")
for f in floats:
    print(f"z={f['z']} x={f['x']} y={f['y']} area={f['area']}mm2")
PY
```

`floating_starts()` is calibrated in `parts.py` as of 2026-07-06 (the algorithm
and its thresholds are OWNED by printforge-mesh-geometry-reference — if these
numbers ever disagree with that skill or `parts.py:67`, those win):

| Setting | Value | Reason |
|---|---:|---|
| Slice step | `1.0mm` | Enough vertical resolution for FDM support problems without making checks slow. |
| Two-layer lookback | last 2 lower sections | Tolerates section noise on thin cylinders. |
| `min_area` | `0.3mm2` | Ignore tiny section artifacts before support comparison. |
| `report_area` | `4.0mm2` | Smaller islands are usually spot-support territory. |
| `tol` | `0.8mm` buffer | Allows near-contact section noise without false positives. |
| XY dedupe | within 4mm | One bad feature reports once instead of every layer. |

**What the numbers mean**

| Result | Action |
|---|---|
| `components > 1` for a one-piece print | Hard block. Fuse the bodies or intentionally separate them. |
| `watertight=False` | Investigate before export; weight/volume may be unreliable. |
| `floating_findings=0` | No bottom-up floating starts at this calibration. Still inspect overhangs. |
| Finding `area > 4mm2` | Seat the feature by embedding it 2-3mm into what is below, or make the underside self-supporting. |
| Small intentional overhang | Soft warning. Tree supports can be acceptable if the user accepts them. |

**Worked examples**

- Boat calibration story: the detector was tuned after it found 16 real issues on a boat,
  including a mast base hovering roughly 0.5-2mm above the deck. That exact broken STL is
  not identified in the current repo, so this historical count is not executed here - re-verify.
- Current repo check executed here: `library/3e7accab949c` rendered as one watertight
  component but reported 3 floating starts: `(z=20.5,x=-14.5,y=0.0,area=17.9)`,
  `(z=20.5,x=28.2,y=0.0,area=12.4)`, `(z=27.5,x=4.3,y=-0.2,area=21.7)`.
  `library/27348cced127` rendered as one watertight component and reported 4 floating
  starts. Treat this as proof that "one piece" and "watertight" do not prove support-free
  printability.

---

## 3. Prove a refine preserved what it must

**When to use**

Use this after any refine that must preserve locked parts, suppressed parts, user intent,
or slider defaults. Use it when a model lost a robot, wheel, chest, lid, or unrelated
module after a refine.

**Exact procedure: locked module proof**

```sh
OLD_SCAD=/tmp/old.scad
NEW_SCAD=/tmp/new.scad
PART_STATE_JSON='{"lid":{"locked":true},"base":{"locked":false}}'
export OLD_SCAD NEW_SCAD PART_STATE_JSON

UV_CACHE_DIR=/tmp/uv-cache uv run python - <<'PY'
import json, os, re
old = open(os.environ["OLD_SCAD"]).read()
new = open(os.environ["NEW_SCAD"]).read()
ps = json.loads(os.environ["PART_STATE_JSON"])

def module_block(scad, name):
    m = re.search(rf"module\s+{re.escape(name)}\s*\(", scad)
    if not m:
        return None
    i = scad.find("{", m.start())
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(scad)):
        if scad[j] == "{":
            depth += 1
        elif scad[j] == "}":
            depth -= 1
            if depth == 0:
                return scad[m.start():j + 1]
    return None

violations = []
for part, state in ps.items():
    if not state.get("locked"):
        continue
    for candidate in (part, f"{part}_module"):
        before = module_block(old, candidate)
        if before is None:
            continue
        after = module_block(new, candidate)
        if after is None or " ".join(before.split()) != " ".join(after.split()):
            violations.append(part)
        break
print("lock_violations=" + ",".join(violations))
PY
```

**Exact procedure: parameter default diff**

Use the same parser class as `/models/{id}/diff`. Important: as of 2026-07-06 `PARAM_RE`
does not accept a minus sign in the default value, so negative defaults silently fail to
become sliders.

```sh
OLD_SCAD=/tmp/old.scad
NEW_SCAD=/tmp/new.scad
export OLD_SCAD NEW_SCAD

UV_CACHE_DIR=/tmp/uv-cache uv run python - <<'PY'
import os, re
PARAM_RE = re.compile(
    r'^(\w+)\s*=\s*([\d.]+|"[^"]*")\s*;\s*//\s*(?:\[([\d.:\-]+)\]|(free text))',
    re.MULTILINE,
)
def defaults(path):
    scad = open(path).read()
    return {m.group(1): m.group(2).strip('"') for m in PARAM_RE.finditer(scad)}
a, b = defaults(os.environ["OLD_SCAD"]), defaults(os.environ["NEW_SCAD"])
print("params_changed", [f"{k}: {a[k]} -> {b[k]}" for k in a if k in b and a[k] != b[k]][:12])
print("params_added", [k for k in b if k not in a][:12])
print("params_removed", [k for k in a if k not in b][:12])
PY
```

**What the numbers mean**

| Output | Interpretation |
|---|---|
| `lock_violations=` empty | Locked modules survived whitespace-normalized comparison. |
| Locked part listed | Treat as a failed refine. Restore that module byte-for-byte before proceeding. |
| Param removed | Possible feature loss or lost slider. Inspect before accepting. |
| Param added | Expected for a new requested feature; suspicious for unrelated churn. |
| Negative default absent | Known `PARAM_RE` limitation; fix the default or route regex change through change control. |

**Worked examples**

- Locked-part enforcement: `app.py` extracts module blocks by brace count and compares
  whitespace-normalized text after every refine. If a locked module changes, PrintForge
  forces one correction round and returns remaining violations.
- Feature-loss overhaul: full-file rewrites repeatedly dropped unrelated modules from a
  long model. Refines now edit `model.scad` in place through `call_codex_edit()` instead
  of reprinting the file wholesale.

---

## 4. Prove a fit

**When to use**

Use this for lids, trays, snap tabs, hinges, board cases, negative-space cutouts, and any
claim like "this Raspberry Pi will fit" or "this lid has enough clearance."

**Exact procedure**

Render or extract the two mating parts as separate STLs in their assembled positions.
Then compare cross-section extents at the mating height:

```sh
PART_A=/tmp/outer_or_case.stl
PART_B=/tmp/inner_or_board.stl
Z=12.5
export PART_A PART_B Z

UV_CACHE_DIR=/tmp/uv-cache uv run \
  --with trimesh --with numpy --with shapely --with rtree --with networkx \
  python - <<'PY'
import os
import numpy as np
import trimesh

def section_bounds(path, z):
    m = trimesh.load_mesh(path)
    sec = m.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
    if sec is None:
        raise SystemExit(f"no section at z={z} for {path}")
    planar, _ = sec.to_2D()
    b = planar.bounds
    return np.array([b[0][0], b[0][1], b[1][0], b[1][1]], dtype=float)

z = float(os.environ["Z"])
a = section_bounds(os.environ["PART_A"], z)
b = section_bounds(os.environ["PART_B"], z)
clearance_x = min(abs(b[0] - a[0]), abs(a[2] - b[2]))
clearance_y = min(abs(b[1] - a[1]), abs(a[3] - b[3]))
print(f"a_bounds_xy={a.round(3).tolist()}")
print(f"b_bounds_xy={b.round(3).tolist()}")
print(f"clearance_x_each_side~{clearance_x:.3f}mm clearance_y_each_side~{clearance_y:.3f}mm")
PY
```

For complex parts, also run `/validate` or the local equivalent for pairwise collision
volume and closest-point gap. The app flags intersections above `0.5mm3`, touching gaps
below `0.15mm`, and tight gaps below `0.4mm`.

**Clearance table** (constants OWNED by printforge-config-and-flags via
`DEFAULT_PROFILES`; rule semantics by printforge-openscad-reference — on
disagreement, re-verify against `app.py:219-234` and defer there)

| Clearance | Use | Provenance as of 2026-07-06 |
|---:|---|---|
| `0.15mm` | Snap-fit profile default and "touching" threshold | `app.py` profile `snap_clearance`; `/validate` flags gaps below this as touching. Known false positives exist for intentional joints. |
| `0.2mm` | Slip fit default | `prompts.py` mating-part rule; `app.py` profile `fit_clearance`; calibration preset says measured slip fit can be recorded from the coupon. |
| `0.4mm` | Loose fit and assembly warning threshold | `prompts.py` loose fit rule; `/validate` warns gaps below this can print fused. |
| `0.5mm` | Print-in-place moving surfaces | `prompts.py` print-in-place mechanism rule. |

**Physical calibration**

Use the `/calibration` coupon before overriding global fit assumptions. It prints holes
and pegs for `[0.1, 0.15, 0.2, 0.3, 0.4]`; the clearance that slides snugly without force
is the printer-specific slip fit and belongs in My presets.

---

## 5. Prove a prompt-contract recipe before it enters `prompts.py`

**When to use**

Use before adding or changing any OpenSCAD recipe, orientation transform, footprint
method, import strategy, or CSG pattern in `prompts.py`. This is mandatory because prompt
rules become executable law for every future generation.

**Exact procedure**

1. Create a minimal SCAD test in `/tmp` that isolates the recipe and includes a measurable
   base, target feature, and failure witness.
2. Render the STL with `openscad-unstable`.
3. Render oblique PNGs from at least two azimuths. Straight-on orthographic views hide low
   relief.
4. Run section or mesh analysis appropriate to the claim.
5. Only then propose the prompt edit through **printforge-change-control**.

```sh
mkdir -p /tmp/printforge-recipe-proof
cat > /tmp/printforge-recipe-proof/test.scad <<'SCAD'
$fn = 48;
// Minimal geometry for the candidate recipe goes here.
cube([30, 12, 6]);
SCAD

HOME=/tmp/nix-home XDG_CACHE_HOME=/tmp/nix-cache \
nix shell nixpkgs#openscad-unstable --command openscad \
  --enable=textmetrics --enable=manifold \
  -o /tmp/printforge-recipe-proof/test.stl --export-format binstl \
  /tmp/printforge-recipe-proof/test.scad

for AZ in 25 205; do
  HOME=/tmp/nix-home XDG_CACHE_HOME=/tmp/nix-cache \
  nix shell nixpkgs#openscad-unstable --command openscad \
    --enable=textmetrics --enable=manifold \
    -o /tmp/printforge-recipe-proof/oblique-$AZ.png \
    --imgsize 1000,750 --autocenter --viewall \
    --camera 0,0,0,70,0,$AZ,120 --projection p \
    /tmp/printforge-recipe-proof/test.scad
done
```

**What the numbers mean**

There is no universal pass number. Define the proof before testing: expected bbox,
expected section outline, expected number of disconnected components, expected changed
region, and expected printability result. If the recipe cannot be falsified by a number,
it is not ready for `prompts.py`.

**Worked examples**

- Negative: a z-scale footprint recipe shipped without this proof. `scale([1,1,big])
  import(...)` stretches the bottom slice, not the top-view outline, and collapsed a
  user's boat parts into slivers. The failure reproduction is not executed here - re-verify
  before changing the rule. `prompts.py` rule 14b now bans it and points to
  `linear_extrude(h) projection() import(path)` only when a true outline is needed.
- Positive: the raised vertical-side emboss recipe in rule 14f was verified empirically
  after failed orientation algebra. The two-side reproduction is not executed here -
  re-verify before changing the rule. The pinned +Y construct is `rotate([90,0,0])
  mirror([1,0,0])`; use the rule, do not re-derive the transform.

---

## 6. Prove an export is correct

**When to use**

Use this when a 3MF fails in Bambu Studio, color assignment looks wrong, multi-part export
may have lost a body, or a grep count disagrees with the slicer.

**Exact procedure**

First run the shipped self-check:

```sh
UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with networkx --with lxml python parts.py
```

Then inspect a real export or generate a temporary 3MF from an STL:

```sh
STL=/tmp/printforge-proof/new.stl
export STL

UV_CACHE_DIR=/tmp/uv-cache uv run \
  --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx --with lxml \
  python - <<'PY'
import os, re, tempfile, zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
import trimesh
from parts import split_parts, write_3mf

with tempfile.TemporaryDirectory(dir="/tmp") as d:
    parts = split_parts(os.environ["STL"])
    out = write_3mf(parts, Path(d) / "proof.3mf")
    loaded = trimesh.load(out)
    loaded_count = len(getattr(loaded, "geometry", {})) if hasattr(loaded, "geometry") else 1
    with zipfile.ZipFile(out) as z:
        print("zip_entries=" + ",".join(z.namelist()))
        xml = z.read("3D/3dmodel.model")
    root = ET.fromstring(xml)
    ns = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
    objs = root.findall(f"{ns}resources/{ns}object")
    items = root.findall(f"{ns}build/{ns}item")
    token_count = len(re.findall(rb"<object\b", xml))
    bad_colors = []
    for i, o in enumerate(objs, start=1):
        expected = str((i - 1) % 6)
        if o.get("pid") != "100" or o.get("pindex") != expected:
            bad_colors.append((o.get("id"), o.get("pid"), o.get("pindex"), expected))
    print(f"parts_in={len(parts)} roundtrip_geometries={loaded_count}")
    print(f"objects_xml={len(objs)} build_items={len(items)} object_tokens={token_count}")
    print(f"bad_colors={bad_colors}")
PY
```

Never use `grep -c '<object' 3D/3dmodel.model` on PrintForge 3MF XML. The model XML is
mostly one line, so `grep -c` counts lines, not objects. Use `grep -o '<object' | wc -l`
or XML parsing.

**What the numbers mean**

| Number | Interpretation |
|---|---|
| `parts_in == objects_xml == build_items` | Every connected component became one 3MF object and build item. |
| `object_tokens == objects_xml` | Text-token count agrees with XML parser. |
| `bad_colors=[]` | Every object has `pid="100"` and rotating palette `pindex`. |
| Round-trip loads as `Scene` | `trimesh` can read the archive back. Check geometry count against expected parts. |

**Worked example**

Executed here against the rendered `library/3e7accab949c` boat: `parts.py self-check OK`;
temporary 3MF round-trip loaded as `Scene`, `geometries=1`, `parts_in=1`, `objects=1`,
`object_tokens=1`, `bad_colors=[]`.

---

## 7. Prove a mechanism claim in an investigation

**When to use**

Use this when explaining a failure. Do not stop at a plausible cause. The accepted bar is:
one mechanism explains all observations, and competing mechanisms fail at least one
observation.

**Exact procedure**

Create an observation table before diagnosing:

| Observation | Must be explained? | Evidence command |
|---|---|---|
| Slivers or collapsed parts | yes | mesh diff, component count, bbox/section proof |
| Floating fragments | yes | `floating_starts()` and component split |
| Bambu 3MF rejection | yes | export XML count, round-trip load, component/floating proof |
| Prompt/code change that introduced it | yes | `git show`, `rg`, or SCAD diff |

Then test candidate mechanisms:

```sh
git show --stat --oneline <suspect-commit>
rg -n 'scale\(\[1,1|projection\(\)|clipped_to_base_footprint|floating_starts|write_3mf' \
  prompts.py app.py parts.py library/*/model.scad
```

Accept a root cause only if it explains every required observation without adding
contradictions.

**Worked example: z-scale diagnosis**

One mechanism explained all observations: the footprint recipe used `scale([1,1,big])
import(...)`, which stretches the base mesh's bottom slice instead of computing a true
top-view outline. That mechanism explains why features were clipped to slivers, why
disconnected/floating fragments appeared, and why Bambu rejected the export. The settled
fix was to ban that recipe in rule 14b, keep base additions inside verified
cross-sections, and reserve `linear_extrude(h) projection() import(path)` for rare true
outline needs. The original failure artifact is not executed here - re-verify before
changing the rule.

---

## When NOT to use this skill

| Need | Use instead |
|---|---|
| You are editing, deploying, or changing behavior | `printforge-change-control` |
| You need symptom triage before choosing a proof | `printforge-debugging-playbook` |
| You need the incident timeline and settled/root-cause status | `printforge-failure-archaeology` |
| You need OpenSCAD syntax, CSG semantics, or prompt-rule details | `printforge-openscad-reference` |
| You need mesh/3MF/STEP domain background beyond these recipes | `printforge-mesh-geometry-reference` |
| You need broad validation policy and golden inventory discipline | `printforge-validation-and-qa` |
| You need systemd, endpoint, or artifact-map operations | `printforge-run-and-operate` |
| You are working on Hunyuan/organic mesh quality | `printforge-organic-quality-campaign` |
| You are evaluating new research directions | `printforge-research-frontier` or `printforge-research-methodology` |

---

## Provenance and maintenance

Re-run these one-liners when the repo drifts:

```sh
# Mesh diff, QA cameras, and changed-region wording
nl -ba app.py | sed -n '303,440p'

# Lock verification and parameter diff endpoint
nl -ba app.py | sed -n '44,91p'; nl -ba app.py | sed -n '480,511p'; nl -ba app.py | sed -n '929,948p'

# Floating-start thresholds and 3MF writer/color metadata
nl -ba parts.py | sed -n '19,120p'

# Prompt contract rule numbers, z-scale ban, emboss recipe, clearance rules
nl -ba prompts.py | sed -n '1,80p'; nl -ba prompts.py | sed -n '127,190p'

# Profile clearance constants, calibration coupon, assembly validation thresholds
nl -ba app.py | sed -n '219,290p'; nl -ba app.py | sed -n '894,925p'; nl -ba app.py | sed -n '1439,1481p'

# Current feature/export claims in README
nl -ba README.md | sed -n '41,74p'; nl -ba README.md | sed -n '99,110p'

# Historical commits cited by the worked examples
git show --stat --oneline 633a536 d7431c2 135463a ee7391c 1c6854d 1461eb2 747a568 14c80b1 a48c8aa

# Safe executable self-check for export machinery
UV_CACHE_DIR=/tmp/uv-cache uv run --with trimesh --with networkx --with lxml python parts.py
```

Claims labeled "not executed here - re-verify" must stay labeled until someone identifies the
exact artifact and reruns the command against it.
