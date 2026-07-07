---
name: printforge-debugging-playbook
description: >
  Symptom-to-triage playbook for PrintForge's real failure modes. Load this when
  something is BROKEN and you need to find the cause fast. Triggers on these
  observed symptoms: "text renders as garbage" / "textmetrics broken" / raised
  text absent on the print but fine in renders; "refine dropped a module" /
  "wheel/robot/chest vanished" / full-file-rewrite regressions; "QA restored
  something I deleted" / "QA reverted my change" / cabin came back; "refine didn't
  change anything" / "editing blind"; "parts collapsed into flat slivers" /
  z-scale footprint; Bambu Studio / slicer "rejects the 3MF" / "floating regions";
  upload "contains no 3D geometry" / "STEP import requires a CAD conversion
  backend" / model imported tiny or 1000x too big / inches; "ERR_CONNECTION_TIMED_OUT"
  / service down / port collision; "generation fell back to local/qwen" /
  "codex not found" / codex auth or quota; organic mode "503 not installed" /
  "502 organic generation failed" / GPU busy / HF download hangs; import from
  Printables / Thingiverse / MakerWorld fails; emboss/relief invisible in renders;
  "/validate flagged a collision" on a snap-fit. Also covers the meta-traps
  (pgrep -x codex, 10-min Bash timeout, grep -o on 3MF) and a first-60-seconds
  triage checklist. Files: app.py, parts.py, prompts.py, run.sh, library/.
---

# PrintForge debugging playbook

You are debugging a LIVE service. Read the safety line first, then triage.

**PrintForge** = a FastAPI app (`app.py`, ~1490 lines) that turns an English
prompt (+ optional photo / base meshes) into parametric OpenSCAD, renders and
validates it, runs a vision-QA fix loop, and exports STL/3MF for Bambu Studio.
It runs as systemd **user** service `printforge` on port **8093** (binds
0.0.0.0), used over LAN by a second real person. Filesystem is the only store:
`library/<12-hex>/` (accepted models), `uploads/<12-hex>.*` (imports),
`/tmp/printforge` (scratch, `WORK_DIR`).

## SAFETY — read before you touch anything

- A collaborator is on this service over the LAN. Diagnose with READ-ONLY moves.
- **GET** on `http://localhost:8093` is fine. Never POST/PUT/DELETE while
  probing — that mutates real user data and can grab the shared GPU.
- Do not `systemctl restart`, run `run.sh`, run `codex exec`, or trigger
  `/organic` just to reproduce. Restart is a deploy; deploys go through
  **printforge-change-control**, not this playbook.
- `openscad` is NOT on the host PATH. It exists only inside run.sh's nix shell.
  To render test geometry yourself, write outputs to your own `/tmp`, never the
  repo: `nix shell nixpkgs#openscad-unstable --command openscad ...`.

## First 60 seconds (run these before theorizing)

```sh
# 1. Is the service even up? (user scope — NOT `systemctl status printforge`)
systemctl --user is-active printforge          # want: active
systemctl --user status printforge --no-pager | head -20

# 2. What does the service think it can do?
curl -s http://localhost:8093/config           # {"bambuddy":true,"organic":true} = healthy
#    (as of 2026-07-06 that is the live answer; false = that subsystem is down)

# 3. Recent errors / which backend answered / tracebacks
journalctl --user -u printforge -n 80 --no-pager

# 4. Which model produced the last accepted model? (backend field in meta)
ls -t /home/cody/projects/printforge/library/*/meta.json | head -1 | \
  xargs grep -o '"backend": *"[^"]*"'
#    "codex/gpt-5.5" = primary (good).  "local/..." = fell back to qwen (row 9).
```

`backend` is written from `LAST_BACKEND` (app.py:148) into meta.json at
app.py:1127. Its values: `codex/gpt-5.5`, `codex/gpt-5.5 (edit)` (a refine),
`local/<model>` (qwen fallback), or `none`.

## Symptom → cause → discriminating experiment → fix

