---
name: printforge-config-and-flags
description: >-
  The complete configuration catalog for PrintForge — every environment
  variable, config file, and hardcoded "acts-like-config" constant, with
  defaults, where each is read (file:line), where each is set, and the
  production value. Load this when you are about to change or add an env var
  (LLM_BACKEND, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY, QA_CHECK, QA_ROUNDS,
  BAMBUDDY_URL, BAMBUDDY_API_KEY, OPENSCAD_ARGS, ORGANIC_LIBS,
  THINGIVERSE_TOKEN); add a new flag/option/knob; touch printer profiles or
  presets (profiles.json, presets.txt, DEFAULT_PROFILES, resolve_profile); tune
  a timeout or size cap (OPENSCAD_TIMEOUT, codex 420/900s, organic 720s, 100MB
  caps); or answer "what does X env var do", "what's the default for X", "where
  is X read", "how do I add a config value", "which printer profile is default".
  Also the source of truth for the /config, /profiles, /presets endpoints.
---

# PrintForge configuration and flags

This is the exhaustive catalog of everything that configures PrintForge:
environment variables, config files, and the hardcoded constants that behave
like config (timeouts, size caps, truncation limits). If a value tunes
behavior, it is documented here with its exact read site and its production
value.

**All facts verified against the repo on 2026-07-06.** Line numbers drift when
`app.py` changes — re-run the grep commands in *Provenance and maintenance* at
the bottom before trusting any `app.py:NNN` citation.

**Before you change behavior based on anything here, route through
`printforge-change-control`.** This skill tells you *what* the knobs are and
*where* they live; the change-control skill tells you *whether and how* you are
allowed to turn them on a live service with a real second user.

Jargon defined once:
- **codex backend** — PrintForge shells out to the `codex exec` CLI (OpenAI
  gpt-5.5) to write OpenSCAD. This is the production generation path.
- **LiteLLM / qwen fallback** — a local OpenAI-compatible server at
  `127.0.0.1:4000` running qwen; the rate-limit lifeboat when codex is down.
- **profile** — a printer's hard constraints (bed size, nozzle, clearances)
  injected into the LLM prompt.
- **preset** — the user's own measured values ("my slip fit = 0.2"), free text.

---

## 1. Environment variables

Every variable is read in `app.py` via `os.environ.get(...)` at module import
(lines 24-34) except `THINGIVERSE_TOKEN` (read per-request) and `ORGANIC_LIBS`
(read per organic generation). "Read at" is the exact `app.py` line.

