---
name: printforge-architecture-contract
description: >-
  The load-bearing design contract for PrintForge — WHY it is built the way it
  is, the invariants that must stay true, and the known-weak points stated
  plainly. LOAD THIS BEFORE any structural change: adding or altering an
  endpoint, adding a pipeline stage (generate/QA/render/validate/organic),
  touching PARAM_RE or the customizer-comment format, changing how refines edit
  files, changing meta.json/library/uploads layout, touching profiles or the
  LLM backend wiring, or the GPU/organic path. Also load when answering "why is
  it built this way?", "can I just rewrite X?", "why filesystem instead of a
  DB?", "why does it edit in place?", or "is it safe to change the param regex?"
  Triggers: app.py, prompts.py, parts.py, /generate, /render, /organic,
  /validate, PARAM_RE, meta.json, library/, uploads/, LAST_BACKEND, WORK_DIR,
  call_codex_edit, vision_qa, floating_starts, lock_violations, part_state.
---

# PrintForge architecture contract

This is the map a new maintainer needs before touching structure. It states the
data flow, the decisions that are load-bearing (with the reason they exist —
several were paid for in incidents), the invariants a reviewer can check, and
the weak points, each labelled **accepted-tradeoff** or **needs-fixing**.

Read this to understand *why*. To actually change behaviour, route through
**printforge-change-control** (gates + deploy protocol). For OpenSCAD/CSG
specifics use **printforge-openscad-reference**; for mesh/3MF/STEP internals use
**printforge-mesh-geometry-reference**; for the deterministic checks and golden
evidence standards use **printforge-validation-and-qa**.

Ground truth: `app.py` (~1491 lines), `prompts.py`, `parts.py`, `organic/`,
`static/index.html`, `run.sh`, `compose.yaml`. Verified against the repo
2026-07-06. Line numbers drift — re-verify with the commands in *Provenance*.

---

## 1. System map — request → response

PrintForge is one FastAPI app (`app.py`) serving a vanilla-JS single-page app
(`static/index.html`, mounted at `/`). No database: the filesystem is the store.
Three persistence roots plus one scratch root:

- `library/<12-hex-id>/{model.scad, meta.json, thumb.png}` — saved models (USER DATA).
- `uploads/<12-hex-id>.{stl,json[,step,svg]}` — imported/traced base meshes.
- `training_lab_data/` — feature-gated evolution runs, immutable candidate
  artifacts, checkpoints, lineage and events. It never writes candidates into
  `library/` automatically.
- `WORK_DIR = /tmp/printforge` — every render, PNG, codex job, export. Ephemeral;
  wiped on reboot. `stl_id` is a 32-hex handle into here (distinct from the 12-hex
  library id).

### /generate — the core pipeline (app.py:987)

`/generate` has **two paths** decided by whether `current_scad` is present AND
`LLM_BACKEND == "codex"`:

```
POST /generate {prompt, current_scad?, image?, mesh_ids?, parent_id?, profile?}
   │
   ├─ decode image data-URL → WORK_DIR/ref-*.png            (if image)
   ├─ if refine+codex: render current model → iso+top PNGs  (LLM edits with eyes open)
   ├─ mesh_note   = _all_mesh_notes(mesh_ids)   (bbox + cross-sections + role per mesh)
   ├─ prof,override = resolve_profile(profile, prompt)  → profile_block (hard constraints)
   │
   ├─ PATH A — REFINE (current_scad present, codex backend):        app.py:1015
   │     floating_starts(current) → "seat these" note
   │     call_codex_edit(current_scad, SYSTEM_PROMPT+notes+intent+rules+part_state)
   │         → codex EDITS model.scad IN PLACE (workspace-write), never reprints
   │     lock_violations(old,new,part_state) → one forced restore round if locks touched
   │     render_stl(scad) → on error, one codex "fix it" round → render again
   │
   ├─ PATH B — FRESH (no current_scad, or non-codex backend):       app.py:1062
   │     messages=[system=SYSTEM_PROMPT+archetype+profile+presets+taste_example,
   │               user=user_prompt(...)]
   │     call_llm(messages, images)  (codex exec, or HTTP fallback to local qwen)
   │     render_stl(scad) → on error, retry once with error fed back
   │
   ├─ QA LOOP (codex + QA_CHECK only, up to QA_ROUNDS=2):            app.py:1082
   │     base_stl = render(current_scad) for refines  (diff vs PREVIOUS, not upload)
   │     vision_qa(): mesh_changes → per-cluster oblique close-ups + floating_starts
   │                  + intent/rules/part_state/profile notes → call_codex_edit (review)
   │                  render fix; status passed|fixed|skipped; loop while "fixed"
   │
   ├─ save_to_library(prompt, scad, stl, intent+[prompt], parent_id) → 12-hex id
   ├─ print_report(stl or assembled_preview render, profile)  → bbox/weight/bed-fit
   ├─ meta.update(qa, backend=LAST_BACKEND, report, profile=SNAPSHOT, part_state)
   └─ asyncio.create_task(_autoname(...))   (best-effort rename via local model)
  → {scad, params(parse_params), stl_id, qa, model_id, backend, report,
     print_warnings, lock_violations, rules, part_state, profile_used, ...}
```