Every "experiment" below is read-only and copy-pasteable. Cause and fix cite
`file:line` you can open. Anything that changes runtime behavior routes through
**printforge-change-control**.

### 1. Text / geometry renders as garbage blobs

- **Likely cause:** OpenSCAD is too old. `textmetrics()` (used by the prompt
  contract to size geometry around text) silently produces junk on OpenSCAD
  **2021.01** (Debian apt default AND old nixpkgs) — it does NOT error. The host
  must run `openscad-unstable` (2024+) with `--enable=textmetrics`.
- **Experiment:**
  ```sh
  nix shell nixpkgs#openscad-unstable --command openscad --version   # want 2024+
  grep -n 'OPENSCAD_ARGS' /home/cody/projects/printforge/app.py       # app.py:34
  # default is "--enable=textmetrics --enable=manifold"; env can blank it
  journalctl --user -u printforge | grep -i 'textmetrics\|WARNING.*text'
  ```
  Docker path is a red herring here: `compose.yaml` deliberately sets
  `OPENSCAD_ARGS=""` because Debian's 2021.01 has no textmetrics and would crash
  on the flag — that path accepts cruder text sizing.
- **Fix:** ensure `run.sh` launches under `nix shell nixpkgs#openscad-unstable`
  (it does) and `OPENSCAD_ARGS` includes `--enable=textmetrics`. If someone set
  `OPENSCAD_ARGS=""` on the host, that is the bug.
- *Origin:* textmetrics turns to garbage geometry without erroring on 2021.01,
  so the whole host stack is pinned to openscad-unstable.

### 2. A refine dropped an UNRELATED feature (wheel/robot/chest vanished)

- **Likely cause:** a full-file LLM rewrite. Asking any LLM to re-print a long
  `.scad` reliably loses unrelated modules. Refines/QA are supposed to run
  codex **editing the file in place** (`call_codex_edit`, app.py:119, which uses
  `codex exec -C <job> -s workspace-write`), NOT re-printing it.
- **Experiment — which path ran?**
  ```sh
  grep -o '"backend": *"[^"]*"' library/<id>/meta.json   # "(edit)" = edit path ran
  journalctl --user -u printforge | grep -i 'codex edit\|call_codex_edit\|workspace-write'
  # confirm the edit path is even reachable — it requires codex:
  grep -n 'LLM_BACKEND == "codex"' app.py                # app.py:999, 1015
  ```
  If `LLM_BACKEND` is not `codex` (e.g. fell back to qwen, row 9), refines take
  the non-edit path and full-file loss returns.
- **Fix:** keep `LLM_BACKEND=codex`. Never reintroduce whole-file rewrite for
  refines. A size guard alone is not enough (QA rounds also re-printed the file
  historically — that is why both go through the edit path now).
- *Origin:* refines of a ~400-line boat wiped the robot/wheel/chest twice; the
  cure was in-place codex edits instead of re-printing.

### 3. QA "fixed" something the user did on PURPOSE (deleted cabin came back)

- **Likely cause:** QA diffed against the wrong base. If QA compares to the
  PRISTINE uploaded mesh instead of the previously-accepted state, every
  intentional change reads as damage. Also: missing **intent lineage** — a
  refine that doesn't carry `parent_id` loses the "never revert these" history.
- **Experiment:**
  ```sh
  grep -n 'parent_id' app.py            # app.py:58 field; 450 load_intent; 543 intent_block
  grep -n 'current_scad\|previous' app.py | sed -n '1,12p'   # QA diffs vs current, ~1087-1091
  grep -o '"intent":' library/<id>/meta.json     # is accepted-decision history present?
  ```
  Open the request: if the frontend fired a refine WITHOUT `parent_id`, intent
  and rules never load (`load_intent`/`load_rules` return `[]`) and QA has no
  memory of the deletion.
- **Fix:** refines must send `parent_id` = the library id they build on. QA diffs
  against the previous state; meta `intent` is injected as "never revert these".
  Watch also for QA inventing support pillars INSIDE hollow voids.
