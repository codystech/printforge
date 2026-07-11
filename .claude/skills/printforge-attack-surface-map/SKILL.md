---
name: printforge-attack-surface-map
description: >
  The map of PrintForge's attack surface for security review: every HTTP route, every
  place it shells out (subprocess sinks), every server-side outbound fetch, every file
  it reads/writes, and where secrets live. Load this SECOND (after
  printforge-security-scope-and-rules) whenever you need to know "what can an attacker
  reach", "where does user input flow", "what are the routes", "what does it shell out
  to", "where are the dangerous sinks", or before starting any per-class review. It
  routes each surface to the sibling skill that owns the deep review. Grounded in
  app.py with file:line anchors; treat line numbers as drift-prone and re-verify with
  the grep commands in Provenance.
---

# PrintForge attack surface map

Read `printforge-security-scope-and-rules` first. This skill is the **inventory of what
to test**. For each surface it gives the code anchor and the sibling skill that owns the
deep dive. It is a map, not a verdict — nothing here is a confirmed bug until a per-class
skill proves it.

Working directory for every command: `~/projects/printforge`.

Architecture in one breath: one process, `app.py` (~1500 lines), FastAPI + uvicorn,
port 8093, **no auth**. Static UI at `static/index.html`. Data on disk:
`WORK_DIR = <tmp>/printforge` (`app.py:35`), `LIB_DIR = ./library` (`:37`),
`UPLOADS_DIR = ./uploads` (`:39`). External brains: `codex` CLI (gpt-5.5) or LiteLLM
HTTP (`:4000`), plus `magick`, `potrace`, `openscad`, `nix`.

## 1. HTTP routes (the front door)

Regenerate the list any time: `grep -nE '@app\.(get|post|put|patch|delete)' app.py`.
As of this writing:

| Route | Method | What it does | Primary review skill |
|---|---|---|---|
| `/upload-mesh` (`:686`) | POST | accept uploaded mesh/bitmap file | file-upload-download |
| `/import-url` (`:739`) | POST | **fetch a URL server-side** | ssrf-and-fetch ⚠ |
| `/profiles` (`:859`), `/profiles/custom` (`:868`) | GET/PUT | printer profiles | authn-authz, input |
| `/spec` (`:884`) | POST | prompt → editable spec (LLM) | input |
| `/calibration` (`:922`) | GET | tolerance coupon | low |
| `/models/{id}/diff` (`:929`) | GET | version diff | authn-authz (IDOR) |
| `/presets` (`:951`,`:960`) | GET/PUT | user measured presets | input, secrets(logging) |
| `/generate` (`:987`) | POST | prompt(+image,+scad) → codex/LLM → OpenSCAD | input, injection ⚠ |
| `/render` (`:1149`) | POST | scad + slider params → `openscad -D` → STL | input (OpenSCAD -D) |
| `/stl/{id}` (`:1164`) | GET | download rendered STL | file-upload-download |
| `/export/{id}` (`:1176`) | GET | 3MF/OBJ/GLB export | file-upload-download |
| `/uploads/{id}` (`:1195`) | PATCH | set mesh role | authn-authz, input |
| `/models/{id}/zip` (`:1211`) | GET | zip a library model | file-upload-download |
| `/models/{id}/duplicate` (`:1222`) | POST | copy a model | authn-authz |
| `/send/{id}` (`:1236`) | POST | **upload 3MF to Bambuddy** | ssrf-and-fetch, secrets |
| `/organic` (`:1278`) | POST | image → local GPU mesh | input, dependency |
| `/config` (`:1312`) | GET | expose config to UI | secrets ⚠ (check what it returns) |
| `/models` (`:1326`), `/models/{id}` (`:1336`) | GET | list / fetch model | authn-authz (IDOR) |
| `/models/{id}/thumb` (`:1345`) | GET | thumbnail | file-upload-download |
| `/models/{id}/rate` (`:1357`) | POST | 👍/👎 | low |
| `/models/{id}` (`:1392`) | PATCH | rename | input |
| `/models/{id}/rules` (`:1405`) | PUT | per-model rules | input |
| `/models/{id}/parts` (`:1418`) | PUT | part states | input |
| `/validate` (`:1438`) | POST | assembly validation (renders) | input |
| `/models/{id}` (`:1484`) | DELETE | **delete a model** | authn-authz (destructive, no auth) |

⚠ = highest-value surfaces; start there.

## 2. Subprocess sinks (where it shells out)

`grep -nE 'subprocess|Popen|check_output' app.py`. All confirmed to use **argv lists,
never `shell=True`** — this blocks classic shell-metacharacter injection (a key
false-positive trap; see input skill). The sinks:

| Sink | Line | Command | Sandbox / limit |
|---|---|---|---|
| `call_codex` | `:108-113` | `codex exec -s read-only --skip-git-repo-check --ephemeral` | codex **read-only** sandbox; 420s timeout |
| `call_codex_edit` | `:134-141` | `codex exec -C <job> -s workspace-write` | codex **workspace-write** scratch dir; 900s |
| `render_stl` | `:186-187` | `openscad ... -D k=v <file>` | key `\w+` validated `:181`; 100MB cap; timeout |
| printability slice | `:312` | `openscad ...` | timeout |
| `_trace_to_svg` | `:581-583` | `magick ...`, `nix shell nixpkgs#potrace ... potrace ...` | argv; 60s/120s timeout |