Both paths converge on: render-to-validate → optional QA → save → report → return.
The response carries `scad` + parsed `params` so the UI can build sliders
immediately.

### /training-lab/api/runs — bounded evolution pipeline

Training Lab runs have two compatible modes. `evolve_existing` requires a safe
12-character Library ID and copies that model into isolated generation zero.
`create_from_spec` requires no source model: Start generates generation zero
from the validated specification, profile and locked requirements, then runs
the same deterministic evaluation before any refinement iteration.

Every attempt is created as a candidate record before backend work begins. Its
SCAD/artifacts, evidence, score, prompt, failure reason, timestamps and lineage
are stored separately; failed or cancelled generation-zero attempts remain
inspectable. Later iterations produce A/B children of the immutable current
best and only checkpoint a replacement when it improves the best without a hard
rejection. Restore and branch create new checkpoints/runs; they never overwrite
an artifact. Explicit deletion is blocked for generation zero, baselines,
current best candidates, and candidates with descendants.

The loop is bounded by iteration count, runtime, backend calls/cost, repeated
generation failures, no improvement, optional quality target, required checks,
graceful stop, or immediate cancellation. Lab Codex calls use their own process
group so cancellation can terminate the subprocess while retaining the partial
candidate record. Legacy requests without `run_mode` still mean
`evolve_existing`; `source_model_id` is nullable only for `create_from_spec`.

### /render — slider re-render (app.py:1149)

```
POST /render {scad, params} → render_stl (openscad -D k=v per param) → {stl_id}
```
Pure geometry, no LLM. Param names are regex-guarded (`\w+`, app.py:181) before
they reach the shell. This is what fires every time a user drags a slider.

### /organic — image → sculpted mesh on the 3090 (app.py:1278)

```
POST /organic {image, target_mm} → 503 if organic/.venv absent
   async with _organic_lock:          # serialize: never share the GPU
       _free_gpu()                     # ask ollama to unload the brain first
       subprocess organic/.venv/bin/python generate.py  (Hunyuan3D-2mini, LD_LIBRARY_PATH=ORGANIC_LIBS)
   _register_mesh(stl) → import(...) wrapper scad → {id, stl_id, scad, params}
```
Output is a base mesh you then refine through `/generate` like any upload — the
organic↔parametric hybrid. ~90s inference; the lock + `_free_gpu` are the whole
concurrency story for the single shared 3090.

### /validate — assembly collision check (app.py:1438)

```
POST /validate {scad, params} → 400 if no *_enabled toggles
   render each part alone in assembled position (assembled_preview=1)
   pairwise: manifold3d boolean intersection >0.5mm³ → COLLISION
             else closest_point gap <0.15 TOUCHING / <0.4 TIGHT FIT
   → {parts, assembled_check, issues}
```
Deterministic, no LLM. Snap-fits are a known false positive (see weak points).

### upload / import (app.py:686, 739)

```
POST /upload-mesh (multipart, trace?)  → _trace_to_svg (magick+potrace) if trace
POST /import-url  {url}                 → Printables GraphQL (keyless) / Thingiverse (token)
   → _register_mesh: validate ext ∈ MESH_EXTS, STEP→GLB via cascadio (×1000, Y→Z-up,
     named-body extraction), export canonical .stl, compute 5 cross-sections + bbox,
     write uploads/<id>.json  → mesh meta
PATCH /uploads/<id> {role}  → printable|reference|fit_cutout|assembly|negative
```
The mesh's `role` decides whether its geometry appears in printable output — the
`reference`/`negative`/`fit_cutout` roles are measured but never printed
(role notes, app.py:812).