- *Origin:* a QA round "restored" a cabin the user removed, and once invented a
  pillar inside the hull — both from diffing the wrong base / goals without
  lineage.

### 4. Refine "didn't change much" / model is editing blind

- **Likely cause:** the reviewer had no picture of the CURRENT model — QA was
  handed only the base render, or renders failed, so it edited blind.
- **Experiment:**
  ```sh
  journalctl --user -u printforge | grep -i 'render\|vision_qa\|no image\|timeout'
  grep -n 'def vision_qa\|def mesh_changes\|def _cluster_bboxes' app.py  # 356 / 337 / 318
  ```
  QA renders the current `.scad`, diffs meshes (`mesh_changes`, app.py:337) and
  feeds per-cluster ZOOMED close-ups (`_cluster_bboxes`, app.py:318) as the
  reviewer's first images. If those renders returned None (OpenSCAD timeout, bad
  geometry), the reviewer sees nothing and nudges nothing.
- **Fix:** confirm renders are attached and non-empty in the logs before blaming
  the model. A 10mm addition is a smudge in a whole-model view (that is WHY the
  close-ups exist) — absence of close-ups is the smell.
- *Origin:* a 10mm robot kept failing review as a smudge in full views; per-
  cluster close-ups off a clean mesh diff were the fix.

### 5. Parts collapsed into flat slivers / floating fragments

- **Likely cause:** the z-scale footprint recipe resurfacing. `scale([1,1,big])
  import(...)` stretches the mesh's BOTTOM SLICE, not its outline — clipped
  features collapse into slivers and disconnect.
- **Experiment:**
  ```sh
  grep -n 'scale(\[1,1' library/<id>/model.scad       # smoking gun in a generated file
  grep -n 'z-scaling stretches\|projection() import' prompts.py   # rule 14b, ~line 53-58
  ```
- **Fix:** the prompt contract already BANS the scale recipe (prompts.py rule
  14b). The correct construct is `linear_extrude(h) projection() import(path)`
  (slow) but picking coordinates from the provided cross-sections is almost
  always better. Fix the offending `.scad` deterministically by hand (clip →
  pass-through) rather than re-prompting.
- *Origin:* a footprint recipe the maintainer himself added collapsed a user's
  flag/chest/cleat into slivers and got the 3MF rejected — recipes in prompts.py
  are executable law, so a wrong one is a factory for broken models.

### 6. Slicer / Bambu rejects the 3MF, or reports "floating regions"

- **Likely cause:** disconnected geometry — a part perched above the deck with
  nothing under it (LLMs place parts at estimated z; the "embed 2–3mm" rule
  matters). `floating_starts()` (parts.py:67) is the detector.
- **Experiment (safe, writes only to a tempdir):**
  ```sh
  cd /home/cody/projects/printforge && uv run --with trimesh --with numpy \
    --with scipy --with shapely --with rtree --with networkx python parts.py
  grep -n 'def floating_starts' parts.py     # parts.py:67
  ```
  Params (parts.py:67): `report_area=4.0`, `min_area=0.3`, two-layer lookback
  (`tol=0.8`), per-XY-column dedupe — algorithm details are canonical in
  printforge-mesh-geometry-reference; re-verify there if these numbers matter.
- **Triage law (do NOT over-fix):** disconnected parts = **HARD BLOCK**, must
  fix. Small overhangs / islands **under 4 mm²** = SOFT — tree supports handle
  them, that is spot-support territory, leave them. After **>2** LLM fix rounds,
  STOP and fix the `.scad` by hand.
- *Origin:* the boat had 16 real floats, operator-reported (mast base hovering
  0.5–2mm); calibrating to >4mm² with a 2-layer lookback stopped thin cylinders
  spamming false hits.

### 7. Upload fails "no 3D geometry" or imports at the wrong size

