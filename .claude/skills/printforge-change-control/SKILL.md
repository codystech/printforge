---
name: printforge-change-control
description: >
  How changes are classified, gated, and shipped in PrintForge. Load this BEFORE editing
  ANY file in the repo — especially prompts.py (LLM contract), app.py (pipeline),
  parts.py / organic/ (geometry), static/index.html (UI), run.sh + Dockerfile
  (dependencies), or presets/profiles config. Load it when you are about to deploy
  ("restart the service", "systemctl --user restart printforge"), when you add a Python
  dependency, when you touch anything the live collaborator sees, when you want to change
  a prompt rule / add a recipe, or when you are unsure whether a change needs the owner's
  sign-off. Covers: risk classification per file, the non-negotiable rules (each with the
  incident that created it — z-scale disaster, port-collision outage, benchmark pollution,
  secret classifier-block, feature-loss rewrites, textmetrics silent garbage), the exact
  deploy protocol, README/commit docs-of-record rules, and the sign-off boundary. Symptoms
  that should pull you here: "is this safe to ship", "how do I deploy", "which files does a
  dep change touch", "can I edit the prompt", "do I need to restart", "will this break the
  collaborator".
---

# PrintForge change control

PrintForge is a LIVE self-hosted service (systemd user unit `printforge`, port 8093, binds
`0.0.0.0`) used over the LAN by a second real person — "the collaborator." There is no CI,
no staging, no test suite except `parts.py`'s `__main__` self-check. The filesystem IS the
database. That means **every edit you make ships to a real user the moment you restart the
service, and there is no safety net but this document.** Read the change class, do the
required validation, follow the deploy protocol, update the docs of record. In that order.

This skill is the gate. It does not teach you HOW to validate geometry, prompts, or
meshes — it tells you WHAT class a change is, WHAT proof it needs, and routes you to the
sibling skill that owns the proof (almost always **printforge-validation-and-qa**).

---

## 1. Change classification

Find the file you are about to touch. Do the required validation. Respect the blast radius.

| Change class | Files | Risk | Required validation (route) | Blast radius |
|---|---|---|---|---|
| **a. Prompt contract** | `prompts.py` | **HIGHEST** | Empirical proof the recipe renders + prints correctly on a real example BEFORE it enters the file (printforge-validation-and-qa; recipe verification via printforge-proof-and-analysis-toolkit) | **Every future generation, forever.** A rule here is executable law the LLM applies blindly to all users, all prompts. |
| **b. Generation pipeline** | `app.py` | High | Render + vision-QA a generation end to end; confirm `/config` and a `POST /generate` still work (printforge-validation-and-qa, printforge-run-and-operate) | Every request. A bug takes the whole service down or corrupts saves. |
| **c. Mesh / geometry** | `parts.py`, `organic/` | High | `parts.py` `__main__` self-check MUST pass; test on a real STL, not a cube (printforge-validation-and-qa, printforge-mesh-geometry-reference) | Export, 3MF split, floating/collision detection, organic sculpts. Silent wrong geometry ships to the slicer. |
| **d. UI** | `static/index.html` | Medium | Load `http://localhost:8093/` in a browser; exercise the changed control; check the console (printforge-validation-and-qa) | The collaborator's entire interface. Vanilla JS + vendored three.js — no build step, no type check catches you. |
| **e. Dependency** | **BOTH `run.sh` AND `Dockerfile`** | High | Verify the dep lists match after editing (command below); start the service and hit `/config` (printforge-build-and-env) | A dep in only one path = the other run mode breaks at import time. |
| **f. Config / profile** | `presets.txt`, `profiles.json`, env vars, constants in `app.py` | Medium | Confirm defaults still load; `GET /config`; if it changes generation behavior, treat as class (a)/(b) (printforge-config-and-flags) | Every job inherits profile/preset constraints. Wrong default = wrong constraints silently applied. |

### Class (a) is not like the others

`prompts.py` is the single highest-risk file in the repo. It is the contract the LLM obeys
for **every** model it ever writes. A human bug affects one model; a `prompts.py` bug is a
**factory for broken models** — it reproduces on every future generation until someone
notices and fixes the rule. Rule: **no recipe, construct, or orientation math enters
`prompts.py` until you have rendered it and confirmed the geometry is correct on a real
example.** See non-negotiable #4 for the two incidents that set this rule.

### Class (e): the dependency two-file rule (verify both lists)

Python deps are declared in **two** places and they must stay identical:

- `run.sh` — the `uv run --with <pkg>` flags (host / codex path).
- `Dockerfile` line 4 — the `pip install` list (docker / qwen-only path).

