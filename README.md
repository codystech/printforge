# PrintForge

Describe a 3D model in plain English (or attach a photo/sketch) → LLM writes
parametric OpenSCAD → vision self-check catches geometry bugs → tweak with
sliders → download STL / split-part 3MF → slice in Bambu Studio, or send it
straight to the Bambuddy archive.

## Current features

**Creating models**
- Plain-English prompt → parametric OpenSCAD via codex/gpt-5.5 (falls back to
  local qwen when rate-limited; the status line names which model answered)
- ✍ Spec preview: your prompt becomes an editable design spec (dimensions,
  features, print orientation, ASSUMPTIONS to correct) before generating
- Archetype guidance auto-injected by keyword: gridfinity, print-in-place
  hinges, clips, signs, enclosures + hardware specs (Raspberry Pi mounting
  grid, 40mm fans, heat-set inserts, standoffs, cable glands)
- 🧬 Organic mode: Hunyuan3D-2 on the local GPU sculpts a real mesh from a
  photo (setup once via `organic/setup.sh`; auto-unloads the ollama brain)
- 📦 Remix: attach STL/3MF/OBJ files, or bitmap logos (auto-traced to SVG
  curves via potrace), or 🔗 import directly from Printables/Thingiverse URLs
- Multi-mesh integration: attach up to 3 base meshes ("mount this Pi model
  inside this case on standoffs") — each sent with measured dimensions

**Quality machinery**
- Vision QA loop: renders (incl. close-ups of changed regions diffed against
  the previous state) reviewed by gpt-5.5, up to 2 auto-fix rounds
- Printability detector: slices bottom-up and flags features that start in
  mid-air (Bambu's "floating regions") with coordinates; fed back for fixes
- Refines run through codex's file-editing tools (no full-file rewrites) and
  inherit design intent — every accepted change is preserved as law
- 👍/👎 taste training: liked models are retrieved as few-shot examples for
  similar future prompts

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
- STL download; multi-part 3MF with per-part AMS color palette; direct upload
  to a Bambuddy archive
- Library: auto-saved, auto-named (local LLM), thumbnails, ratings, rename,
  delete, lineage (parent links), per-model rules and part states

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
- Phase 2: organic/figurine generation (Hunyuan3D/TripoSR on the 3090).
- SVG import for real logo silhouettes (code-CAD polygon art is angular).
- STEP export, filament color metadata inside the 3MF.