- **Likely causes & experiments:**
  ```sh
  grep -n 'no 3D geometry\|CAD conversion backend\|apply_scale(1000)' app.py
  ```
  - `422 "contains no 3D geometry ... is it an empty or 2D-only file?"`
    (app.py:641) → the file is 2D-only (a flat SVG/DXF outline, or an empty STL).
  - `415 "STEP import requires a CAD conversion backend"` (app.py:605) →
    `cascadio` (trimesh's OpenCascade backend) is missing. It must be in run.sh
    deps (`--with cascadio`); a service restart is needed after adding it.
  - Model imports **1000× too big or too small** → STEP units. cascadio emits GLB
    in **meters**, STEP dimensions are **mm**; import applies `apply_scale(1000)`
    + an X-axis rotation (app.py:615). If that scale is missing/doubled, size is
    wrong.
  - Model imports **~25× too small** → the source file was in **inches**, not mm
    (nothing auto-detects this). Check reported `bbox_mm` (app.py:203) — a
    "keychain" reading 3×1×0.2 mm is an inches file.
- **Fix:** for units, correct via the model's own scale param or re-import; do NOT
  hand-hack cascadio. For missing cascadio, add the dep in run.sh AND Dockerfile
  (dependency changes land in both) via change-control.
- *Origin:* cascadio's meters-vs-mm mismatch made STEP imports arrive 1000× off
  until the ×1000 scale was added.

### 8. ERR_CONNECTION_TIMED_OUT / service unreachable

- **Likely cause:** service down or a port collision. Historically caused by
  running `run.sh` as background shell tasks that piled up until port 8093
  collided — that outage drove the systemd migration.
- **Experiment:**
  ```sh
  systemctl --user is-active printforge
  journalctl --user -u printforge -n 40 --no-pager | grep -i 'address already in use\|bind'
  ss -ltnp | grep 8093        # who holds the port
  ```
- **Fix:** it is a systemd user service now — `systemctl --user restart
  printforge` (a deploy: route through change-control). **Lingering gotcha:**
  if `loginctl show-user cody -p Linger --value` is `no`, a full logout KILLS
  the service — check it before blaming a crash (it was `yes` on 2026-07-07;
  the state has flipped before; printforge-run-and-operate owns this fact).
- *Origin:* a week of background `run.sh` tasks accumulated into a port collision
  the collaborator saw as a timeout; systemd (Restart=on-failure) replaced them.

### 9. Generation fell back to local/qwen, or errors "codex not found"

- **Likely cause:** codex unreachable from the service. Common: systemd does NOT
  inherit the login-shell PATH, so `codex` (in `~/.local/npm/bin`) isn't found;
  or codex auth/quota is exhausted.
- **Experiment:**
  ```sh
  grep -n 'export PATH' run.sh          # run.sh must prepend ~/.local/npm/bin (commit e072c3f)
  grep -o '"backend": *"[^"]*"' library/<id>/meta.json   # "local/..." = it fell back
  journalctl --user -u printforge | grep -i 'codex backend failed\|falling back\|not found\|quota\|auth'
  ```
  The service catches a codex failure and switches to qwen (app.py:161-162),
  setting `LAST_BACKEND=local/...`. Note: **image input hard-requires codex**
  (app.py:164) — a photo prompt with codex down returns 422/502, no fallback.
- **Fix:** ensure run.sh exports `PATH="$HOME/.local/npm/bin:...`" (it does — that
  is exactly what e072c3f fixed) and codex is authenticated. qwen is a
  deliberate rate-limit lifeboat, fine for simple brackets but inadequate for
  multi-part; don't ship multi-part work off a `local/` backend.
- *Origin:* systemd's stripped PATH hid codex from the service until run.sh began
  exporting the npm bin dir (e072c3f).

### 10. Organic mode: 503 or 502

- **503 "organic mode not installed — run organic/setup.sh once"** (app.py:1281):
  the venv is missing.
  ```sh
  ls /home/cody/projects/printforge/organic/.venv/bin/python   # _organic_ready checks this (app.py:1258)
  curl -s http://localhost:8093/config                          # "organic":false confirms
  ```
