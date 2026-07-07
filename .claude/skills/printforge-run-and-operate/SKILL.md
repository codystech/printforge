---
name: printforge-run-and-operate
description: >
  Operate the live PrintForge service — ROUTINE ops: how to start, stop, deploy,
  restart; the systemd user unit printforge, run.sh, journalctl logs, codex jobs,
  Bambuddy sending, organic availability, library/uploads/artifact locations, and
  the /generate /render /stl /export /models /upload-mesh /import-url /profiles
  /presets /validate /organic /send HTTP API reference. For WHY an unexpected
  symptom happens (ERR_CONNECTION_TIMED_OUT, crashes, wrong output — root-cause
  triage), use printforge-debugging-playbook; this skill covers the mechanics of
  operating, plus API error meanings like "BAMBUDDY_API_KEY not set", "OpenSCAD
  render timed out", "bad id", "not found", "organic mode not installed".
---

# PrintForge run and operate

## Prime operational fact

PrintForge is a LIVE LAN service used by a second real person. Treat every
restart, delete, upload, profile edit, preset edit, or library mutation as
production work.

Deploy protocol (the gate itself is owned by printforge-change-control — do not
deploy without its classification/sign-off rules; if the two skills ever
disagree on this sequence, change-control wins):

```sh
cd /home/cody/projects/printforge

# Expect no output. Non-empty means a generation is running; wait.
pgrep -x codex

# Restart only after edits are complete and no codex job is running.
systemctl --user restart printforge

# Verify service health.
curl -s http://localhost:8093/config
journalctl --user -u printforge -n 50 --no-pager
```

Never restart while generation is running. `codex` generation jobs commonly run
5-10 minutes as of 2026-07-06, and a restart kills in-flight work. Use
`pgrep -x codex`, not `pgrep -f 'codex exec'`; the `-f` form can match your
watch command.

Never run `./run.sh` or `uvicorn` manually alongside the systemd service. The
port-collision outage happened because background `run.sh` processes accumulated
next to the live service, port 8093 stopped answering, and the collaborator saw
`ERR_CONNECTION_TIMED_OUT`. The only supported live run path is the systemd user
unit.

Jargon defined once:

| Term | Meaning |
|---|---|
| systemd user unit | A per-user service managed by `systemctl --user`; here it owns the live PrintForge process. |
| artifact | A generated file: SCAD source, STL mesh, thumbnail, JSON metadata, 3MF/OBJ/GLB export, or uploaded mesh. |
| `stl_id` | A temporary 32-hex render id stored in `/tmp/printforge`; it can disappear on reboot. |
| model/upload id | A persistent 12-hex id under `library/` or `uploads/`. |
| Bambuddy | The print-archive server configured by `BAMBUDDY_URL`; PrintForge uploads 3MF archives there. |

## When NOT to use this skill

| Need | Use instead |
|---|---|
| Deciding whether a code/config behavior change is allowed, or how to validate before deploy | `printforge-change-control` |
| Rebuilding Nix/uv/Docker/organic dependencies from scratch | `printforge-build-and-env` |
| Adding or auditing env vars, flags, profiles, presets, or timeouts | `printforge-config-and-flags` |
| Debugging a specific failure after you have the symptom/log/error | `printforge-debugging-playbook` |
| Checking geometry, printability, OpenSCAD, 3MF, floating regions, or collisions | `printforge-validation-and-qa`, `printforge-mesh-geometry-reference`, `printforge-openscad-reference` |
| Measuring with shipped scripts/tools | `printforge-diagnostics-and-tooling` |

## Service management card

Use user-scope systemd only. Do not use `sudo` for this unit.

| Task | Command | Notes |
|---|---|---|
| Check for an active generation | `pgrep -x codex` | Empty output means no `codex` process. Non-empty means wait before restart/stop. |
| Status | `systemctl --user status printforge --no-pager` | Read-only status command; not executed during this skill authoring because local safety rules forbade `systemctl`. Re-run before relying on current live status. |
| Restart after an approved deploy | `systemctl --user restart printforge` | Mutates the live service. Route code/config behavior changes through `printforge-change-control` first. |
| Stop | `systemctl --user stop printforge` | Takes the collaborator's app down. Use only when the owner explicitly asks. |
| Recent logs | `journalctl --user -u printforge -n 50 --no-pager` | Safe first log check after restart. |
| Follow logs | `journalctl --user -u printforge -f` | Watch live requests/errors; stop with Ctrl-C. |
| Verify HTTP is up | `curl -s http://localhost:8093/config` | Healthy live response observed as `{"bambuddy":true,"organic":true}` on 2026-07-07; re-verify because it depends on env and organic install. |

