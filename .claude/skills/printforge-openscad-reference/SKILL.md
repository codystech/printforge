---
name: printforge-openscad-reference
description: >
  OpenSCAD and CSG runbook for PrintForge as implemented here. Load when reading,
  writing, reviewing, or hand-fixing any .scad file; when editing prompts.py prompt
  rules, ARCHETYPES, customizer variables, base-mesh import recipes, textmetrics,
  color-zone or multi-part rules; or when reasoning about generated geometry that is
  wrong, missing, buried, floating, slivered, clipped, invisible in render, bad after
  scale([1,1,big]) import(...), wrong on vertical emboss, wrong after -D slider
  overrides, missing sliders, negative defaults, union/difference/intersection mistakes,
  assembled_preview, *_enabled toggles, import(), projection(), linear_extrude(), or
  OpenSCAD render errors.
---

# PrintForge OpenSCAD Reference

Use this as the PrintForge-specific OpenSCAD/CSG pack, not a general CAD textbook.
Treat `prompts.py` as executable law: a wrong recipe there becomes a factory for broken
models. Any behavior-changing edit to `prompts.py`, `app.py`, or validation thresholds
must route through `printforge-change-control`.

Definitions:

- OpenSCAD: a declarative CAD language. You describe solids with code; OpenSCAD renders
  the final mesh.
- CSG, or constructive solid geometry: building solids from boolean operations:
  `union()` adds solids, `difference()` subtracts later children from the first child,
  and `intersection()` keeps only overlapping volume.
- Body: a connected printable solid in the STL.
- Base mesh: an uploaded STL/3MF/OBJ/GLB/STEP converted to STL and referenced with
  `import("<absolute path>")`.
- FDM: filament printing. In this app, assume Z-up millimeters, a flat XY-bed base, and
  print physics first.

## When NOT to use this skill

| Task | Use instead |
|---|---|
| Changing repo behavior, deploying, restarting, editing live service files, or changing prompt law | `printforge-change-control` |
| Debugging endpoint failures, service outages, network issues, import failures, or command failures | `printforge-debugging-playbook` |
| Understanding historical failures in depth | `printforge-failure-archaeology` |
| Working on trimesh, 3MF, STEP, GLB, slicing, mesh math, `parts.py`, or collision internals | `printforge-mesh-geometry-reference` |
| Rebuilding Nix, Docker, Python, OpenSCAD, or organic environments | `printforge-build-and-env` |
| Operating the running app, API endpoints, systemd, library hygiene, or Bambuddy | `printforge-run-and-operate` |
| Selecting evidence standards or validating a generated model | `printforge-validation-and-qa` |
| Measuring or extending diagnostic scripts | `printforge-diagnostics-and-tooling` |

## Minimal OpenSCAD mental model

Checklist for reading or writing PrintForge SCAD:

- Use millimeters. Z is up. The print bed is the XY plane and printable parts should start
  at `z=0` unless intentionally arranged otherwise.
- OpenSCAD is declarative: order inside `union()` does not make one object "paint over"
  another. Boolean volume decides what exists.
- `union() { a(); b(); }` returns the outside surface of the combined volume. Geometry
  fully buried inside another solid leaves no visible triangles.
- `difference() { base(); cutter(); }` subtracts every later child from the first child.
  Use it only when cutting, engraving, hollowing, or intentionally reshaping.
- `intersection() { a(); b(); }` keeps only shared volume. It is a clip, not a layout tool.
- Transforms compose from outside to inside. In
  `translate([10,0,0]) rotate([0,0,90]) cube([1,2,3]);`, the cube is rotated first, then
  translated.
- `mirror([1,0,0])` flips across the YZ plane. `rotate([90,0,0])` rotates around X.
- A `module name(args) { ... }` is a reusable shape function. Use modules for every
  repeated feature and every distinct part.
