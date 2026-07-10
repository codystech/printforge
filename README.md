# PrintForge

Describe a 3D model in plain English (or attach a photo/sketch) → LLM writes
parametric OpenSCAD → vision self-check catches geometry bugs → tweak with
sliders → download STL / split-part 3MF → slice in Bambu Studio, or send it
straight to the Bambuddy archive.

## Current features

**Creating models**
- Plain-English prompt → parametric OpenSCAD via codex/gpt-5.5 (falls back to
  local qwen when rate-limited; the status line names which model answered)
- ✍ Spec preview: your prompt becomes an editable design spec with PRINTER,
  ATTACHED (per-model context), ASSUMPTIONS and MISSING sections — correct it
  before any generation time is spent
- 🖨 Printer profiles: neutral machine/material profiles (Bambu A1/P1S ×
  PLA/PETG, Generic FDM) selected per browser; bed volume, nozzle, layer,
  material, clearances, detail limits and AMS support become hard constraints
  for every job. Prompts naming another printer override visibly per job;
  refining under a different profile warns first. Custom profiles via
  `PUT /profiles/custom`.
- My presets: your measured objects (phone, desk edge, calibrated fits) fed
  into every relevant spec/generation; 🧪 builds a tolerance calibration
  coupon so those numbers come from your actual printer
- Archetype + hardware guidance injected by keyword: gridfinity, hinges,
  clips, signs, enclosures, Raspberry Pi mounting grid, 40mm fans, heat-set
  inserts, standoffs, cable glands, countersinks, magnet pockets, zip-tie
  channels, keyhole hangers, pegboard hooks, dovetails, snap tabs
- 🧬 Organic mode: Hunyuan3D-2 on the local GPU sculpts a real mesh from a
  photo (setup once via `organic/setup.sh`; auto-unloads the ollama brain)
- 📦 Import: drag-and-drop STL / 3MF / OBJ / GLB / **STEP** (converted through
  OpenCascade, original preserved) / bitmap logos (auto-traced to SVG curves),
  or 🔗 import from Printables/Thingiverse URLs. Every upload is inspected:
  format, dimensions, triangles, bodies, watertightness, unit warnings.
- Mesh roles per upload: 🖨 printable / 👁 reference / 📐 fit-cutout /
  🧩 assembly / ⛔ negative-space. Reference boards never enter printable
  output, and a guard warns if you ask for a "case" around a printable mesh.