Unit file location:

```text
~/.config/systemd/user/printforge.service
```

Verified unit content:

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

`Restart=on-failure` means systemd tries to bring PrintForge back after a crash,
but it does not protect against bad code that repeatedly crashes. If logs show a
traceback loop, fix the cause; do not leave the service flapping.

Lingering caveat: the 2026-07-06 operational note said user lingering was not
enabled and the suggested-but-not-applied command was:

```sh
loginctl enable-linger cody
```

This fact is volatile. A local recheck during this skill authoring returned
`yes` for:

```sh
loginctl show-user cody -p Linger --value
```

Re-run that command before claiming the service will or will not survive a full
logout.

## Artifact map

The filesystem is the database. Do not edit, delete, or "clean up" these paths
unless the owner explicitly asks.

| Path | Lifetime | Id shape | Contents | Operating rule |
|---|---:|---|---|---|
| `library/<12hex>/model.scad` | Persistent | `[0-9a-f]{12}` | Saved OpenSCAD source | USER DATA. Test/benchmark models must be named with a `test: ` prefix or deleted immediately. |
| `library/<12hex>/meta.json` | Persistent | `[0-9a-f]{12}` | Model metadata | USER DATA and taste corpus; ratings feed future prompt examples. |
| `library/<12hex>/thumb.png` | Persistent | `[0-9a-f]{12}` | 400x300 library thumbnail | Regenerated only by app flows, not by hand. |
| `uploads/<12hex>.stl` | Persistent | `[0-9a-f]{12}` | Normalized uploaded mesh | USER DATA. Imported/uploaded meshes live here. |
| `uploads/<12hex>.json` | Persistent | `[0-9a-f]{12}` | Upload metadata: bounds, role, tris, bodies, sections | Role PATCH mutates this file. |
| `uploads/<12hex>.step` | Persistent when STEP/STP uploaded | `[0-9a-f]{12}` | Original CAD source preserved for STEP/STP | STEP import is preserved; non-STEP originals are converted to STL and removed. |
| `uploads/<12hex>.svg` | Persistent for SVG/trace | `[0-9a-f]{12}` | 2D outline upload | Used by OpenSCAD `import()` with `linear_extrude`. |
| `/tmp/printforge/<32hex>.stl` | Ephemeral | `[0-9a-f]{32}` | Rendered STL preview/export source | `stl_id` points here and can die on reboot. Re-open a library model to re-render a fresh `stl_id`. |
| `/tmp/printforge/*.scad`, `*.png`, `*.3mf`, `*.obj`, `*.glb`, `*.zip`, `codex-*.txt` | Ephemeral | mixed | Scratch files, QA renders, exports, zips, codex outputs | Scratch only. Do not treat as durable. |

Id namespaces are enforced in `app.py`:

| Namespace | Regex | Used by |
|---|---|---|
| Temporary STL/render id | `[0-9a-f]{32}` | `/stl/{stl_id}`, `/export/{stl_id}`, `/send/{stl_id}` via `_stl_path()` |
| Persistent model id | `[0-9a-f]{12}` | `/models/{model_id}` and child routes via `_model_dir()` |
| Persistent upload/mesh id | `[0-9a-f]{12}` | `_mesh_note()` and `PATCH /uploads/{mesh_id}` |

`meta.json` field inventory:

| Field | Where it comes from | Notes |
|---|---|---|
| `id`, `name`, `prompt`, `intent`, `rules`, `parent`, `created` | Initial `save_to_library()` write | `name` starts as prompt prefix, then best-effort autoname can overwrite it. |
| `qa`, `backend`, `report`, `profile` | `/generate` updates meta after render/QA/report | `profile` is a snapshot dict, not just a profile name. |
| `part_state` | `/generate` copies parent state when present; `PUT /models/{id}/parts` mutates it | Locks/suppression state for part modules. |
| `rating` | `POST /models/{id}/rate` | Optional; absent means unrated. Positive ratings can be used as taste examples. |