- **502 "organic generation failed"** (app.py:1301): the subprocess ran and
  errored. Read the tail it prints. Usual suspects:
  - **Missing CUDA/libGL libs** → `ORGANIC_LIBS` (set in run.sh via `nix build
    ... libglvnd`) becomes `LD_LIBRARY_PATH` for the venv (app.py:1293). If it's
    empty, torch can't find CUDA.
  - **GPU held by ollama** → `_free_gpu()` (app.py:1266) asks ollama (`:11434`) to
    unload first; if ollama is wedged, inference OOMs.
  - **Weights missing** → they live in `~/.cache/hy3dgen/tencent`, NOT the HF hub
    cache. `ls ~/.cache/hy3dgen/tencent`.
  - **HF download HANGS silently** → lab AdGuard blocks `us.aws.cdn.hf.co`
    (resolves to `::`). It never errors, it just hangs. `dig us.aws.cdn.hf.co`
    returning `::` is the tell; workaround is `curl --resolve` to a DoH IP or
    whitelist the domain.
- *Origin:* getting Hunyuan3D-2mini onto the 3090 was a CUDA/libGL/opencv gauntlet
  and the AdGuard block made weight downloads hang with no error at all.

### 11. Import-from-URL fails for a specific site

- **Experiment:** `grep -n 'printables\|thingiverse\|makerworld' app.py` (703 /
  724 / import_url 740).
  - **Printables** works KEYLESS via GraphQL at `https://api.printables.com/graphql/`
    (app.py:703) — the **trailing slash is required** and the `getDownloadLink`
    mutation needs `source: model_detail` (app.py:715). If it 502s "did not
    return a download link," those two quirks are the first thing to check.
  - **Thingiverse** needs `THINGIVERSE_TOKEN` in `.env` — without it you get a
    400 telling you to create a token (app.py:727).
  - **MakerWorld** is **hard-blocked** (no API + Cloudflare). Not a bug to fix —
    download via Bambu Studio and attach the file. Cults3D/MMF need logins too.
- *Origin:* each site's auth/API shape was reverse-engineered once; MakerWorld's
  Cloudflare wall is why "attach the file" is the documented answer.

### 12. Emboss / relief looks perfect in renders but is ABSENT on the print

- **Likely cause:** the **ortho-camera trap**. Straight-on **orthographic**
  projection cannot show low relief — text can read as "present" in a render and
  be absent or garbage in reality. This fooled the maintainer's own verification,
  not just the LLM.
- **Experiment:** inspect obliquely. `render_png(..., ortho=False)` (app.py:303;
  QA uses oblique perspective at app.py:387/421). Re-render your suspect model
  with a perspective, angled camera to your own /tmp:
  ```sh
  nix shell nixpkgs#openscad-unstable --command openscad \
    --projection p --camera 0,0,0,70,0,25,340 --imgsize 1000,750 \
    -o /tmp/relief-check.png library/<id>/model.scad
  ```
- **Fix (geometry):** raised text must START INSIDE the body and cross the
  surface (prompts.py rule 14a); for a vertical side use the EMPIRICALLY VERIFIED
  recipe in prompts.py rule 14f (`rotate([90,0,0]) mirror([1,0,0])` for the +Y
  side; mirror for -Y). Do NOT let the LLM derive its own rotation algebra.
- *Origin:* embossed text "verified present" in ortho renders was blank on the
  print; QA cameras were switched to oblique perspective and always inspect
  relief at an angle now.

### 13. /validate flags a collision on a snap-fit (known FALSE POSITIVE)

- **Likely cause:** `/validate` (app.py:1438) checks pairwise part collisions via
  manifold3d booleans. A snap-fit lid interferes BY DESIGN (~12 mm³ on the first
  live test — operator-reported, not re-measured) — correct geometry, flagged anyway.