- `linear_extrude(h) shape2d();` turns 2D geometry into height `h` along Z.
- `projection() import("base.stl")` makes the XY silhouette of the full imported mesh;
  `linear_extrude(h) projection() import(path)` is correct but can be slow.
- `import(path)` brings in mesh or SVG geometry. For PrintForge base meshes, use the exact
  absolute path from the mesh note.
- `$fn = 64;` is the prompt-contract default for smooth circles and cylinders.

## Customizer contract

The app's customizer is not OpenSCAD's full customizer. It is exactly the regex parser in
`app.py`.

Write top-of-file parameters before geometry:

```scad
width = 60; // [20:150]
clearance = 0.2; // [0.1:0.05:0.6]
label = "CODY"; // free text
$fn = 64;
// --- model ---
```

Rules:

- Numeric sliders must be `name = default; // [min:max]` or
  `name = default; // [min:step:max]`.
- Text inputs must be `name = "text"; // free text`.
- Put the separator line `// --- model ---` after variables. The parser does not require
  it, but the prompt contract does.
- Use `\w+` names only: letters, digits, and underscores. This also matches render
  override validation.
- Do not emit reports, assumptions, warnings, or status strings as string parameters.
  Rule 7b says parameters exist only for user-adjustable geometry.

Exact parser as of 2026-07-06 (`app.py:46-48`):

```python
r'^(\w+)\s*=\s*([\d.]+|"[^"]*")\s*;\s*//\s*(?:\[([\d.:\-]+)\]|(free text))'
```

What silently fails to become a slider:

| SCAD line | Result | Why |
|---|---|---|
| `x = 1.5; // [-5:0.5:5]` | slider | Positive default matches `[\d.]+`; negative range bounds are allowed in `[\d.:\-]+`. |
| `x = -1.5; // [-5:0.5:5]` | no parameter | The default value alternation is `[\d.]+` with no minus sign. |
| `x = 1 + 2; // [0:10]` | no parameter | Expressions are not accepted as defaults. |
| `x = .5; // [0:1]` | slider | `.` and digits match, then `float(".5")` parses. |
| `label = "abc"; // free text` | text input | Quoted string plus exact `free text`. |
| `label = "abc"; // [0:10]` | text input | Any quoted default is treated as text even if a range exists. |

Render overrides use OpenSCAD `-D` exactly as `render_stl()` builds them
(`app.py:179-185`):

```sh
openscad $OPENSCAD_ARGS -o /tmp/out.stl --export-format binstl \
  -D width=80 \
  -D 'label="HELLO"' \
  /tmp/model.scad
```

The app rejects override keys that do not match `\w+`. Numeric values are passed as
strings like `80`; text values are wrapped as OpenSCAD strings.

## Prompt contract, with rationale

Source of truth: `prompts.py` `SYSTEM_PROMPT`, re-verify there before editing. Rule
numbers below are current as of 2026-07-06. Base-mesh fusion rules are rule 14
(`14a`-`14f`): the z-scale footprint ban is `14b`, and the verified vertical-face
emboss recipe is `14f`.