## HTTP API reference

Rules for operators:

- Treat rows marked "affects live user data" as production mutations.
- Treat rows marked "scratch only" as lower risk, but still avoid them for
  liveness checks because they can invoke OpenSCAD or other expensive work.
- Never call mutating endpoints for probing. Use `GET /config`, `GET /profiles`,
  `GET /models`, and `GET /presets` for safe liveness checks.
- Never call `/generate` just to test liveness; it can take minutes and writes to
  `library/`.

| Method | Path | Purpose | Key params/body | Response shape / failures |
|---|---|---|---|---|
| `POST` | `/upload-mesh` | Upload mesh or SVG; affects live user data in `uploads/`. | Multipart `file`; query `trace=true` turns bitmap logo into SVG via imagemagick+potrace. Supports STL, 3MF, OBJ, GLB/GLTF, STEP/STP, SVG. 100 MB cap. | Upload meta with `id`, `name`, `format` or `kind`, bounds/sections/role. 415 unsupported, 413 too large, 422 unreadable/trace failure. |
| `POST` | `/import-url` | Import remote model URL; affects live user data in `uploads/`. | JSON `{url}`. | Upload meta from `_register_mesh()`. Site matrix below. |
| `GET` | `/profiles` | List printer profiles. | None. | `{profiles:[...], default:"Generic FDM - 220x220x250 PLA"}`. |
| `PUT` | `/profiles/custom` | Save custom printer profile; affects live user data in `profiles.json`. | JSON `{profile:{...}}`; only keys in default profile shape accepted, name capped at 50. | Saved profile dict. |
| `POST` | `/spec` | Ask LLM for an approved spec before generation; no live user data write. | JSON `{prompt, mesh_id?, mesh_ids?, profile?}`. | `{spec, profile, override}`. Calls LLM; no library write. |
| `GET` | `/calibration` | Render tolerance calibration coupon. | None. | `{scad, params, stl_id}` with temp 32-hex `stl_id`. |
| `GET` | `/models/{model_id}/diff` | Compare child model to parent. | 12-hex `model_id`. | `{parent_name, qa:[old,new], report:[old,new], params_changed, params_added, params_removed}`. 404 if no parent. |
| `GET` | `/presets` | Read user measured presets. | None. | `{text}` from `presets.txt` or default text. |
| `PUT` | `/presets` | Save user measured presets; affects live user data in `presets.txt`. | JSON `{text}` capped to 4000 chars. | `{ok:true}`. |
| `POST` | `/generate` | Generate/refine OpenSCAD, render, QA, save to library; affects live user data in `library/`. | JSON `{prompt, current_scad?, image?, mesh_id?, mesh_ids?, parent_id?, profile?}`. `image` is data URL and requires codex backend. | `{scad, params, stl_id, qa, model_id, print_warnings, backend, rules, part_state, lock_violations, report, print_warning_details, profile_used, profile_override}`. Can take 5-10 min. |
| `POST` | `/render` | Render SCAD with slider overrides to temp STL; scratch only. | JSON `{scad, params}`; param names must match `\w+`. | `{stl_id}`. 400 bad param, 422 OpenSCAD error, 504 timeout, 413 STL too large. |
| `GET` | `/stl/{stl_id}` | Fetch temp STL. | 32-hex `stl_id`; optional `?download=1`. | `model/stl` file. 400 bad id, 404 missing scratch file. |
| `GET` | `/export/{stl_id}` | Export temp STL to another mesh/archive format. | 32-hex `stl_id`; query `fmt=3mf|obj|glb|step`. Default `3mf`. | `3mf` splits connected components into multi-part 3MF; `obj`/`glb` via trimesh. `fmt=step` honestly refuses with 400 because output is mesh-only CAD, not BRep. |
| `PATCH` | `/uploads/{mesh_id}` | Set uploaded mesh role; affects live user data in `uploads/<id>.json`. | 12-hex `mesh_id`; JSON `{role}` where role is `printable`, `reference`, `fit_cutout`, `assembly`, or `negative`. | Updated upload meta. 400 bad role/id, 404 missing mesh. |
| `GET` | `/models/{model_id}/zip` | Download a library model directory as zip. | 12-hex `model_id`. | `printforge-<id>.zip` containing files from `library/<id>/`; zip written to `/tmp/printforge`. |
| `POST` | `/models/{model_id}/duplicate` | Duplicate library model; affects live user data in `library/`. | 12-hex `model_id`. | New meta with new `id`, name suffixed `(copy)`, fresh `created`. |
| `POST` | `/send/{stl_id}` | Build 3MF from temp STL and upload to Bambuddy; affects external archive data. | 32-hex `stl_id`; query `name=...` sanitized to 60 chars. | Upstream Bambuddy JSON. 400 if `BAMBUDDY_API_KEY` missing, 502 if Bambuddy returns >=300. |
| `POST` | `/organic` | Image-to-mesh sculpting; affects live user data in `uploads/` and temp `stl_id`. | JSON `{image, target_mm=80}`; image is data URL; target clamped 10-250 mm. | Upload meta plus `stl_id`, `scad`, `params`. 503 if organic venv missing, 400 bad image, 502 subprocess failure. Uses a lock so the RTX 3090 is not shared. |
| `GET` | `/config` | Feature availability probe. | None. | `{bambuddy: bool, organic: bool}`. |
| `GET` | `/models` | List library meta sorted newest first. | None. | Array of `meta.json` objects. |
| `GET` | `/models/{model_id}` | Load model, re-render SCAD, return fresh temp STL id. | 12-hex `model_id`. | `{meta, scad, params, stl_id}`. Re-render writes scratch only. |
| `GET` | `/models/{model_id}/thumb` | Fetch library thumbnail. | 12-hex `model_id`. | `image/png`; 404 if no thumbnail. |
| `POST` | `/models/{model_id}/rate` | Set thumbs rating; affects live user data in `library/<id>/meta.json`. | JSON `{rating}`; clamped to `-1`, `0`, or `1`. | Updated meta. |
| `PATCH` | `/models/{model_id}` | Rename model; affects live user data. | JSON `{name}`; stripped and capped to 60 chars. | Updated meta. |
| `PUT` | `/models/{model_id}/rules` | Save per-model rules; affects live user data. | JSON `{rules:[...]}`; strips blanks, caps to 20. | Updated meta. |
| `PUT` | `/models/{model_id}/parts` | Save part locks/suppression/aliases; affects live user data. | JSON `{part_state:{part:{locked,suppressed,alias}}}`; part names must match `\w+`, caps to 30, alias capped to 40. | Updated meta. |
| `POST` | `/validate` | Render enabled parts in assembled position and check collisions/clearances; scratch only. | JSON `{scad, params}`. Requires `<part>_enabled` toggles. | `{parts, assembled_check, issues}`. 400 if no toggles. Known open false positive: intentional snap-fit interference can be flagged. |
| `DELETE` | `/models/{model_id}` | Delete library model; affects live user data. | 12-hex `model_id`. | `{ok:true}` after removing `library/<id>`. Use only with explicit owner/user intent. |