The LLM writes the OpenSCAD that `openscad` then executes. codex runs it in **its own
sandbox** (read-only for generate, workspace-write scratch for edit) — so LLM-authored
OpenSCAD is not free host RCE, but review the sandbox flags anyway (input skill).

## 3. Server-side outbound fetches (SSRF surface)

`grep -nE 'httpx|client\.(get|post)|follow_redirects' app.py`.

| Fetch | Line | Destination | Attacker-controlled? |
|---|---|---|---|
| `/import-url` else-branch | `:763` | **any URL** ending `.stl/.3mf/.obj`, `follow_redirects=True` | **YES — the core SSRF candidate** |
| `_printables_import` | `~:713` | fixed `api.printables.com` | host fixed, id from URL |
| `_thingiverse_import` | `~:730` | fixed `api.thingiverse.com` | host fixed, id from URL |
| `/send/{id}` → Bambuddy | `:1244` | `BAMBUDDY_URL` (env, default internal VM) | destination from env, not request |
| `_free_gpu` | `:1290s` | `127.0.0.1:11434` ollama | server-controlled |
| LLM HTTP backend | `:168,:971` | `LLM_BASE_URL` (env) | destination from env |

Deep dive: `printforge-ssrf-and-fetch-review`. The `/import-url` else-branch is the one
that matters.

## 4. Filesystem (the "database")

- `LIB_DIR = ./library/` — saved models (`meta.json`, `model.scad`, thumbnails). Git-
  ignored. Reads via `_model_dir` (id `[0-9a-f]{12}` validated, `:1317`).
- `UPLOADS_DIR = ./uploads/` — uploaded meshes/SVGs + `.json` sidecars. id
  `[0-9a-f]{12}` validated (`:793`,`:1200`).
- `WORK_DIR = <tmp>/printforge` — render scratch, codex output, 3MF/zip build. STL id
  `[0-9a-f]{32}` validated (`:1155`).
- `.env` — secrets, git-ignored (`.gitignore`).
- `presets.txt`, `profiles.json` — user data, git-ignored.

**ID validation is real and consistent** — path traversal through `{id}` params is
blocked by the regexes above. This is a false-positive trap: don't report traversal on
these routes without showing the regex is bypassable. See file-upload-download and
authn-authz skills.

## 5. Secrets

`grep -nE 'API_KEY|TOKEN|SECRET|BAMBUDDY' app.py`: `LLM_API_KEY` (`:27`),
`BAMBUDDY_URL`/`BAMBUDDY_API_KEY` (`:30-31`), `THINGIVERSE_TOKEN` (`:725`). All from env
/ `.env` (git-ignored). Check `/config` (`:1312`) does not leak them to the UI, and that
`print(...stderr...)` server logs (`:143`) don't capture them. Deep dive:
`printforge-secrets-and-config-review`.

## 6. Frontend

`static/index.html` (~797 lines), `static/vendor/three.module.js` + STL/OrbitControls.
`innerHTML` sinks at `:228,:231,:608,:695`; `localStorage` at `:471,:774,:783`;
`.src =` at `:512,:700`. Deep dive: `printforge-frontend-security-review`.

## When NOT to use this skill

- You already know which surface you're testing — go straight to that per-class skill.
- You want the authorization rules — that's `printforge-security-scope-and-rules`.
- You want to run a PoC — that's `printforge-security-testing-playbook`.
- You want homelab topology (which VM, which VLAN) — that's `homelab-inventory-reference`
  in the LabOps library, not here.

## Evidence checklist (before you trust this map)

- [ ] Re-ran the route grep; the table still matches.
- [ ] Re-ran the subprocess grep; still argv-list, still no `shell=True`.
- [ ] Confirmed the `/import-url` else-branch still does `follow_redirects=True` with no
      host allowlist (`:763`).
- [ ] Confirmed the id-validation regexes still guard `_stl_path`/`_model_dir`/mesh ids.

## Severity guidance

This skill assigns none — it routes. Severity is decided per finding in
`printforge-security-validation-and-triage` using `printforge-finding-report-template`.

## Reporting template

Use `printforge-finding-report-template`. Reference the exact route + `app.py:line` from
the tables above so the finding is reproducible.

## Provenance and maintenance

Supported by direct reading of `app.py` (routes, sinks, fetches, id validation) and
`static/index.html` (frontend sinks), plus `README.md` "How it works".

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -nE '@app\.(get|post|put|patch|delete)' app.py           # route table
grep -nE 'subprocess|Popen|check_output' app.py               # sinks (expect argv, no shell=True)
grep -nE 'httpx|follow_redirects' app.py                      # outbound fetches
grep -nE 'fullmatch\(r"\[0-9a-f\]' app.py                     # id-validation regexes
grep -nE 'innerHTML|localStorage|\.src *=' static/index.html  # frontend sinks
```

Remaining uncertainty:
- Line numbers drift as `app.py` changes; always re-anchor with grep before citing.
- `/config` (`:1312`) contents were not fully enumerated here — the secrets skill must
  confirm exactly what it returns.