- **Experiment:** `grep -n 'def validate\|combinations\|intersection' app.py`
  (~1438). A tiny intersection volume on parts that are SUPPOSED to mate is the
  signature.
- **Fix:** this is an OPEN, known false positive — do not "fix" the model to
  satisfy it. Touching pairs are already excluded from the Fix-button feed
  (commit e1ed7e3); "teach the validator about intentional joints" is a tracked
  open item, not a today-bug.
- *Origin:* a designed snap-fit lid tripped the collision check on the first
  assembly test; excluding touching pairs from the fix feed was the interim call.

## Meta-traps (they will waste an hour each)

- **`pgrep -x codex`, not `pgrep -f`.** `pgrep -f 'codex exec'` matches the
  watcher/your own grep too and lies about whether a generation is running. Use
  `pgrep -x codex` (matches the process name exactly). *Origin: `-f` kept
  matching the watching command itself.*
- **The 10-minute Bash timeout vs long generations.** Agent Bash calls cap at
  ~10 min; a codex generation/QA loop runs longer (edit path timeout is 900s,
  app.py:143) and creative asks take 5–10 min. Do NOT block on the HTTP call —
  **fire and poll the library**: the model auto-saves to `library/` even if the
  HTTP client dies. Watch with `ls -t library/ | head`. *Origin: creative
  additions were made a multi-round loop, pushing runtime past any short client
  timeout.*
- **3MF XML is ONE line — `grep -c` undercounts.** To count objects use
  `grep -o '<object' file.3mf | wc -l`, never `grep -c` (which counts matching
  *lines*, and there is only one). *Origin: a one-line 3MF made grep -c report
  "1 object" for a multi-part model.*

## When NOT to use this skill

- The service is fine and you want to CHANGE behavior (deploy, add a flag, edit a
  prompt rule) → **printforge-change-control** (gates + deploy protocol) and, for
  flags/env, **printforge-config-and-flags**.
- You want the FULL story of a settled battle (root cause, evidence, commit) not
  just a triage row → **printforge-failure-archaeology**.
- You need OpenSCAD/CSG semantics or verified recipes (textmetrics, emboss algebra,
  the projection construct) in depth → **printforge-openscad-reference**.
- You need trimesh / 3MF / STEP / slicing internals in depth →
  **printforge-mesh-geometry-reference**.
- Organic mode is your actual project, not a fire → **printforge-organic-quality-campaign**
  (campaign) or **printforge-build-and-env** (recreate the venv/gauntlet).
- You need the measuring tools themselves (renders, diffs, floating-region
  reports) → **printforge-diagnostics-and-tooling**; for evidence standards →
  **printforge-validation-and-qa**.
- Routine systemd/deploy/API operations with nothing broken →
  **printforge-run-and-operate**.

## Provenance and maintenance (re-verify when the repo drifts)

One-liners; run from `/home/cody/projects/printforge`. If a line's output no
longer matches this skill, that row is stale.

```sh
systemctl --user is-active printforge; curl -s http://localhost:8093/config   # live state
grep -n 'OPENSCAD_ARGS\|LAST_BACKEND\|LLM_BACKEND' app.py                      # 34 / 148 / 24
grep -n 'def floating_starts' parts.py                                        # 67; check report_area=4.0
grep -n 'def call_codex_edit\|def vision_qa\|def mesh_changes\|intent_block' app.py  # 119/356/337/543
grep -n 'no 3D geometry\|CAD conversion backend\|apply_scale(1000)' app.py    # 641/605/615
grep -n 'organic mode not installed\|organic generation failed\|def _free_gpu' app.py # 1281/1301/1266
grep -n 'z-scaling stretches\|rotate(\[90,0,0\]) mirror' prompts.py           # rule 14b / 14f
grep -n 'printables\|thingiverse\|makerworld' app.py                          # import quirks
grep -n 'export PATH' run.sh                                                   # codex on PATH (e072c3f)
ls library | wc -l                                                            # 53 as of 2026-07-06
```