---

## 2. Load-bearing decisions (with the reason they exist)

**Single-file FastAPI + vanilla JS + vendored three.js, no build step.** `app.py`
is the whole server; `static/index.html` is the whole client with three.js
vendored inline. Deliberate: a 1–2-user self-hosted tool must be deployable by
`edit files → systemctl --user restart printforge` with zero toolchain. There is
no bundler, no npm, no transpile. **Do not introduce a build step** — it breaks
the deploy story and buys nothing at this scale.

**Filesystem-as-database.** `library/<id>/` dirs with `meta.json`, `uploads/<id>.*`,
scratch in `WORK_DIR`. Chosen because scale is 1–2 users and the artifacts are
already files (`.scad`, `.stl`, `.png`). A DB would add a dependency and a
migration surface for no query needs `iterdir()` + JSON can't serve. The cost is
paid in the weak points below (no locking, no transactions) — accepted at this
scale, not forever.

**`PARAM_RE` is THE contract between LLM output and the UI.** (app.py:46)
```python
PARAM_RE = re.compile(
    r'^(\w+)\s*=\s*([\d.]+|"[^"]*")\s*;\s*//\s*(?:\[([\d.:\-]+)\]|(free text))',
    re.MULTILINE,
)
```
The LLM is instructed (SYSTEM_PROMPT rule 2) to emit every tunable as
`name = default; // [min:max]` or `// [min:step:max]`, strings as
`name = "v"; // free text`. `parse_params()` turns those comments into sliders and
text inputs. This regex is the *entire* coupling between generated code and the UI,
`/validate` (it finds `*_enabled` toggles), `assembled_preview` detection, and the
diff endpoint. **Every one of the ~53 stored models today was written to match this
exact format.** Changing the regex — even "loosening" it — silently drops params
from every model that no longer matches, turning sliders into nothing. If you must
change it, it is a data-migration event across all of `library/`, gated through
change-control, not a quick edit.

**Refines EDIT the file in place; they never full-file-rewrite.** (call_codex_edit,
app.py:119; wired at app.py:1041) Codex runs `-s workspace-write` on a scratch copy
of `model.scad` and makes minimal edits with its file tools. This invariant was
*born from an incident*: asking any LLM to re-print a ~400-line file reliably
dropped unrelated modules (a robot, a wheel, a chest — twice). A size-guard was
not enough because QA rounds also re-printed. The prompt hard-forbids rewriting
from scratch. **Never reintroduce whole-file rewrite for refines or QA.**

**QA = advisory LLM reviewer over a DETERMINISTIC gate.** The deterministic layer
decides pass/fail geometry; the vision LLM judges intent and placement. Concretely:
`floating_starts()` (parts.py:67) flags mid-air features (slicer "floating regions"),
`lock_violations()` (app.py:499) proves locked modules survived byte-identical,
`/validate` uses manifold3d booleans for real collisions. These are numbers, not
vibes. The LLM in `vision_qa` looks at oblique close-ups and asks "is every
requested element present and sensibly placed?" — but the hard blocks come from the
deterministic checks. **The determinism gates; the LLM advises.** Do not replace a
deterministic check with "ask the model."

**QA diffs against the PREVIOUS accepted state, not the pristine upload.**
(app.py:1087 renders `current_scad` as the base_stl) Incident: QA kept "restoring"
a cabin the user had deliberately removed, because it diffed against the original
upload so every intentional change looked like damage. It once invented a support
pillar *inside* a hollow hull to satisfy "no floating parts." Fix: diff vs the last
state, and inject `intent` (accepted-decision lineage) + `rules` as "never revert
these." **Keep the base_stl = previous-state, not upload.**

**Oblique perspective QA cameras, never straight-on ortho.** (vision_qa render_png
`ortho=False`, app.py:419–425) Straight-on orthographic projection cannot show low
relief — embossed text that "verified present" in a flat render was absent on the
actual print, and this fooled the *maintainer's own eyes*, not just the LLM.
Close-ups of changed regions come first because a 10mm addition is a smudge in a
whole-model view.