| Rule | Enforce | Rationale and incident anchor |
|---|---|---|
| 1 | Output only OpenSCAD code. | The server renders the returned text directly. Markdown fences are stripped defensively, but prose is still wrong input. |
| 2 | Every tunable dimension is a top parameter with range comment; strings use `// free text`. | This is how sliders/text inputs are discovered by `PARAM_RE`; nonmatching lines are invisible to the UI. |
| 3 | Put `// --- model ---` after variables. | Keeps the file easy for humans and smaller models to navigate before hand-fixing. |
| 4 | FDM constraints: flat XY base, Z=0 up, no floating parts, avoid overhangs over 45 degrees, min wall 1.2 mm. | PrintForge validates print physics, not just visual render. Floating-region failures caused slicer rejection. NOTE: the injected printer profile (`min_wall: 2.0` in `DEFAULT_PROFILES`) is a hard constraint that OVERRIDES this 1.2 at generation time — 2.0 binds unless a profile says otherwise. |
| 5 | Units are mm, `$fn = 64`, `textmetrics()` is available. | The host path uses `nixpkgs#openscad-unstable`; text sizing should use actual metrics, not guessed character width. |
| 6 | Use modules and keep models simple. | Modules are the unit of hand-fixing, locking, suppression, and feature preservation. |
| 7 | For code modification requests, return the complete updated file. | Required for fallback LLM paths. Primary codex refines edit in place to avoid the feature-loss rewrite incident. |
| 7b | Parameters only for adjustable geometry, never status prose. | The app measures/report models itself. Prose parameters pollute the UI and cannot validate geometry. |
| 8 | Multi-part designs: one module per part; lay parts side by side with 10 mm gaps in print orientation; 0.2 slip, 0.4 loose clearance. | The user splits parts in the slicer. Clearances come from prompt law and profile defaults. |
| 9 | Multi-color designs: each color zone is its own separate body laid beside the base. | Slicers assign filament per body; color is not reliable geometry metadata here. |
| 10 | Gridfinity: 42 mm pitch, 7 mm height units, 41.5 mm bin footprint per cell, 4.75 mm base profile with 0.8 mm corner radius, optional 6x2 mm magnets. | Prevents plausible-looking but incompatible bins. (Rule text verbatim; the ARCHETYPES entry adds the `n*42-0.5` multi-cell formula and calls the 0.8 mm a bottom chamfer.) |
| 11 | Print-in-place mechanisms: 0.5 mm moving clearance, assembled position, no support-needing internal overhangs. | Moving surfaces fuse unless clearance is larger than normal slip fits. |
| 12 | Reference image: reproduce shape/proportions as printable inferred-mm geometry. | Image input is reference, not texture; it must become real solids. |
| 13 | Assembly discipline: every distinct part gets `<part>_enabled = 1; // [0:1]`; top-level calls are guarded; add `assembled_preview = 0; // [0:1]`. | Parts panel, lock/suppress, `/validate`, and assembled print report depend on toggles and a real assembled position. |
| 14 | Base mesh rule: build with `import(path)`, keep added-feature parameters, use bbox and cross-sections, and follow fusion rules 14a-14f. | This is the settled response to base-mesh incidents: z-scale slivers, buried sails, global-top placement errors, and emboss orientation failures. |

Clearance table used here (this skill owns the fit SEMANTICS; the numeric
constants are canonical in printforge-config-and-flags / `DEFAULT_PROFILES`,
`app.py:219-234` — on disagreement, those win):

| Fit type | Value | Source |
|---|---:|---|
| Slip fit | 0.2 mm | `prompts.py` rule 8; `DEFAULT_PROFILES["fit_clearance"]` |
| Loose fit | 0.4 mm | `prompts.py` rule 8; `DEFAULT_PROFILES["loose_clearance"]`; `/validate` warns below 0.4 mm |
| Print-in-place moving surfaces | 0.5 mm | `prompts.py` rule 11 |
| Snap-fit clearance | 0.15 mm | `DEFAULT_PROFILES["snap_clearance"]`; `/validate` marks gaps below 0.15 mm as touching |

## CSG traps and settled recipes

Incident story details in this section are historic and were not re-executed here -
re-verify from project history before using them as forensic evidence. The current prompt
rules, app guardrails, and the small `/tmp` OpenSCAD render checks were verified in this
authoring pass.

### Buried sail: `union()` erases interior geometry

If a requested thin addition is inside an existing solid, `union()` does not preserve a
visible internal part. It removes internal faces and the feature vanishes. This caused a
sail to disappear when it was extruded into a cabin.

Do this:

- Place thin/planar additions so they project into open air.
- Check the uploaded mesh cross-sections before choosing the side and direction.
- Give every requested added element its own visible added-geometry region during review.