Import URL support matrix:

| Site/input | Support | Notes |
|---|---|---|
| Printables | Supported keyless | Needs `printables.com/model/<id>-...`; uses `https://api.printables.com/graphql/` and `getDownloadLink(... source: model_detail)`. |
| Thingiverse | Supported with token | Needs `THINGIVERSE_TOKEN`; accepts `thingiverse.com/thing:<id>` or `/thing/<id>`. |
| MakerWorld | Refused | No public API plus Cloudflare; download with Bambu Studio/browser and upload file. |
| Cults3D / MyMiniFactory | Refused | Login/API key required; download in browser and upload file. |
| Direct file URL | Supported for `.stl`, `.3mf`, `.obj` | Follows redirects, then registers mesh. |

Export format matrix:

| `fmt` | Result |
|---|---|
| omitted or `3mf` | Multi-part 3MF using `split_parts()` and `write_3mf()`. |
| `obj` | OBJ mesh exported by trimesh. |
| `glb` | GLB mesh exported by trimesh. |
| `step` | 400 refusal: STEP export would imply CAD/BRep output, but the pipeline has mesh-only output. |
| anything else | 400 unknown format. |

## Long-generation operating pattern

Use this when a client, browser tab, or agent times out during `/generate`:

```sh
# Do not restart first. Check whether codex is still working.
pgrep -x codex

# Poll the library newest-first. Use jq if available.
curl -s http://localhost:8093/models | jq '.[0] | {id,name,created,backend,qa,report}'
```