**Profile is stored as a SNAPSHOT in meta, not a reference.** (app.py:1128,
`"profile": dict(prof)`) The generated geometry was designed against *those* wall
thicknesses, clearances and bed size. If meta stored only the profile *name* and the
user later edited that profile, the model's provenance would silently change and the
stored geometry would no longer match its recorded constraints. The snapshot freezes
"what this model was actually built for." A profile *change* on refine is detected
(app.py:1010) and flagged to the LLM to re-check walls/fit.

**Mesh notes ground the LLM with real geometry, not guesses.** `_register_mesh`
computes bbox + 5 horizontal cross-sections (app.py:648) and, for STEP, named CAD
body positions (app.py:619). `_mesh_note` feeds these so the LLM places features
"where the section shows material at that height" instead of at the global bbox top
(where it kept burying text inside curved hulls). Cross-sections are the antidote to
the z-scale footprint disaster — pick coordinates from them, never `scale([1,1,big])`.

**`WORK_DIR` ephemeral vs `library/`/`uploads/` persistent.** Renders, PNGs, codex
jobs, exports are disposable and live in `/tmp/printforge`; only accepted models and
source meshes persist. This is why a dropped HTTP client doesn't lose work (auto-save
lands in `library/` regardless) and why a reboot costs nothing but scratch.

**`_organic_lock` + `_free_gpu` for exclusive 3090 use.** (app.py:1254, 1266) One
GPU, shared with the local "brain" (ollama). The `asyncio.Lock` serializes organic
jobs so two never run at once; `_free_gpu` asks ollama to unload before inference so
VRAM is free. This is the entire GPU-arbitration design — minimal on purpose.

**Blocking subprocesses run via `asyncio.to_thread`.** openscad renders, codex
calls, trimesh math, `floating_starts` are all synchronous and slow; wrapping them in
`to_thread` keeps the single-process async server responsive while a 5-minute codex
generation runs. This is why the event loop doesn't stall during generation.

**`LLM_BACKEND=codex` primary, HTTP (local qwen) fallback.** (call_llm, app.py:151)
gpt-5.5 via `codex exec` writes competent multi-part designs; local qwen does not
(fine for simple brackets, inadequate for multi-part). Codex is primary; qwen is the
rate-limit lifeboat. **Image input hard-requires codex** (app.py:159) — the fallback
raises rather than silently degrade.

---

## 3. Invariants (a reviewer can check each of these)