- Multi-mesh integration: up to 3 base meshes ("mount this Pi model inside
  this case on standoffs") — each sent with measured dimensions and its role

**Quality machinery**
- Vision QA loop: renders (incl. close-ups of changed regions diffed against
  the previous state) reviewed by gpt-5.5, up to 2 auto-fix rounds
- Printability detector: slices bottom-up and flags features that start in
  mid-air (Bambu's "floating regions") with coordinates; 🩹 one click sends
  detected issues back as a minimal repair refine
- Print report on every generation: assembled dimensions, part count,
  material-correct weight estimate, watertightness, bed-fit check against the
  active printer profile
- Δ version compare: refines show QA transition, weight delta, part-count and
  parameter changes vs the parent version
- Refines run through codex's file-editing tools (no full-file rewrites) and
  inherit design intent — every accepted change is preserved as law
- 👍/👎 taste training: for a similar future prompt, the best-matching liked
  model is retrieved as a few-shot example to emulate and the best-matching
  disliked model as a counter-example to avoid

**Assembly (v1 + Parts Panel v2)**
- Every part gets an `_enabled` toggle + `assembled_preview` mode
- Parts panel: hide, 🔒 lock (hard constraint, verified by diffing the locked
  module after each refine), 🚫 suppress (AI may not re-add), ✎ rename,
  ♻ regenerate-one-part
- Project rules: user-authored constraints stored per model, inherited by
  refines, enforced in generation and QA
- 🔧 Validate assembly: renders each part in assembled position, checks
  pairwise collisions (manifold booleans) and clearances (<0.4mm warned)

**Output & library**
- Export: STL, multi-part 3MF with per-part AMS color palette, OBJ and GLB
  (`/export/{id}?fmt=`); STEP export is honestly refused (mesh-only
  pipeline); direct upload to a Bambuddy archive
- Library: auto-saved, auto-named (local LLM), thumbnails, ratings, search
  (incl. printer profile), rename, duplicate, delete, download-as-ZIP,
  copy-prompt, lineage (parent links), per-model rules, part states, QA
  outcome, backend and printer-profile snapshot in metadata
- Every Library card and loaded-model panel shows the safe 12-character public
  model ID with a Copy ID action; `/?model=<id>` opens that model directly

**Evolution Training Lab (experimental, disabled by default)**
- `/training-lab/` is an isolated runtime-evolution workspace; it never replaces
  the normal Generate workflow and never writes candidates into `library/`
- Two controlled candidates share one baseline, specification, profile, locks,
  reference roles and export exclusions; evidence scoring selects a winner only
  when it beats the immutable current best
- New runs can either select an existing Library model through a searchable
  name/thumbnail/ID/version/status picker or create generation zero directly
  from a design specification; legacy source-model requests remain compatible
- Iteration count, runtime, repeated failures, no-improvement streak and an
  optional quality target are explicit bounded stop controls (defaults: 5
  iterations, 20 minutes, 3 failures and 2 no-improvement iterations)
- Every generation-zero attempt and A/B candidate is independently persisted
  with prompt, score, validation evidence, failure reason, timestamps and
  lineage. Candidates can be inspected, compared, restored, branched or
  explicitly deleted; baselines and the current best are protected
- Live run state reports iteration, stage, best score, latest failure and
  elapsed time. Immediate cancellation terminates the isolated lab Codex
  process group and keeps the interrupted candidate for diagnosis
- Atomic filesystem state under gitignored `training_lab_data/` preserves runs,
  candidates, rejected variants, evidence, events, checkpoints, scoped memory,
  calibration/physical feedback, benchmark results and promotion proposals
- The page visualizes persisted pipeline state, synchronized A/B previews,
  rewards, issues, mutations, lineage, memory, score progression and event logs;
  its seeded SIX SEVEN example is permanently labeled demo data
- Dataset exports produce redacted JSON, JSONL, CSV or ZIP preference, repair,
  calibration, supervised and failure examples. This prepares data; it does not
  change neural-network weights.
- Actual fine-tuning is not implemented for the current backends. The training
  endpoint reports unsupported and cannot deploy or merge anything.

## Run (primary — host, codex backend)

```sh
./run.sh   # open http://localhost:8093
```

Generation goes through `codex exec` (gpt-5.5 on the OpenAI pool — needs the
codex CLI + auth on the host), with automatic fallback to local qwen via
LiteLLM if codex errors. OpenSCAD comes from `nixpkgs#openscad-unstable`
(2024+ — needed for `textmetrics()`), python deps via uv.

## Run (docker — qwen only)

```sh
docker compose up --build
```

No codex inside the container: generation uses the local LiteLLM/qwen brain
(`claude-brain-coder` at `:4000`). Debian's OpenSCAD is 2021.01, so
`OPENSCAD_ARGS` is blanked — no textmetrics, cruder text sizing. Env vars:
`LLM_BACKEND` (`codex`/`http`), `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`,
`OPENSCAD_ARGS`.

Experimental flags all default to `false` and are read at startup:

| Variable | Purpose |
|---|---|
| `PRINT_FORGE_EVOLUTION_ENABLED` | permits explicit A/B evolution jobs |
| `PRINT_FORGE_TRAINING_LAB_ENABLED` | enables the isolated API/page and demo history |
| `PRINT_FORGE_MEMORY_LEARNING_ENABLED` | permits scoped memory updates/application |
| `PRINT_FORGE_PHYSICAL_FEEDBACK_ENABLED` | permits physical and calibration records |
| `PRINT_FORGE_ACTUAL_TRAINING_ENABLED` | outer gate for a future supported training provider |
| `PRINT_FORGE_TRAINING_ENABLED` | future training-job gate; no provider exists today |
| `PRINT_FORGE_LAB_ONLY` | blocks non-Training-Lab mutations on the isolated test service |

Future training configuration is inert today: `PRINT_FORGE_TRAINING_BACKEND`,
`PRINT_FORGE_TRAINING_DATASET`, `PRINT_FORGE_BASE_MODEL`,
`PRINT_FORGE_TRAINED_MODEL_PATH`, `PRINT_FORGE_TRAINED_MODEL_VERSION`, and
`PRINT_FORGE_TRAINED_MODEL_APPROVED`. Enabling flags does not merge branches,
activate learned production rules, or perform model-weight training.

### Isolated Training Lab test service

`printforge-training-lab.service` runs the current experimental branch at
`http://localhost:8094` (redirecting to `/training-lab/`). It listens on
`0.0.0.0:8094` so a source-limited host firewall can make it available to
trusted LAN clients; on Cody's main PC that URL is
`http://192.168.1.77:8094/training-lab/`. It enables the
runtime-evolution/memory/physical-feedback lab flags, keeps actual model training
disabled, and sets `PRINT_FORGE_LAB_ONLY=true`. In lab-only mode, non-lab writes
such as `/generate`, uploads, ratings, profile edits, and model deletion return
HTTP 403; production on port 8093 is unaffected. The Training Lab has no login,
so network access should remain restricted to trusted source networks.

## How it works

- `POST /generate` — prompt (+ current .scad when refining, + optional image
  data URL) → LLM → parametric OpenSCAD with customizer variables; backend
  validates by rendering, feeds errors back for one retry, then runs the
  vision QA round (codex only, `QA_CHECK=0` to disable) and auto-saves to
  `library/`.
- `POST /render` — .scad + slider overrides → `openscad -D k=v` → binary STL.
- `GET /stl/{id}` — preview (three.js) or download (`?download=1`).
- `GET /export/{id}` — split-part 3MF (trimesh connected components).
- `POST /send/{id}` — upload the 3MF to Bambuddy (`BAMBUDDY_URL` +
  `BAMBUDDY_API_KEY` in `.env`, gitignored).
- `GET/PATCH/DELETE /models…` — the library.

Model quality tracks the LLM: gpt-5.5 via codex handles multi-part designs
(LED signs, enclosures); qwen is fine for simple brackets. "Generate/Refine"
edits the current model; "New model" starts from scratch — don't refine a
phone stand into an LED sign.

## Later (deliberately not built yet)

- Deploy to a lab CT behind NPM/Authelia once it proves useful.
- Sparc3D/TRELLIS.2 evaluation as alternative organic backends.
- Bambu-native paint-color metadata (basematerials support unconfirmed).
- Text-only figurines (local image gen chained into Hunyuan3D).
- In-UI custom printer-profile editor (API-only today).
- Thin-wall analysis (the slicer does it better — intentionally skipped).
