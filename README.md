# PrintForge

Describe a 3D model in plain English (or attach a photo/sketch) → LLM writes
parametric OpenSCAD → vision self-check catches geometry bugs → tweak with
sliders → download STL / split-part 3MF → slice in Bambu Studio, or send it
straight to the Bambuddy archive.

v2 features: vision QA (renders reviewed by gpt-5.5 before you see them),
multi-part/multi-color 3MF export (one object per connected part, AMS-ready),
model library with thumbnails (auto-saved, reload/rename/delete), photo/sketch
input, preset chips, 256mm P1S build plate in the viewer.

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