Do not do this:

```scad
union() {
    import(base_path);
    translate([x, y, z]) cube([thin, wide, tall]); // buried inside cabin volume
}
```

### Z-scale footprint trap

Never clip to an imported mesh footprint with:

```scad
scale([1,1,1000]) import(path);
```

Why: z-scaling stretches the bottom slice of the mesh, not the XY outline. On tapered or
irregular bases, it collapses added features into slivers. This caused the boat
flag/chest/cleat failure and a rejected 3MF.

Correct but slow:

```scad
linear_extrude(h) projection() import(path);
```

Better for most PrintForge base-mesh jobs: use the provided cross-section extents and
pick coordinates that are inside the body at that height.

Executed check in this authoring pass: a tapered base with 10 mm bottom diameter and
40 mm top diameter rendered as 10.01 mm wide under the z-scale mask and 40.0 mm wide
under `linear_extrude(2) projection() import("base.stl")`.

### Features must start inside the body

The uploaded mesh note includes five horizontal cross-sections at 10%, 30%, 50%, 70%,
and 90% of mesh height (`app.py:648-659`). Use them. `bbox_max.z` is the global top, not
the local surface. Thin plates and tall bosses make global top placement wrong.

Do this for raised features on a base mesh:

- Choose the local surface side and height from cross-sections.
- Start the feature inside the body, often at or below mid-height or a few millimeters
  below the local surface.
- Extrude through the local surface by the requested raise.

### Verified vertical-face raised text recipe

Use this recipe verbatim for raised text on a vertical side of an imported mesh. Never
derive your own rotation algebra.

```scad
// readable from +Y; y_start just OUTSIDE the surface, depth crosses INTO the body
module label_plus_y(txt, sz, x, z, y_start, depth)
    translate([x, y_start, z]) rotate([90,0,0]) mirror([1,0,0])
        linear_extrude(depth)
            text(txt, size=sz, halign="center", valign="center");
// -Y side: mirror([0,1,0]) label_plus_y(...);
// X-facing sides: rotate([0,0,90]) or ([0,0,-90]) around the whole construct.
```

Pick `y_start = (surface y at that height, from the cross-sections) + raise`, and
`depth = raise + at least 4` so text crosses into the curved surface and fuses.

Executed check in this authoring pass: the +Y recipe rendered a valid STL for a box with
raised text extending to `y=8.8` from an 8 mm-deep body.

### `textmetrics()` and OpenSCAD version/flags

The contract tells the LLM to use `textmetrics()`. The host path must use a 2024+ style
OpenSCAD from `nixpkgs#openscad-unstable` and pass `--enable=textmetrics` through
`OPENSCAD_ARGS` (`app.py:34`; `run.sh` enters the nix shell).

As of 2026-07-06, Docker deliberately blanks `OPENSCAD_ARGS` because Debian OpenSCAD is
2021.01 (`compose.yaml`). Old OpenSCAD/textmetrics behavior is a settled incident: it can
produce garbage geometry without hard failing. OpenSCAD 2021.01-specific behavior was
not executed here - re-verify. In this authoring pass, the nix unstable binary rendered a
textmetrics-sized STL with `--enable=textmetrics`; without the flag it warned and still
emitted a 1x1x1 STL. Re-verify in your environment before claiming a specific OpenSCAD
build's behavior.

## Archetype and hardware dimensions

Source of truth = `prompts.py` `ARCHETYPES` - re-verify there. Values below are current
as of 2026-07-06.