| Var | Default (in code) | Read at | Set by (production) | Production value | Load-bearing? |
|-----|-------------------|---------|---------------------|------------------|---------------|
| `LLM_BACKEND` | `"http"` | `app.py:24` | **run.sh** (`${LLM_BACKEND:-codex}`) | `codex` | **PRODUCTION-CRITICAL.** `codex` = gpt-5.5 path; anything else = HTTP/OpenAI-compatible call to `LLM_BASE_URL`. Image input hard-requires `codex` (app.py:159-164). Docker leaves it unset → `http`. |
| `LLM_BASE_URL` | `"http://127.0.0.1:4000/v1"` | `app.py:25` | compose (`${LLM_BASE_URL:-...}`); unset on host | `http://127.0.0.1:4000/v1` (LiteLLM) | Load-bearing **only when backend=http** (fallback path). Ignored when backend=codex. |
| `LLM_MODEL` | `"claude-brain-coder"` | `app.py:26` | compose (`${LLM_MODEL:-...}`); unset on host | `claude-brain-coder` (qwen via LiteLLM) | Load-bearing only for the http/fallback path. Also shown in `LAST_BACKEND` string on local fallback. |
| `LLM_API_KEY` | `"dummy"` | `app.py:27` | compose (`${LLM_API_KEY:-dummy}`) | `dummy` | LiteLLM ignores it; `dummy` is fine. Only matters if you point `LLM_BASE_URL` at a keyed OpenAI-compatible endpoint. |
| `QA_CHECK` | `"1"` → `True` | `app.py:28` | unset (uses default) | on | Experimental-ish toggle. `=="1"` enables the vision QA self-check loop (codex backend only). Set to `0` to disable QA entirely (faster, no auto-fix). |
| `QA_ROUNDS` | `"2"` → `int 2` | `app.py:29` | unset (uses default) | 2 | **Tuning knob.** Max look-fix-rerender iterations. Higher = more thorough, much slower (creative asks already take 5-10 min at 2). Raising past ~2 hits diminishing returns / LLM whack-a-mole — fix the `.scad` by hand instead. |
| `BAMBUDDY_URL` | `"http://192.168.1.50:8000"` | `app.py:30` | unset (default is the prod VM) | `http://192.168.1.50:8000` (VM 104) | Optional integration. Points at the Bambuddy print-archive VM. |
| `BAMBUDDY_API_KEY` | `""` | `app.py:31` | **.env** (sourced+exported by run.sh) | *(secret — in .env)* | Optional integration, but **the only var in `.env` today**. Empty ⇒ `/config` reports `bambuddy:false` and archive is disabled (`bool(BAMBUDDY_API_KEY)`, app.py:1314). **Never inline this — .env only.** |
| `OPENSCAD_ARGS` | `"--enable=textmetrics --enable=manifold"` (`.split()`) | `app.py:34` | host: default (nix openscad-unstable). **compose: `""`** | `--enable=textmetrics --enable=manifold` | **PRODUCTION-CRITICAL on host.** Passed to every openscad invocation. Docker blanks it because Debian's openscad 2021.01 has no such flags and turns `textmetrics()` into silent garbage geometry. Do not blank it on the nix host. |
| `ORGANIC_LIBS` | `"/run/opengl-driver/lib"` | `app.py:1293` | **run.sh** (`/run/opengl-driver/lib:<libglvnd>/lib`) | `/run/opengl-driver/lib:<nix libglvnd>/lib` | Load-bearing **for organic mode only**. Becomes `LD_LIBRARY_PATH` for the `organic/generate.py` subprocess so CUDA + libGL resolve inside the venv. Wrong value ⇒ organic generation fails to import torch/cv2. |
| `THINGIVERSE_TOKEN` | `""` | `app.py:725` | unset (feature unused without it) | *(unset)* | Optional integration. Empty ⇒ Thingiverse import raises "needs a token". Printables works keyless; MakerWorld is hard-blocked regardless. |
| `PATH` | inherited | run.sh exports | run.sh: `~/.local/npm/bin:~/.local/bin:$PATH` | — | **Not app config but load-bearing.** systemd does not inherit the login-shell PATH; run.sh prepends `~/.local/npm/bin` so the `codex` binary is found. Removing this line = codex backend silently unavailable. |

Notes:
- The host (systemd/run.sh) path relies on **code defaults** for most vars —
  run.sh only sets `LLM_BACKEND`, `ORGANIC_LIBS`, `PATH`, and exports
  `BAMBUDDY_API_KEY` from `.env`. Everything else is the `os.environ.get`
  default. There is no `.env` line for `LLM_*`, `QA_*`, `BAMBUDDY_URL`,
  `OPENSCAD_ARGS`, or `THINGIVERSE_TOKEN` today.
- The **Docker path** (`compose.yaml`) is qwen-only by design: it sets
  `OPENSCAD_ARGS=""`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, and leaves
  `LLM_BACKEND` unset (⇒ `http`). It has no codex (the CLI + auth live on the
  host).

Training Lab also reads `PRINT_FORGE_CADQUERY_ENABLED` in
`evolution_lab/config.py`. It defaults to false and only records a dormant
`cadquery-v1` request in Training Lab bootstrap capabilities. Bootstrap reports
contract support separately from `runtime_ready=false`; the flag does not
install CadQuery/Bubblewrap, provide a dedicated worker, or replace the active
OpenSCAD adapters.

`PRINT_FORGE_BAMBU_SLICER_ENABLED` is a second off-by-default Training Lab
request flag. It advertises that slicing was requested, but bootstrap keeps
`slicer=false` and `bambu_slicer_runtime_ready=false` until a pinned Bambu
Studio binary, Bubblewrap, complete immutable machine/process/filament profiles,
and matching real smoke evidence are all available. The flag never installs or
executes Bambu Studio by itself.

