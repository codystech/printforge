---
name: printforge-build-and-env
description: >-
  Recreate every PrintForge environment from scratch (host run.sh stack, Docker
  fallback, and the organic/ CUDA venv) with the traps that bite. Load when
  setting up PrintForge on a new machine or after a wipe; when hitting "command
  not found" (codex/openscad/magick/uv/nix), "No module named ..." import
  errors, torch/CUDA/libGL errors, or a silently-hanging HuggingFace weights
  download; when editing run.sh, Dockerfile, compose.yaml, or organic/setup.sh;
  when the systemd service won't start or dies on logout; or when a dependency
  bump changes behavior on restart. Covers the nix-shell openscad wrap, the
  uv run --with dependency list, ORGANIC_LIBS/LD_LIBRARY_PATH, the pymeshlab sed
  bypass, and the ~/.cache/hy3dgen weights.
---

# PrintForge build & environment

Everything here is verified against the repo as of **2026-07-06**. PrintForge has
**no package manifest and no lockfile** — the runtime environment is assembled
imperatively by `run.sh` (host) or `Dockerfile` (container), and the image→mesh
pipeline by `organic/setup.sh`. There is nothing to `pip install -r`; recreate
the environment by re-running those scripts and understanding what each line is
for. This skill is that understanding.

There are **three separate environments**, do not conflate them:

| Environment | Built by | Python | Purpose |
|---|---|---|---|
| **Host stack** (production) | `run.sh` via systemd | `uv run --with …` ephemeral | the live service on :8093, codex backend |
| **Docker** (fallback only) | `Dockerfile` + `compose.yaml` | `pip` into image | qwen-only, no codex, cruder text |
| **Organic venv** (optional) | `organic/setup.sh` | `organic/.venv` py3.11 | image→mesh on the RTX 3090 |

Before you change anything that affects behavior, read
`printforge-change-control` — deploy is edit + `systemctl --user restart
printforge` + verify, and a second real user is on this service over the LAN.

---

## 1. Host prerequisites

The host stack needs these on `PATH` **before** `run.sh` runs. Verified present
on the production box today (paths shown are where they actually live here):

| Tool | Verified path (this box) | Why it's needed | Verify |
|---|---|---|---|
| **nix** (flakes enabled) | `/run/current-system/sw/bin/nix` | wraps openscad + libglvnd; `run.sh` calls `nix shell` / `nix build` | `nix flake --help >/dev/null && echo ok` |
| **uv** | `/etc/profiles/per-user/cody/bin/uv` | resolves the ephemeral python env per start | `which uv` |
| **codex CLI** (auth'd) | `~/.local/npm/bin/codex` | primary LLM backend (`codex exec`, gpt-5.5) | `which codex` |
| **magick** (ImageMagick 7) | `/nix/store/…-imagemagick-7.1.2-*/bin/magick` | image preprocessing; note it is **`magick`**, not `convert` | `which magick` |
| LiteLLM/ollama brain *(optional)* | `http://127.0.0.1:4000` | qwen fallback when codex is rate-limited/unavailable | `curl -s http://127.0.0.1:4000/health/liveliness` → `"I'm alive!"` |
| Bambuddy *(optional)* | `http://192.168.1.50:8000` | archive/print handoff; needs `BAMBUDDY_API_KEY` in `.env` | `curl -s -o /dev/null -w '%{http_code}' http://192.168.1.50:8000` |

**Flakes must be enabled.** `nix shell nixpkgs#…` and `nix build nixpkgs#…`
require `experimental-features = nix-command flakes` in `nix.conf` (or the
matching NixOS `nix.settings`). Without it every `run.sh` start fails at the
`nix build` line and the openscad wrap.

### openscad is deliberately NOT installed host-wide

There is **no `openscad` on the host PATH** and that is on purpose. `run.sh`
wraps the entire server in `nix shell nixpkgs#openscad-unstable`, which pins a
2024+ build. **Why:** OpenSCAD **2021.01** — the default in Debian apt and in
older nixpkgs channels almost everywhere — turns `textmetrics()` calls into
**garbage geometry without erroring**. The LLM prompt contract instructs models
to size text with `textmetrics()`, so a 2021.01 openscad silently produces
broken text on the print (an incident that fooled even the maintainer's own
render-based verification). `app.py` therefore runs openscad with
`OPENSCAD_ARGS="--enable=textmetrics --enable=manifold"` (app.py:34), which only
a recent build honors. Never `apt install openscad` or add it to the host
profile — it will regress this silently.

Ad-hoc openscad (for a one-off render during debugging) must use the same wrap:

```sh
nix shell nixpkgs#openscad-unstable --command openscad --enable=textmetrics --enable=manifold -o /tmp/out.stl model.scad
```

---

## 2. `run.sh`, line by line

```sh
#!/usr/bin/env sh
cd "$(dirname "$0")"                                                    # (a)
export PATH="$HOME/.local/npm/bin:$HOME/.local/bin:$PATH"               # (b)
[ -f .env ] && . ./.env && export BAMBUDDY_API_KEY                      # (c)
export LLM_BACKEND="${LLM_BACKEND:-codex}"                              # (d)
export ORGANIC_LIBS="/run/opengl-driver/lib:$(nix build --print-out-paths --no-link nixpkgs#libglvnd 2>/dev/null)/lib"  # (e)
exec nix shell nixpkgs#openscad-unstable --command \                    # (f)
  uv run --with fastapi --with uvicorn --with httpx --with trimesh --with numpy --with scipy \
         --with python-multipart --with networkx --with lxml \
         --with shapely --with rtree --with manifold3d --with cascadio \
  uvicorn app:app --host 0.0.0.0 --port 8093
```

(The 13-package list must stay identical in run.sh and the Dockerfile —
printforge-change-control owns that rule and ships a `diff` one-liner to check it.)

- **(a)** `cd` to the repo root so `app:app` and `.env` resolve regardless of
  where systemd invokes the script.
- **(b) PATH export — the systemd PATH incident.** systemd **user** services do
  **not** inherit your login-shell PATH. codex lives in `~/.local/npm/bin`; when
  the service migrated from background shell tasks to systemd, codex became "not
  found" and the backend silently fell back to qwen. This line puts codex (and
  `~/.local/bin`) back on PATH. If you ever see the UI reporting the local/qwen
  backend when you expect codex, **check this line first** — it is the usual
  cause under systemd.