| Trigger | Verified numbers and constraints |
|---|---|
| Gridfinity | 42.0 mm grid pitch; bin footprint `n*42-0.5` mm; heights in 7 mm units; base profile about 4.75 mm tall; 0.8 mm bottom chamfer; stacking lip mirrors base profile. |
| Print-in-place hinge | Barrel at least 8 mm diameter; pin-to-barrel clearance 0.5 mm; print axis horizontal along bed; 45-degree teardrop tops to barrel holes. |
| Clips/hooks/clamps | Flex fingers 1.6-2.4 mm thick along bend direction; opening 0.5-1 mm smaller than held object; fillet flex root; orient layers along flex direction. |
| Signs/plaques/logos | Base plate 3-4 mm; raised art 1.2-2 mm; separate part per color zone; text at least 6 mm tall for 0.4 mm nozzle. |
| Boxes/enclosures | Walls at least 2 mm; lid lip 1.5-2 mm with 0.2 mm clearance; inside corners filleted; screw posts 2x screw diameter with 0.2 mm pilot clearance. |
| Raspberry Pi B-series 3/4/5 | Board 85x56 mm; four M2.5 holes on 58x49 mm grid; holes 3.5 mm from corners; standoffs 6 mm tall, 6 mm OD; 2.2 mm pilot for M2.5 screws or 2.7 mm clearance for inserts; leave 20 mm above headers/HAT; ports overhang 85 mm edge by about 2 mm. |
| 40 mm fan / 4010 | Body 40x40x10 mm; holes 32x32 mm spacing; 4.3 mm diameter for M3 screws/self-tappers; airflow opening 38 mm diameter; grill open area over 50%. |
| Heat-set inserts | M3: 4.0 mm hole, 5.8 mm deep, boss at least 7 mm OD. M2.5: 3.4 mm hole, 5.0 mm deep, boss at least 6 mm OD. Add 0.3 mm entry chamfer; keep at least 2 mm from wall edge. |
| Standoffs/bosses/screw posts | OD at least 2x screw diameter; pilot = screw diameter - 0.4 mm for thread-forming; 0.5 mm base fillet; embed/fuse into floor by at least 1 mm. |
| Countersunk screws | M3: 3.4 mm through-hole plus 82-degree cone to 6.5 mm, head 0.2 mm below surface. M4: 4.5 mm hole, cone to 8.5 mm. |
| Magnet pockets | Diameter = magnet + 0.2 mm; depth = magnet height + 0.1 mm; hidden magnets leave 0.4-0.6 mm cover skin. |
| Zip-tie channels | 5.5x3 mm rectangular tunnel with rounded entries; minimum 2 mm wall around; route perpendicular to mounting surface. |
| Keyhole hangers | 8 mm entry circle; 4.5 mm wide x 12 mm slot; 3.5 mm deep cavity behind a 2.5 mm-deep face slot; orient slot upward. |
| Pegboard | 6.0 mm pegs; 25.4 mm hole grid; 90-degree hook bend; 8-10 mm engagement behind board; add top-rear support nub. |
| Dovetails | 8-10 degree flank angle; 0.2 mm sliding clearance; neck at least 6 mm; stop shoulder at one end. |
| Snap tabs | Arm length at least 8x arm thickness; arm 1.6-2.4 mm thick; catch depth 0.8-1.2 mm; 30-45 degree lead-in ramp; about 90-degree retention face; layers along arm. |
| Cable glands | PG7 = 12.5 mm hole; PG9 = 15.2 mm hole; wall boss at least 3 mm thick around hole; printable slit gasket alternative has cone, cable bore, and 1 mm slit. |

## Deterministic hand-fix workflow

Use this when QA has already had about two rounds or the same geometry mistake is
recurring. Do not send POST/PUT/PATCH/DELETE to the live service for this workflow. Work
on a `/tmp` copy, render locally, then route any project behavior change through
`printforge-change-control`.

1. Copy the SCAD to a scratch directory. Never edit `library/` in place.

```sh
mkdir -p /tmp/printforge-handfix
cp /home/cody/projects/printforge/library/<12-hex-id>/model.scad /tmp/printforge-handfix/model.scad
```

2. Find the relevant module or parameter, then edit only that target module.