Observed operational behavior as of 2026-07-06: HTTP client death does not
necessarily kill the server-side generation, and successful jobs auto-save into
`library/` before returning. This was not reproduced during this skill authoring
because safe verification rules prohibit `POST /generate`; re-verify before
building tooling that depends on disconnect semantics.

Agent/tooling timeout note: some agent shells cap command execution near 10
minutes. A long creative refine can outlive the tool call. Fire the request,
then poll `GET /models` for the newest entry instead of assuming failure.

## Bambuddy integration

Bambuddy is the print archive server configured by:

| Config | Source | Current behavior |
|---|---|---|
| `BAMBUDDY_URL` | `app.py` env default | Defaults to `http://192.168.1.50:8000`. |
| `BAMBUDDY_API_KEY` | `.env`, sourced/exported by `run.sh` | Empty key disables sending; live `GET /config` returned `bambuddy:true` on 2026-07-07. Never print or inline the key. |

`POST /send/{stl_id}` does this:

1. Validate `stl_id` as a 32-hex temp STL id.
2. Build a 3MF in `/tmp/printforge` by splitting connected components.
3. Sanitize `name` to word/dash/space characters, max 60 chars.
4. Upload `safe-name.3mf` to `${BAMBUDDY_URL}/api/v1/archives/upload` with
   bearer auth.

Failure modes:

| Failure | HTTP result |
|---|---|
| Missing `BAMBUDDY_API_KEY` | 400 `"BAMBUDDY_API_KEY not set"` |
| Missing/expired temp `stl_id` | 400 bad id or 404 not found before upload |
| Bambuddy upstream returns >=300 | 502 `"Bambuddy upload failed (...)"` with response text prefix |

## Fast safe checks

These commands do not mutate PrintForge:

```sh
cd /home/cody/projects/printforge
grep -n '@app\.' app.py
curl -s http://localhost:8093/config
curl -s http://localhost:8093/profiles | head -c 1000
curl -s http://localhost:8093/presets
curl -s http://localhost:8093/models | jq 'length'
journalctl --user -u printforge -n 20 --no-pager
pgrep -x codex
```

Do not use `curl -X POST`, `PUT`, `PATCH`, or `DELETE` for health checks.

## Provenance and maintenance

Re-run these one-liners before trusting drift-prone claims:

```sh
# Route inventory and line numbers
cd /home/cody/projects/printforge && grep -n '@app\.' app.py

# Request/response models, id regexes, paths, Bambuddy, organic, params, metadata writes
cd /home/cody/projects/printforge && rg -n 'WORK_DIR|LIB_DIR|UPLOADS_DIR|BAMBUDDY|PARAM_RE|fullmatch|def save_to_library|meta\.update|@app\.|class .*Request|_register_mesh|_register_svg|_stl_path|_model_dir|set_mesh_role|send_to_bambuddy|organic|import_url' app.py

# Unit file content
sed -n '1,120p' ~/.config/systemd/user/printforge.service

# Host run command and exported env
cd /home/cody/projects/printforge && sed -n '1,120p' run.sh

# README-supported features and API summary
cd /home/cody/projects/printforge && sed -n '60,116p' README.md

# Live feature flags: safe GET only
curl -s http://localhost:8093/config

# Live profiles/presets/models: safe GET only
curl -s http://localhost:8093/profiles | head -c 2000
curl -s http://localhost:8093/presets
curl -s http://localhost:8093/models | head -c 2000

# Recent logs and active generation check
journalctl --user -u printforge -n 50 --no-pager
pgrep -x codex

# Linger state; volatile and already drifted once
loginctl show-user cody -p Linger --value

# Artifact shape sampling without editing user data
find /home/cody/projects/printforge/library -maxdepth 2 -type f | sed -n '1,80p'
find /home/cody/projects/printforge/uploads -maxdepth 1 -type f | sed -n '1,80p'
find /tmp/printforge -maxdepth 1 -type f 2>/dev/null | sed -n '1,80p'
```

Claims not executed here: `systemctl --user status/restart/stop printforge`,
`POST /generate`, `POST /send/{stl_id}`, and all other mutating API calls. Re-run
only with owner intent and the live-user/restart rules above.