- **(c) `.env` sourcing.** `.env` is gitignored and its only variable is
  `BAMBUDDY_API_KEY` (the Bambuddy bearer token). Sourced then `export`ed so the
  Python process inherits it. Secrets go here, **never inline in code** (an
  inline secret got classifier-blocked once; this is the accepted pattern).
- **(d) `LLM_BACKEND` default.** Defaults to `codex`. Note `app.py`'s own
  default is `http` (app.py:24) — i.e. if you launch `app.py` **without**
  `run.sh`, you get the qwen/LiteLLM path, not codex. `run.sh` is what makes
  codex the production backend. `${LLM_BACKEND:-codex}` lets you override from
  the environment for testing.
- **(e) `ORGANIC_LIBS` construction — NixOS-specific.** Builds an
  `LD_LIBRARY_PATH` for the organic venv's CUDA + libGL needs:
  `/run/opengl-driver/lib` (NixOS's GPU driver libs, incl. libcuda) plus the nix
  store path of **libglvnd**'s `/lib` (`libGL`). `nix build --print-out-paths
  --no-link` prints the store path without creating a `result` symlink. On a
  non-NixOS host this line yields a mostly-empty/incorrect path — see §5 for
  what the venv actually needs at runtime. It is exported here but only *used*
  when `app.py` spawns the organic subprocess (app.py:1292-1293).
- **(f) the wrap.** `nix shell nixpkgs#openscad-unstable --command <…>` puts a
  modern `openscad` on PATH for the wrapped command only, then `uv run --with …`
  resolves an **ephemeral** Python environment (no venv on disk) and launches
  uvicorn. `exec` replaces the shell so systemd tracks the real process.

### The `uv run --with` dependency list — why each one is there

`app.py`/`parts.py` import these; a missing one is an `ImportError` at request
time, not at start. Reasons:

| Dep | Why | Symptom if missing |
|---|---|---|
| `fastapi`, `uvicorn` | the web app + ASGI server | won't start |
| `httpx` | async HTTP to LiteLLM, Bambuddy, model-import sites | import error |
| `trimesh` | core mesh I/O and geometry (STL/3MF/OBJ/GLB) | import error |
| `numpy`, `scipy` | geometry math, clustering | import error |
| `python-multipart` | FastAPI **`UploadFile`** form parsing | 500 on file upload |
| `networkx` | trimesh **3MF** graph model | 3MF load fails |
| `lxml` | trimesh **3MF** XML parsing | 3MF load fails |
| `shapely` | 2D ops in the **floating-region detector** (`parts.py`) | detector import error |
| `rtree` | spatial index the **floating detector** relies on | detector import error |
| `manifold3d` | robust CSG **booleans** (union/diff for validation) | boolean ops fail |
| `cascadio` | trimesh's OpenCascade backend for **STEP** import | STEP upload fails |

**Any dependency change must land in BOTH `run.sh` and `Dockerfile`** — they are
two independent dependency lists for the same app. Diverging them is a known
foot-gun.

---

## 3. From-scratch host bring-up checklist

Run on a fresh (NixOS) host. Assumes the repo is cloned to
`~/projects/printforge`. Do **not** run any of this against the live box.

1. **Install/enable prerequisites** (§1): nix with flakes, uv, codex (then
   `codex login` / auth once), ImageMagick (`magick`). Optional: the LiteLLM
   brain on :4000 and Bambuddy reachability.
2. **Secrets:** create `~/projects/printforge/.env` (gitignored) with
   `BAMBUDDY_API_KEY=…` if you want the archive/print path. Omit for a
   generate-only deployment.
3. **Smoke-test the stack without systemd** (optional, one manual start):
   `cd ~/projects/printforge && ./run.sh` — first start pays a one-time nix +
   uv resolve cost (minutes). `Ctrl-C` once it's serving.
4. **Install the user unit.** File lives at
   `~/.config/systemd/user/printforge.service`:

   ```ini
   [Unit]
   Description=PrintForge AI 3D model builder
   After=network.target

   [Service]
   Type=simple
   WorkingDirectory=%h/projects/printforge
   ExecStart=%h/projects/printforge/run.sh
   Restart=on-failure
   RestartSec=5

   [Install]
   WantedBy=default.target
   ```

   (`%h` = the user's home. `Restart=on-failure` gives automatic recovery — the
   reason we migrated off background shell tasks after a port-collision outage.)

5. **Enable + start:**
   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now printforge
   ```
6. **Verify it's actually up:**
   ```sh
   curl -s http://localhost:8093/config          # -> {"bambuddy":…,"organic":…}
   journalctl --user -u printforge -n 40 --no-pager
   ```
   `/config` returning JSON means the app started and resolved its deps.

### Lingering caveat (volatile — check, don't assume)

A systemd *user* service stops on full logout **unless** lingering is enabled
(`loginctl enable-linger cody`). The state has flipped during this project's
life, so never assume it — check:

```sh
loginctl show-user cody -p Linger --value   # `yes` on 2026-07-07 (survives logout)
```

printforge-run-and-operate owns this fact. Decide linger changes deliberately —
on a shared workstation, lingering means the service (and its :8093 bind +
potential GPU use) runs with nobody logged in.

---

## 4. Docker path (fallback only)

`Dockerfile` + `compose.yaml` build a **qwen-only** container. Know what it is
**for** and what it deliberately cannot do:

**For:** a self-contained fallback that runs the app against the local LiteLLM
brain when you don't have (or don't want) codex — e.g. a machine without codex
auth, or a fully-offline generate path. It is **not** the production path and is
not what the live service runs.

Deliberate limitations, by design:

- **No codex.** The container has no codex CLI and no host auth, so
  `LLM_BACKEND` is left at `app.py`'s default (`http`) → the LiteLLM/qwen
  backend. qwen is fine for simple brackets but **inadequate for multi-part
  designs**, and **image input hard-requires codex** (app.py:164) so
  image→OpenSCAD is unavailable in the container.
- **`OPENSCAD_ARGS: ""`** (compose.yaml:9). Debian's `openscad` (installed via
  apt in the Dockerfile) is **2021.01** — it does not understand
  `--enable=textmetrics --enable=manifold` and would error on them. Blanking the
  args means text is sized more crudely (no `textmetrics`) but the app runs. Do
  **not** copy this blank value to the host — the host needs the flags (§1).
- **`network_mode: host`** (compose.yaml:6). LiteLLM binds **127.0.0.1 only**;
  the container must share the host loopback to reach it on `127.0.0.1:4000`.
  The app also listens on host port 8093 directly. This is why there is no
  `ports:` mapping.

Env knobs (`compose.yaml`): `LLM_BASE_URL` (default
`http://127.0.0.1:4000/v1`), `LLM_MODEL` (default `claude-brain-coder`),
`LLM_API_KEY` (default `dummy` — LiteLLM ignores it). `restart: unless-stopped`.

---

## 5. The organic venv gauntlet

`organic/setup.sh` builds `organic/.venv` for **Hunyuan3D-2mini** (an
image→mesh diffusion pipeline) on the RTX 3090. This is the single most
fragile setup on the box; every line exists because something broke without it.
**Run it once, by hand, when the 3090 is available.** Below is `setup.sh` as a
numbered procedure with the reason for each weird step. (Do not run this against
the live box's GPU without coordinating — see §5 GPU sharing.)

1. **`uv venv .venv --python 3.11`** — a real on-disk venv (unlike the host
   stack's ephemeral `uv run`). Python **3.11** specifically; the ML stack's
   wheels target it.
2. **`uv pip install torch torchvision --index-url
   https://download.pytorch.org/whl/cu121`** — the **CUDA 12.1** wheel index.
   Installing plain `torch` from PyPI gets a build that won't match the box's
   CUDA/driver → CUDA init failures at generate time. Pin the cu121 index.
3. **`uv pip install trimesh numpy pillow scipy networkx rtree
   fast-simplification rembg onnxruntime`** — mesh post-processing +
   background removal (`rembg`/`onnxruntime`) + decimation
   (`fast-simplification`).
4. **`uv pip install hy3dgen || uv pip install
   "git+https://github.com/Tencent/Hunyuan3D-2.git"`** — the pipeline package.
   **PyPI first, git fallback:** `hy3dgen` on PyPI is thin/occasionally
   unpublished; the Tencent repo is the source of truth. The `||` means "if PyPI
   doesn't have it, build from git."
5. **opencv swap:** `uv pip uninstall opencv-python … ; uv pip install
   opencv-python-headless`. A transitive dep pulls in GUI `opencv-python`, whose
   `cv2` wants **libxcb** (X11 libs we don't have on a headless NixOS box) →
   import crash. The **-headless** build has the same API without the GUI libs.
6. **The pymeshlab sed bypass** (verbatim from setup.sh):
   ```sh
   SG=.venv/lib/python3.11/site-packages/hy3dgen/shapegen/__init__.py
   grep -q "except ImportError" "$SG" || sed -i 's/^from .postprocessors import .*/try:\n    from .postprocessors import FaceReducer, FloaterRemover, DegenerateFaceRemover, MeshSimplifier\nexcept ImportError:\n    pass/' "$SG"
   ```
   **Why:** `hy3dgen.shapegen` imports `.postprocessors`, which imports
   **pymeshlab**, which needs a pile of system libs we don't have — so the whole
   `shapegen` import (and thus generation) fails at load. We don't use pymeshlab
   post-processing anyway (we post-process with **trimesh** in
   `organic/generate.py`), so the sed wraps that import in
   `try/except ImportError: pass`, making it optional. The `grep -q` guard makes
   the patch **idempotent** — re-running `setup.sh` won't double-patch. If a new
   `hy3dgen` version reformats that import line, the `sed` pattern silently
   won't match and you'll be back to a hard `ImportError` on `shapegen` — re-derive
   the patch against the new source.

### Weights: location, first-run download, and the AdGuard hang

- **Weights live in `~/.cache/hy3dgen`** (verified today:
  `~/.cache/hy3dgen/tencent/Hunyuan3D-2mini`), **NOT** the HF hub cache
  (`~/.cache/huggingface`). *(The comment at the top of `setup.sh` says
  "~/.cache/huggingface" — that comment is stale; trust the verified path.)*
  ~**4–6 GB**, pulled from HuggingFace on the **first `/organic` generation**,
  not during `setup.sh`.
- `generate.py` loads with
  `Hunyuan3DDiTFlowMatchingPipeline.from_pretrained("tencent/Hunyuan3D-2mini",
  subfolder="hunyuan3d-dit-v2-mini")` (generate.py:56, `--subfolder` default).
  The `subfolder` is required — the mini DiT weights are in that subdir of the
  repo.
- **The AdGuard silent-hang trap (OPEN).** The lab's AdGuard **blocks
  `us.aws.cdn.hf.co`** (it resolves to `::`), so the first-run HuggingFace
  download **HANGS SILENTLY** — no error, just a stuck generate. Workarounds:
  (a) whitelist `us.aws.cdn.hf.co` in AdGuard (suggested to the owner; not
  confirmed done), or (b) resolve the CDN host via DoH and fetch with
  `curl --resolve us.aws.cdn.hf.co:443:<ip> …`. **Any new model-weights
  download** (a future Sparc3D / TRELLIS evaluation) will hit this same wall —
  check it first when a download hangs.

### Runtime env and GPU sharing

- **`LD_LIBRARY_PATH` at runtime.** `app.py` spawns
  `organic/.venv/bin/python generate.py` with `LD_LIBRARY_PATH` set from
  **`ORGANIC_LIBS`** (falling back to `/run/opengl-driver/lib` if unset;
  app.py:1292-1293). This is what lets the venv's torch find **libcuda** and
  **libGL** on NixOS. If organic generation dies with a `libGL`/`libcuda`
  "cannot open shared object" error, `ORGANIC_LIBS` is wrong or unset — it comes
  from `run.sh` line (e), so it must be present in the service's environment.
- **GPU sharing — only when the 3090 is in the base NixOS generation.** Before
  generating, `_free_gpu()` (app.py:1266) asks **ollama** (on :11434) to unload
  any loaded model (`keep_alive: 0`) so the 3090 has VRAM for Hunyuan3D, and
  `_organic_lock` serializes generations so two never share the card. Inference
  is ~90s for the full pirate-boat test at 30 steps. Practical rule: only expect
  organic mode to work when the workstation is booted into the base NixOS
  generation that owns the 3090 (not a VM-passthrough/gaming generation). Output
  is Y-up and `generate.py` rotates it to Z-up for printing.
- **Presence checks** (non-destructive): `ls organic/.venv/bin/python` and
  `ls ~/.cache/hy3dgen`. Live liveness: `GET /config` → `"organic": true` iff
  `organic/.venv/bin/python` exists (`_organic_ready`, app.py:1257).

---

## 6. Environment drift traps

- **`uv run --with` is resolved fresh on every start.** There is no lockfile.
  Each `systemctl --user restart printforge` re-resolves those dependencies to
  whatever the current PyPI latest is. An **upstream dep bump can change behavior
  on a plain restart** with zero code change — most likely `trimesh`,
  `manifold3d`, or `shapely`. If a restart changes mesh/boolean/detector
  behavior, suspect a silent upstream bump. **Pin only if it bites**: add
  `--with 'trimesh==X.Y.Z'` in `run.sh` (and the mirror in `Dockerfile`). Don't
  pre-pin everything — the ephemeral, unpinned env is a deliberate
  low-maintenance choice; pinning is the exception, not the default.
- **nixpkgs channel motion moves `openscad-unstable`.** `nix shell
  nixpkgs#openscad-unstable` tracks whatever your nix registry's `nixpkgs`
  points at. A channel/registry update can change the openscad version under
  you. Upside is you stay off 2021.01; downside is a future openscad could
  change a flag or rendering behavior on restart. If openscad behavior shifts
  with no `.scad` change, check `nix registry list` / your flake's nixpkgs pin.
- Any change here that alters runtime behavior (adding a dep, changing a flag,
  pinning a version) goes through **`printforge-change-control`** and must be
  mirrored across `run.sh` and `Dockerfile`.

---

## When NOT to use this skill

Use a sibling instead when:

- You need to **operate** the running service (deploy, restart, read logs, hit
  the API, find artifacts) rather than build the environment → **printforge-run-and-operate**.
- Something is **broken at runtime** and you're triaging a symptom → **printforge-debugging-playbook**
  (symptom→triage), or **printforge-failure-archaeology** for the settled-incident dossier.
- You're changing an **env var, flag, or config value** and want the full
  registry + add-a-flag checklist → **printforge-config-and-flags**.
- You're about to **change behavior** and need the gates and deploy protocol →
  **printforge-change-control**.
- The task is **OpenSCAD/CSG** authoring or **mesh/3MF/STEP** semantics, not
  environment plumbing → **printforge-openscad-reference** / **printforge-mesh-geometry-reference**.
- You're working the **organic mesh-quality frontier** (model selection, quality
  gates) rather than *installing* the venv → **printforge-organic-quality-campaign**.

---

## Provenance and maintenance

Everything above was read from the repo and verified non-destructively on
**2026-07-06**. Re-verify drift-prone facts with:

```sh
# the two dependency lists that MUST stay in sync
cat run.sh
grep -n 'pip install' Dockerfile
# env vars the app reads (defaults live here)
grep -n 'os.environ' app.py
# organic setup, verbatim (incl. the sed patch and index URLs)
cat organic/setup.sh
grep -n 'subfolder\|from_pretrained' organic/generate.py
# how the organic subprocess gets its libs
grep -n 'ORGANIC_LIBS\|LD_LIBRARY_PATH\|_free_gpu\|_organic_ready' app.py
# the systemd unit + linger state
cat ~/.config/systemd/user/printforge.service
loginctl show-user "$USER" -p Linger
# host tools actually present
which uv nix codex magick; ls organic/.venv/bin/python; ls ~/.cache/hy3dgen
```

Facts most likely to go stale: the ephemeral `uv run` dependency set (unpinned,
resolves latest each start), the `openscad-unstable` version (nixpkgs channel
motion), the weights path/size (`~/.cache/hy3dgen`, ~4–6 GB), the **linger
state** (volatile — see printforge-run-and-operate), and the OPEN **AdGuard
`us.aws.cdn.hf.co` block**.