| # | Invariant | Where enforced / checked |
|---|-----------|--------------------------|
| I1 | Every generated model's params parse with `PARAM_RE`; the UI only sees what `parse_params` returns. | app.py:46/78; SYSTEM_PROMPT rule 2 |
| I2 | Refines and QA **edit in place**, never re-print the whole file. | call_codex_edit app.py:119; prompt forbids rewrite |
| I3 | QA diffs against the **previous accepted state** (rendered `current_scad`), not the pristine upload. | app.py:1087–1091 |
| I4 | Locked modules survive a refine **byte-identical modulo whitespace**; a violation forces one restore round. | lock_violations app.py:499; forced round app.py:1046 |
| I5 | `reference` / `fit_cutout` / `negative` meshes are measured but their geometry **never appears as positive printed geometry** — `reference`/`fit_cutout` get a `*_preview_enabled` preview module; `negative` is only subtracted (no preview toggle); `assembly` IS printed. | role notes app.py:812–830 |
| I6 | Every id used in a filesystem path is validated before use: library/model/mesh/parent = 12-hex, stl scratch = 32-hex, param/part names = `\w+`. | app.py:181, 444, 793, 1156, 1200, 1318, 1424 |
| I7 | All secrets arrive via env (`BAMBUDDY_API_KEY`, `THINGIVERSE_TOKEN`); none inline. `.env` is gitignored, sourced by run.sh. | app.py:31,725; run.sh:7 |
| I8 | Generated `.scad` renders before it is saved; a render failure triggers exactly one automatic fix round, then propagates to the UI. | app.py:1055/1071 |
| I9 | The QA loop is bounded by `QA_ROUNDS` (default 2); it stops on `passed`/`skipped` or after the cap. | app.py:1093 |
| I10 | Organic generation holds `_organic_lock` for its whole run and frees the GPU first — never two GPU jobs at once. | app.py:1290 |
| I11 | `profile` is snapshotted into meta as a full dict, not a name reference. | app.py:1128 |
| I12 | Deterministic checks (floating_starts, lock_violations, /validate booleans) gate; the vision LLM only advises. | parts.py:67; app.py:499; app.py:1462 |
| I13 | Evolution candidates are versioned under `training_lab_data/`, never overwrite the current best, and every run has explicit finite stop controls. | evolution_lab/engine.py; evolution_lab/store.py |
| I14 | `source_model_id` is required for `evolve_existing`, nullable for `create_from_spec`, and omitted `run_mode` remains backward-compatible. | evolution_lab/schemas.py; evolution_lab/engine.py |
| I15 | Dormant `cadquery-v1` source is parsed with bounded AST/literal evaluation before any worker runs; untrusted worker gate claims are ignored and a trusted parent validator must derive B-rep validity, STEP export/round-trip, STL tessellation, existing mesh checks, build volume, locks and role leakage from captured files. SCAD aliases are legacy-only, missing source is explicit, and runtime readiness remains false until a dedicated worker exists. | evolution_lab/cadquery.py; evolution_lab/router.py |
| I16 | Dataset v2 rows fail closed on a store-hashed human provenance audit with explicit owned/licensed-for-training/public-domain rights, deterministic/slicer evidence and matching evaluator/profile fingerprints; every included parent is independently audited and siblings use the run-derived part-family split. Physical evidence uses a deterministic printable run/candidate/artifact tuple, remains pending until candidate/mutation/memory backlinks succeed, and exact replay is idempotent. Failed exact artifacts never enter SFT, and decisive opposite verified physical evidence vetoes preference rows. V1 exports remain available unchanged and selected by default. | evolution_lab/dataset_v2.py; evolution_lab/datasets.py; evolution_lab/router.py; evolution_lab/store.py |
| I17 | Bambu slicing runs only after every trusted CadQuery hard gate passes, uses immutable full machine/process/filament bytes plus a pinned binary identity in a networkless scratch sandbox, and treats missing/invalid/empty output or incomplete metrics as a hard rejection. Blocked candidates and evidence persist, but cannot become the restored best, a production exemplar, or a Bambuddy-deliverable candidate. Runtime readiness remains false until binaries, profiles, and a matching real smoke are all proven. | evolution_lab/slicer.py; evolution_lab/cadquery.py; evolution_lab/engine.py; evolution_lab/router.py |

If a change would break any of these, it is a structural change — take it through
**printforge-change-control** with evidence per **printforge-validation-and-qa**.

**Verify the id-validation invariant (I6) quickly:**
```bash
grep -nE "re\.fullmatch\(r" /home/cody/projects/printforge/app.py
```
Every filesystem-path-forming id should appear here before any `Path` join. If you
add an endpoint that takes an id and builds a path, it MUST fullmatch first.

---

## 4. Known-weak points (stated plainly)

Labelled **accepted-tradeoff** (a deliberate choice for a 1–2-user LAN tool) or
**needs-fixing** (a real latent bug/risk). The README "Later" list is the tie-break
for what's acknowledged.