```sh
rg -n "module |assembled_preview|_enabled|label_plus_y|import\\(|difference\\(|intersection\\(" /tmp/printforge-handfix/model.scad
```

3. Render the print layout locally with the same command anatomy as `render_stl()`:
   `openscad`, `OPENSCAD_ARGS`, `-o`, `--export-format binstl`, optional `-D k=v`,
   then the SCAD file.

```sh
cd /tmp/printforge-handfix
HOME=/tmp/printforge-nix-home XDG_CACHE_HOME=/tmp/printforge-nix-cache \
  nix shell nixpkgs#openscad-unstable --command openscad \
  --enable=textmetrics --enable=manifold \
  -o /tmp/printforge-handfix/model.stl \
  --export-format binstl \
  /tmp/printforge-handfix/model.scad
```

4. Render assembled mode when the model has `assembled_preview`.

```sh
cd /tmp/printforge-handfix
HOME=/tmp/printforge-nix-home XDG_CACHE_HOME=/tmp/printforge-nix-cache \
  nix shell nixpkgs#openscad-unstable --command openscad \
  --enable=textmetrics --enable=manifold \
  -o /tmp/printforge-handfix/model-assembled.stl \
  --export-format binstl \
  -D assembled_preview=1 \
  /tmp/printforge-handfix/model.scad
```

5. For parameter-specific failures, pass overrides exactly like the app does.

```sh
cd /tmp/printforge-handfix
HOME=/tmp/printforge-nix-home XDG_CACHE_HOME=/tmp/printforge-nix-cache \
  nix shell nixpkgs#openscad-unstable --command openscad \
  --enable=textmetrics --enable=manifold \
  -o /tmp/printforge-handfix/model-param.stl \
  --export-format binstl \
  -D clearance=0.4 \
  -D 'label="CODY"' \
  /tmp/printforge-handfix/model.scad
```

If Nix fails with a read-only cache error, keep `HOME` and `XDG_CACHE_HOME` redirected to
`/tmp` as shown. If Nix or OpenSCAD still cannot run, mark the render claim
"not executed here - re-verify"; do not guess.

## Provenance and maintenance

Re-verify drift-prone claims with one-liners:

- Prompt rules and rule numbers: `cd /home/cody/projects/printforge && nl -ba prompts.py | sed -n '1,124p'`
- Archetype table: `cd /home/cody/projects/printforge && nl -ba prompts.py | sed -n '127,193p'`
- Customizer regex and parse behavior: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '42,91p'`
- Render command construction and `-D` override handling: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '175,194p'`
- `OPENSCAD_ARGS`, host nix shell, and Docker blank args: `cd /home/cody/projects/printforge && rg -n "OPENSCAD_ARGS|openscad-unstable|textmetrics" app.py run.sh compose.yaml README.md`
- Mesh cross-sections and base-mesh note: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '648,856p'`
- Clearance defaults and `/validate` thresholds: `cd /home/cody/projects/printforge && nl -ba app.py | sed -n '219,281p' && nl -ba app.py | sed -n '1438,1481p'`
- Local OpenSCAD availability/version: `HOME=/tmp/printforge-nix-home XDG_CACHE_HOME=/tmp/printforge-nix-cache nix shell nixpkgs#openscad-unstable --command openscad --version`
- Z-scale trap render proof: create a tapered `cylinder(h=10, r1=5, r2=20)` in `/tmp`, render `intersection(){translate([-25,-25,0]) cube([50,50,2]); scale([1,1,1000]) import("base.stl");}` and compare to `linear_extrude(2) projection() import("base.stl")`; expected widths are about 10 mm vs 40 mm.
- Textmetrics flag proof: render a `/tmp` SCAD using `m = textmetrics("PF", size=8); cube([m.size[0]+2,m.size[1]+2,1]);` with and without `--enable=textmetrics`; re-check whether your OpenSCAD build hard-fails, warns, or emits garbage geometry.