As of 2026-07-06 both lists are exactly these 13 packages:

```
fastapi uvicorn httpx trimesh numpy scipy python-multipart
networkx lxml shapely rtree manifold3d cascadio
```

After any dependency edit, prove the two lists agree — the output must be empty:

```sh
cd /home/cody/projects/printforge
diff <(grep -oE -- '--with [a-zA-Z0-9_-]+' run.sh | awk '{print $2}' | sort) \
     <(grep -oE 'pip install --no-cache-dir .*' Dockerfile | sed 's/pip install --no-cache-dir //' | tr ' ' '\n' | sort)
```

(`openscad` is NOT a pip dep — it comes from `nixpkgs#openscad-unstable` on the host and
from Debian `apt` in the container; see non-negotiable #6.)

---

## 2. Non-negotiables — rule → rationale → incident

Each of these was written in blood. Do not relitigate them; they are settled.

### N1. Deploy = edit + restart + verify. Never run the app in a background shell.
- **Rule:** To ship a change: edit the file, then `systemctl --user restart printforge`,
  then verify (section 3). Never launch `run.sh` or `uvicorn` by hand as a background job.
- **Rationale:** Two processes both want port 8093; the second silently fails or the pair
  collide, and the service stops answering.
- **Incident:** A week of running `run.sh` as background shell tasks accumulated into a
  **port collision — the collaborator got `ERR_CONNECTION_TIMED_OUT`.** The fix was the
  systemd user service (`Restart=on-failure`) that is now the only supported run path.
  (Related: systemd does not inherit the login-shell `PATH`, so `run.sh` exports
  `~/.local/npm/bin` to find `codex` — do not remove that line.)

### N2. `library/` and `uploads/` are USER DATA. Test generations are labeled or deleted.
- **Rule:** Never edit, delete, or pollute `library/` or `uploads/`. Any test/benchmark
  generation you create gets a **`test: ` name prefix** or is deleted immediately.
- **Rationale:** The library is the collaborator's real saved work AND the taste-training
  corpus (liked models feed back as few-shot examples) — junk in it degrades everyone's
  generations.
- **Incident:** The maintainer's benchmark generations polluted the real library and had
  to be renamed after the fact. Several `test: ` entries exist in `library/` today as the
  standing convention. Both dirs are gitignored (`.gitignore`) precisely because they are
  data, not code.

### N3. Secrets only in the gitignored `.env`. Never inline.
- **Rule:** Credentials go in `.env` (gitignored; `run.sh` sources it and exports
  `BAMBUDDY_API_KEY`). Never paste a key into code, a prompt, or chat.
- **Rationale / incident:** A secret pasted inline once got **classifier-blocked**, jamming
  the workflow. The `.env`-sourced-by-`run.sh` pattern is the accepted, unblocked way. The
  only var in `.env` today is `BAMBUDDY_API_KEY`.

### N4. Empirical verification before any recipe enters `prompts.py`.
- **Rule:** No construct, recipe, or orientation math goes into the prompt contract until
  you have rendered it and confirmed the geometry on a real example. Do not let the LLM
  "derive" orientation algebra — give it a recipe you verified.
- **Rationale:** A wrong recipe in `prompts.py` breaks every future model (class (a)).
- **Incidents (two):**
  1. **The z-scale footprint disaster.** A maintainer-added rule told the LLM to "clip to
     the footprint" with `scale([1,1,1000]) import(...)`. z-scaling stretches the *bottom
     slice* of a mesh, not its outline — it collapsed a user's flag/chest/cleat into
     disconnected slivers and **Bambu Studio rejected the 3MF.** The correct construct is
     `linear_extrude(h) projection() import(path)` (slow) or, better, picking coordinates
     from the provided cross-sections. `prompts.py` now explicitly **bans** the scale
     recipe (see the base-mesh fusion rules, `prompts.py:54-58`).
  2. **Emboss orientation algebra.** Two consecutive failed attempts at raised text on a
     curved hull — LLM-derived rotation math is reliably wrong. The fix was an
     **empirically verified** recipe pinned in the contract (`rotate([90,0,0]) mirror([1,0,0])`
     for the +Y side, mirrored for -Y; `prompts.py:69-79`). Do not "improve" it by
     re-deriving.

### N5. Never reintroduce full-file rewrites for refines.
- **Rule:** Refines and QA edit `model.scad` **in place** via codex's file-editing tools
  (`call_codex_edit`, `app.py:119`, runs `codex exec -C <jobdir> -s workspace-write`).
  Never make the LLM re-print a whole file.
- **Rationale / incident:** Asking any LLM to re-print a ~400-line model wholesale reliably
  drops unrelated content — refines **wiped the robot, wheel, and chest modules, twice.** A
  size guard was not enough because QA rounds also re-printed. In-place editing was the fix.
- Note: `prompts.py` rule 7 still says "return the COMPLETE updated file" — that path is for
  the non-codex (qwen) fallback; the codex path (primary) is the in-place editor. Do not
  route codex refines back through whole-file rewrites.

### N6. OpenSCAD must stay `openscad-unstable` with `OPENSCAD_ARGS` on the host.
- **Rule:** The host runs `nixpkgs#openscad-unstable` (2024+) with
  `OPENSCAD_ARGS="--enable=textmetrics --enable=manifold"` (default set in `app.py:34`).
  Do not pin an older OpenSCAD on the host. Docker deliberately blanks `OPENSCAD_ARGS`
  (`compose.yaml`) because Debian's OpenSCAD is 2021.01.
- **Rationale / incident:** OpenSCAD 2021.01 turns `textmetrics()` calls into **garbage
  geometry with no error.** The contract prompts the LLM to use `textmetrics()`
  (`prompts.py:12`), so an old OpenSCAD silently produces broken text sizing.
  `openscad` is not on the host PATH at all — it exists only inside `run.sh`'s nix shell.

### N7. Never hard-block a user-requested cut or reshape of a base mesh.
- **Rule:** When the user asks to cut, hollow, engrave, or creatively reshape a base mesh,
  the pipeline must allow it. Detectors may WARN, but must not refuse.
- **Rationale / incident:** QA once "restored" a cabin the user had deliberately removed —
  it diffed against the pristine upload, so every intentional change looked like damage.
  Creative "damage" is often intent. The fix: QA now diffs against the **previous accepted
  state**, not the original upload (`app.py:1088-1091` re-renders `current_scad` as the
  base), and library meta carries an `intent` history injected as "never revert these"
  (`intent_block`, `app.py:543`). Locks are the opt-in hard constraint, verified by
  brace-count diff after every refine (`lock_violations`, `app.py:499`) — trust the diff,
  not the LLM's promise.

---

## 3. Deploy protocol (exact commands)

Ship a change with these steps, in order. All commands are user-scope systemd
(`--user`) — do not use `sudo`.

```sh
# 0. DO NOT restart mid-generation. codex jobs run 5–10 min and auto-save on completion;
#    a restart kills the job and loses that work. Check first — expect NO output:
pgrep -x codex

# 1. Restart the service to load your edits.
systemctl --user restart printforge

# 2. Confirm it came up clean (look for the uvicorn "Application startup complete" line,
#    no tracebacks).
journalctl --user -u printforge -n 50 --no-pager

# 3. Prove it answers and features are wired.
curl -s http://localhost:8093/config      # → {"bambuddy":true,"organic":true} when healthy
```

- **`pgrep -x codex`** (not `pgrep -f 'codex exec'` — that matches the watcher process
  itself). Non-empty = a generation is running; wait for it before restarting.
- If step 2 shows a traceback, the service will be flapping (`Restart=on-failure`). Read the
  full error, fix, restart again. Do not leave it flapping — the collaborator sees an
  intermittent site.
- **Linger state is volatile — never assume it, check it**: `loginctl show-user cody -p
  Linger --value` (returned `yes` on 2026-07-07, i.e. the service survives logout; it has
  flipped before). printforge-run-and-operate owns this fact.

---

## 4. Docs of record

### README.md is the canonical, kept-current feature list.
- **Any feature change must update `README.md` in the same change.** This is an observed
  convention: README gets its own dedicated commits as features land — e.g.
  `266af68` "README: current through printer profiles" and `8827529` "README: current
  feature list (through Parts Panel v2)". If you add, remove, or materially change a
  feature and do not touch README, the change is incomplete.
- Never contradict README. If your change makes README wrong, fix README.

### Commit-message house style.
- Observe `git log --oneline`: **imperative mood, a single-line feature summary**, no body
  needed for most. Examples from history: `Guard empty-geometry uploads; robust error
  parsing in upload UI`, `Neutral printer profile system`, `Replace broken z-scale
  footprint recipe in prompt rules`. Semicolons separate the sub-changes of one commit.
- Commit/push only when the owner asks (this repo is not auto-committed by sessions).

### The README "Later" section is the official deferred/retired ledger.
- `README.md` ends with **"Later (deliberately not built yet)"** — this is the authoritative
  list of what is intentionally deferred or rejected. **Do not re-propose retired ideas as
  new work.** Retired (do not resurrect): **thin-wall analysis** (the slicer does it
  better), **AMF export** (dead format), **fake STEP export** (dishonest for a mesh-only
  pipeline), scale/rotate/center helpers (slicer's job), **cancel buttons for
  generation** (`codex exec` is not cancellable), and **format/QA library filters**
  (search suffices; this one is absent from README's list but is settled — see
  printforge-failure-archaeology). Still-deferred (fine to pick up with
  sign-off): Sparc3D/TRELLIS.2 organic-backend evaluation, thumbs-down negative examples,
  Bambu paint-color metadata, text→image→3D chaining, CT/NPM/Authelia deploy, in-UI custom
  profile editor.

---

## 5. Sign-off boundary — what a session may do vs what needs the owner

A session may act autonomously on **reversible, low-blast-radius** work. Anything that
touches the live user, spends metered quota in bulk, or destroys data needs the owner's
explicit go-ahead first.

| A session MAY just do it | REQUIRES the owner's sign-off first |
|---|---|
| Read/grep/analyze any repo file | Any change to the live service's **behavior for the collaborator** (prompt-contract edits, pipeline changes, UI changes that ship) |
| `GET` localhost:8093 endpoints for verification | **Restarting the service** while the collaborator may be using it |
| Run `parts.py` self-check (writes only to tempdir) | **Bulk codex spend** — running many `codex exec` generations (metered OpenAI quota) |
| Render throwaway geometry to your own `/tmp` scratch | **Deleting or renaming user data** in `library/` or `uploads/` |
| Draft a change and show the diff | **Reintroducing a retired idea** from the README "Later" ledger |
| Fix an obvious typo in a comment | **Enabling lingering, changing the systemd unit, or the run/deploy path** |

When in doubt, the change touches the live service, or it spends quota / deletes data:
**ask first, deploy second.**

---

## When NOT to use this skill

This skill is the gate and the incident memory. Use a sibling when you need the actual
how-to:

- **HOW to validate a change / evidence standards / golden inventory** →
  printforge-validation-and-qa (this skill routes you there; it owns the proof).
- **A recipe's first-principles verification** (before it enters `prompts.py`) →
  printforge-proof-and-analysis-toolkit.
- **Diagnosing a live failure / symptom→triage** → printforge-debugging-playbook;
  the settled war stories in full → printforge-failure-archaeology.
- **systemd ops, artifact map, API surface** → printforge-run-and-operate.
- **Recreating the host/docker/organic environments** → printforge-build-and-env.
- **What each env var/flag/constant does + add-a-flag checklist** →
  printforge-config-and-flags.
- **OpenSCAD/CSG or trimesh/3MF/STEP domain detail** →
  printforge-openscad-reference / printforge-mesh-geometry-reference.
- **The load-bearing architecture decisions and invariants** →
  printforge-architecture-contract.

Anything that tells you to change behavior routes back through THIS skill for the gate.

---

## Provenance and maintenance

Re-verify these one-liners if the repo has drifted (dates stamped 2026-07-06):

```sh
cd /home/cody/projects/printforge
# Dep lists agree across run.sh and Dockerfile (expect empty output):
diff <(grep -oE -- '--with [a-zA-Z0-9_-]+' run.sh | awk '{print $2}' | sort) \
     <(grep -oE 'pip install --no-cache-dir .*' Dockerfile | sed 's/pip install --no-cache-dir //' | tr ' ' '\n' | sort)
# z-scale ban + emboss recipe still in the prompt contract:
grep -n 'scale(\[1,1,big\]\|linear_extrude(h) projection\|do not derive your own rotations' prompts.py
# In-place codex edit path (no full-file rewrite for refines):
grep -n 'workspace-write\|def call_codex_edit' app.py
# OpenSCAD feature flags default + textmetrics usage:
grep -n 'OPENSCAD_ARGS = os.environ' app.py; grep -n 'textmetrics' prompts.py
# Live config endpoint + which backend answered:
grep -n '@app.get("/config")\|LAST_BACKEND =' app.py
curl -s http://localhost:8093/config
# systemd unit is the deploy path:
systemctl --user cat printforge | head -20
# README docs-of-record commits + the "Later" ledger:
git show 266af68 --stat | head; git show 8827529 --stat | head
grep -n 'Later (deliberately not built' README.md
# .env is gitignored; user-data dirs are gitignored:
grep -n '.env\|library/\|uploads/' .gitignore
```