| Weak point | Detail | Label |
|------------|--------|-------|
| Global `LAST_BACKEND` | Module-global string (app.py:148) records which model answered the *most recent* generation. With two real LAN users generating concurrently, user A can see "backend: codex" reported for user B's job. Single-user state in a two-user app. | **needs-fixing** (cosmetic, low blast radius; carry it in response body per-request instead) |
| `meta.json` read-modify-write, no locking | Every rate/rename/rules/parts/role/generate does read → mutate → write with no lock (e.g. app.py:1125, 1358, 1408, 1428). Two concurrent refines (or a refine racing a rename) on the same model can lost-update. No transactions in the filesystem store. | **needs-fixing** (real race; rare at 1–2 users, so unhit so far) |
| No auth on `0.0.0.0:8093` | Binds all interfaces, no authentication. Relies on the LAN-trust assumption. Anyone on the LAN can GET/POST/DELETE any model. | **accepted-tradeoff** (README "Later": deploy behind NPM/Authelia when it proves useful) |
| `WORK_DIR` unbounded growth | Every render/PNG/codex-job/export accretes in `/tmp/printforge` with no cleanup; only a reboot reclaims it. | **accepted-tradeoff** (tmpfs, self-clears on reboot; add a reaper only if disk pressure appears) |
| `profiles.json` / `presets.txt` are server-global | Custom profiles (app.py:868) and measured presets (app.py:960) are single shared files, but *profile selection is per-browser* (`localStorage pf_profile`, index.html:774). Two users share one custom-profile pool and one presets file even though each picks their own active profile. | **accepted-tradeoff** (1–2 trusted users; per-user stores would need auth first) |
| QA quality gated on one vision model | The whole QA judgement rests on codex/gpt-5.5's vision. If it's wrong or unavailable (QA runs codex-only), the advisory layer is blind — only the deterministic checks remain. | **accepted-tradeoff** (deterministic gate still holds the printability line; multi-model QA is not planned) |
| Organic mesh quality is v1 | Hunyuan3D-2mini works end-to-end but is first-generation quality; the runner is pluggable and Sparc3D/TRELLIS.2 evaluation is deferred. | **accepted-tradeoff** (README "Later"; the Phase-2 frontier — see **printforge-organic-quality-campaign**) |
| `/validate` flags snap-fits | Intentional snap-fit interference (~12mm³, operator-reported) is reported as a collision — correct geometry, flagged anyway. "Teach the validator about intentional joints" is open. | **needs-fixing** (known false positive; e1ed7e3 already excludes touching pairs from the Fix feed) |
| Service may die on full logout | systemd user service; it survives logout only while lingering is enabled. State is volatile — check `loginctl show-user cody -p Linger --value` (`yes` on 2026-07-07). | **volatile** (printforge-run-and-operate owns the current state) |

None of these are latent enough to change without a reason — but a new maintainer
must know they exist before assuming the system is concurrency-safe or authenticated.
It is neither, by design.

---

## When NOT to use this skill

- **Actually changing behaviour / shipping a change** → this skill explains *why*;
  the gates, deploy protocol and non-negotiables live in **printforge-change-control**.
- **OpenSCAD/CSG semantics, the customizer-comment recipes, text/emboss algebra** →
  **printforge-openscad-reference**.
- **trimesh / 3MF / STEP / slicing internals, split_parts, cascadio units** →
  **printforge-mesh-geometry-reference**.
- **What each env var/flag/constant does and how to add one** →
  **printforge-config-and-flags**.
- **Recreating the host/docker/organic environment** → **printforge-build-and-env**.
- **systemd ops, deploy commands, the live API surface** → **printforge-run-and-operate**.
- **A specific bug's symptom → root cause** → **printforge-debugging-playbook** or
  **printforge-failure-archaeology**.
- **Evidence standards, golden inventory, how to add a check** →
  **printforge-validation-and-qa**.
- **The organic quality campaign / research frontier** →
  **printforge-organic-quality-campaign**, **printforge-research-frontier**.

Use *this* skill only for the "how is it wired, why that way, what must stay true,
where is it thin" questions.

---

## Provenance and maintenance

Everything here was verified against the repo on 2026-07-06. Line numbers drift;
re-verify the load-bearing anchors with:

```bash
cd /home/cody/projects/printforge
grep -n 'PARAM_RE = re.compile' app.py           # the LLM↔UI contract (§2)
grep -n 'def call_codex_edit' app.py             # edit-in-place invariant (I2)
grep -n 'def vision_qa\|def floating_starts' app.py parts.py  # QA gate (§2, I12)
grep -n 'def lock_violations' app.py             # lock invariant (I4)
grep -nE 're\.fullmatch\(r' app.py               # id-validation sites (I6)
grep -n 'LAST_BACKEND' app.py                    # global single-user state (§4)
grep -n '_organic_lock\|def _free_gpu' app.py    # GPU arbitration (§2, I10)
grep -n '"profile": dict(prof)' app.py           # profile snapshot (I11)
grep -niA10 'Later' README.md                    # which weak points are acknowledged
rg -n 'parse_model_contract|REQUIRED_CHECKS|unshare-all|model_envelope' evolution_lab/cadquery.py
rg -n 'build_examples_v2|family_split|verified_join|artifact_checksum' evolution_lab tests/test_dataset_v2.py
rg -n 'BambuStudioCLIAdapter|slice_metrics_incomplete|bambu_slicer_runtime_ready' evolution_lab tests/test_slicer_phase3.py
```

When any of these move or change shape, update the matching section here. If
`PARAM_RE`, the edit-in-place path, the QA base-diff, or the id-validation set
changes, treat it as a contract change: it belongs in change-control and this file
must be updated in the same breath.