---

## 2. Hardcoded constants that act like config

These are not env-configurable; changing them means editing `app.py` and
restarting. Each has a real behavioral consequence.

| Constant | Value | At | What it governs / change implication |
|----------|-------|----|--------------------------------------|
| `OPENSCAD_TIMEOUT` | `60` s | `app.py:32` | Per-openscad-call wall clock (render_stl app.py:187, render_png app.py:312). Complex models can be slow; a too-low value silently returns `None` (no STL / no PNG), not an error. Raise if legit large models time out. |
| codex exec timeout | `420` s | `app.py:113` | `call_codex` (read-only generation). Agent Bash caps at 10 min; a full generation can exceed it — the app itself allows 7 min per codex call. |
| codex edit timeout | `900` s | `app.py:141` | `call_codex_edit` (workspace-write, in-place edits for refines/QA). Edits of long files are slower; 15 min ceiling. |
| organic timeout | `720` s | `app.py:1299` | `organic/generate.py` subprocess. ~90s typical inference; 12 min ceiling covers cold model load + GPU unload wait. |
| STL size cap | `100_000_000` (100 MB) | `app.py:192` | Rendered STL rejected above this (render_stl). Guards against runaway geometry. |
| Upload size cap | `100_000_000` (100 MB) | `app.py:599` | Uploaded mesh rejected above this → HTTP 413 "mesh too large (100MB cap)". |
| Default render size | `"800,600"` | `app.py:304` | `render_png` default imgsize. |
| Oblique/cluster QA render size | `"1000,750"` | `app.py:387,421,425` | Higher-res oblique close-ups for the vision QA reviewer (low relief needs oblique + detail). |
| Thumbnail size | `"400,300"` | `app.py:539` | Library `thumb.png`. |
| potrace pre-resize | `"1000x1000>"` | `app.py:581` | imagemagick downscale before tracing a logo→SVG (`_trace_to_svg`). |
| organic `target_mm` clamp | `10.0`–`250.0` | `app.py:1298` | User-requested organic output size is clamped to `max(10.0, min(250.0, req.target_mm))`. Default `target_mm=80` (app.py:1263). |
| `intent[-12:]` | last 12 | `app.py:534` | Only the 12 most-recent accepted-decision intent entries persist in library meta (the "never revert these" list). |
| `rules[:20]` | first 20 | `app.py:1409` | Per-model user rules capped at 20 (`PUT /models/{id}/rules`). |
| `part_state[:30]` | first 30 | `app.py:1423` | Per-model part lock/state entries capped at 30 (`PUT /models/{id}/parts`). |
| params-changed cap | `[:12]` | `app.py:946-948` | `/models/{id}/diff` reports at most 12 changed/added/removed params each. |
| model id shape | `[0-9a-f]{12}` | `app.py:1318` | 12-hex library id validation. |

These caps are guardrails, not tuning targets. Raise a timeout when a legit job
exceeds it; do not lower the size caps below real upload needs. Any change here
is a code change → change-control + restart + verify.

---

## 3. Configuration files

### `.env` — secrets, gitignored
- Sourced and exported by run.sh: `[ -f .env ] && . ./.env && export
  BAMBUDDY_API_KEY`.
- **Only variable present today: `BAMBUDDY_API_KEY`** (verify names with
  `grep -oE '^[A-Z_]+' .env` — never print values).
- This is the accepted secret pattern. A secret pasted inline into code/chat got
  classifier-blocked once; keep secrets in `.env`, out of git, out of the repo.
- If you add a secret, add a matching `export VAR` in run.sh (only
  `BAMBUDDY_API_KEY` is auto-exported today; `. ./.env` sets the shell var but
  child processes need it exported).

### `presets.txt` — user's measured defaults, user-owned
- Path: repo root (`PRESETS_FILE`, app.py:285). **User data — do not edit by
  hand.**
- Read/written via `GET /presets` and `PUT /presets` (app.py:951-963).
- **4000-char cap** on write: `req.text[:4000]` (app.py:962).
- If the file is missing, `DEFAULT_PRESETS` (a commented example block,
  app.py:286-290) is served instead.
- Injected into prompts by `presets_block()` (app.py:293-300) as "USER
  DEFAULTS — use an entry ONLY when the request involves that object".

### `profiles.json` — custom printer profiles, user-owned
- Path: repo root (`PROFILES_FILE`, app.py:235). **User data — do not edit by
  hand.**
- Written via `PUT /profiles/custom` (app.py:868-881): only keys already present
  in the default profile shape are accepted (`if k in base`), name capped at 50
  chars, stored as `{name: profile}` JSON.
- Merged **over** `DEFAULT_PROFILES` by `all_profiles()` (app.py:239-246): custom
  entries with the same name override defaults; a corrupt file is silently
  ignored (`except: pass`).

### `DEFAULT_PROFILES` — the built-in printer table (in code)
Built by the `_profile()` helper (app.py:219-225), which fixes many fields to
constants. Full shape of every profile:

| field | value / source |
|-------|----------------|
| `nozzle` | `0.4` (fixed) |
| `layer` | `0.2` (fixed) |
| `min_wall` | `2.0` (fixed) |
| `fit_clearance` | `0.2` (fixed) |
| `snap_clearance` | `0.15` (fixed) |
| `loose_clearance` | `0.4` (fixed) |
| `min_detail_depth` | `0.8` (fixed) |
| `name`, `printer`, `bed_mm`, `material`, `density`, `multicolor`, `max_overhang_deg`, `supports` | per-profile (below) |

The five defaults (`app.py:228-234`):

| name | printer | bed_mm | material | density | AMS | max_overhang_deg |
|------|---------|--------|----------|---------|-----|------------------|
| `Bambu A1 - 0.4mm PLA` | Bambu A1 | 256×256×256 | PLA | 1.24 | yes | 50 |
| `Bambu A1 - 0.4mm PETG` | Bambu A1 | 256×256×256 | PETG | 1.27 | yes | 45 |
| `Bambu P1S - 0.4mm PLA` | Bambu P1S | 256×256×256 | PLA | 1.24 | yes | 50 |
| `Bambu P1S - 0.4mm PETG` | Bambu P1S | 256×256×256 | PETG | 1.27 | yes | 45 |
| `Generic FDM - 220x220x250 PLA` | Generic FDM | 220×220×250 | PLA | 1.24 | no | 50 |

`supports` default text (all): `"prefer support-free geometry; tree supports
acceptable"`. `max_overhang_deg` defaults to 50 unless the profile passes 45
(the two PETG variants).

### `DEFAULT_PROFILE` constant
`"Generic FDM - 220x220x250 PLA"` (app.py:236). This is the fallback profile
when none is selected/named, and the base copied for a new custom profile.

### Profile resolution precedence (`resolve_profile`, app.py:254-266)
For each generation, the active profile is chosen as:

1. **Prompt-named printer override** (highest). If the prompt text mentions a
   non-generic printer name (e.g. "print on my P1S") that differs from the
   selected profile's printer, that printer wins — preferring the variant whose
   **material matches** the currently active profile. Returns an `override`
   reason string surfaced to the user.
2. **Client-selected profile.** `get_profile(name)` for the `profile` field the
   client sent.
3. **Default.** If the selected name is missing/blank, `DEFAULT_PROFILE`, else
   the first available profile (`get_profile`, app.py:249-251).

The chosen profile is rendered into the prompt by `profile_block()`
(app.py:269-282) as "hard print constraints for this job, overriding any
conflicting user defaults".

---

## 4. "Add a new config axis" checklist

Follow this exactly; skipping a step is how flags rot. **Route the change
itself through `printforge-change-control`.**

1. **Read it with a default.** Add near app.py:24-34:
   `NEW_FLAG = os.environ.get("NEW_FLAG", "<safe-default>")`. Cast/compare
   explicitly (`== "1"` for bools, `int(...)` for numbers) — see QA_CHECK /
   QA_ROUNDS for the pattern. The default must be the safe production behavior
   so an unset var never breaks the live service.
2. **Document it in the README run section** (README.md is the doc of record —
   `printforge-change-control` owns that rule). Table row: name, default, what
   it does.
3. **Set it in run.sh only if production needs a non-default** value. Match the
   existing style (`export NEW_FLAG="${NEW_FLAG:-...}"`). If it is a **secret**,
   put it in `.env` and add `export NEW_FLAG` after the `. ./.env` line — never
   inline a secret in run.sh, compose, or code.
4. **Add it to `compose.yaml` `environment:`** if it is Docker-relevant.
   Remember the Docker path is qwen-only with `OPENSCAD_ARGS=""` — don't hand
   Docker a codex-only flag.
5. **Restart and verify.** `systemctl --user restart printforge`, then confirm:
   - `GET http://localhost:8093/config` (feature flags), and/or
   - `journalctl --user -u printforge` for startup, and/or
   - a `GET` that exercises the new behavior.
6. **`/config` visibility is optional.** `GET /config` returns **only**
   `{"bambuddy": bool(BAMBUDDY_API_KEY), "organic": _organic_ready()}`
   (app.py:1312-1314) — two feature flags, nothing else. If your new axis is a
   user-visible feature toggle, adding a field here is nice but not required;
   most knobs are not surfaced.

Never add a config value for something that never varies (YAGNI). If the value
has exactly one real setting, hardcode it as a named constant like
`OPENSCAD_TIMEOUT` and skip the env var.

---

## When NOT to use this skill

- **Actually changing/deploying anything** → the gate is
  `printforge-change-control` (deploy protocol, non-negotiables). This skill is
  the *catalog*; that one is the *permission and process*.
- **Recreating the host/docker/organic environment from scratch** (nix, uv,
  torch wheels, `organic/setup.sh`) → `printforge-build-and-env`.
- **Running the service, systemd ops, artifact/API map** →
  `printforge-run-and-operate`.
- **Why `OPENSCAD_ARGS` / textmetrics matters, or any past config incident's
  story** → `printforge-failure-archaeology`.
- **OpenSCAD customizer flags / CSG semantics** →
  `printforge-openscad-reference`. **trimesh/3MF/STEP knobs** →
  `printforge-mesh-geometry-reference`.
- **A symptom to diagnose** ("codex not found", "organic fails") →
  `printforge-debugging-playbook`.

This skill answers "what is the default / where is it read / how do I add one".
It does not diagnose, deploy, or explain domain semantics.

---

## Provenance and maintenance

Verified 2026-07-06 against `/home/cody/projects/printforge` by reading
`app.py`, `run.sh`, `compose.yaml`, `Dockerfile`, and
`~/.config/systemd/user/printforge.service` (host-specific path; the unit runs
`run.sh` with `Restart=on-failure`, `WantedBy=default.target`; linger state is
volatile — check `loginctl show-user cody -p Linger --value`, owned by
printforge-run-and-operate).

**Flags rot — re-verify before trusting line numbers.** Run these; if output
differs from the tables above, update this file:

```sh
cd /home/cody/projects/printforge
# every env var + its read site and default:
grep -n 'os.environ' app.py organic/generate.py
# hardcoded constants that act like config:
grep -nE 'OPENSCAD_TIMEOUT|timeout=[0-9]|100_000_000|target_mm|intent\[|rules\[|part_state\[|imgsize|DEFAULT_PROFILE|4000' app.py
# what run.sh / compose actually set:
grep -nE 'export|LLM_|OPENSCAD_ARGS|ORGANIC_LIBS' run.sh
grep -nE 'OPENSCAD_ARGS|LLM_' compose.yaml
# .env variable NAMES only — never print values:
grep -oE '^[A-Z_]+' .env
# live feature flags (GET is safe):
curl -s http://localhost:8093/config
# default printer profiles + resolution logic:
sed -n '219,266p' app.py
```

Volatile facts to re-date: the "only var in .env is BAMBUDDY_API_KEY" claim, the
five DEFAULT_PROFILES rows, and the `/config` response shape.
