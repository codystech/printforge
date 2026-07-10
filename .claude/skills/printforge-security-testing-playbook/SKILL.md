---
name: printforge-security-testing-playbook
description: >
  How to safely test PrintForge security findings: stand up a local throwaway instance,
  run non-destructive read-only probes and minimal local PoCs, and prove impact without
  harming the live service, the collaborator's data, or the owned Bambuddy VM. Load when
  you are about to RUN a test, "reproduce a finding", "write a PoC", "curl an endpoint",
  "spin up a test instance", or "prove this is exploitable". Enforces: never test against
  the live :8093 instance's state, never hit out-of-scope hosts, PoCs use canaries not
  real secrets. Grounded in run.sh, app.py port/paths, the scope skill.
---

# PrintForge security testing playbook

Read `printforge-security-scope-and-rules` FIRST — it defines what you may touch. This
skill is the *how*: safe mechanics for reproducing and proving findings.

Working directory: `~/projects/printforge`.

## Rule 0 — static first, dynamic only when needed

Most PrintForge findings are provable by **reading `app.py`** (the sink, the missing
check) — no traffic required. Only run a dynamic PoC when static reading can't settle
whether something is exploitable (e.g. does `/import-url` actually reach loopback? does a
CSRF simple-request fire?). Prefer the smallest dynamic test that resolves the question.

## Read-only probes against the LIVE instance (allowed)

GET requests that don't change state are safe against the running `:8093`:
```sh
curl -sI  http://localhost:8093/                 # response headers (CSP? framing?)
curl -s   http://localhost:8093/config           # expect booleans only
curl -s   http://localhost:8093/models | head    # list (no-auth reality, read-only)
```
Do **not** run POST/PUT/PATCH/DELETE against `:8093` — those mutate the collaborator's
real library or trigger real Bambuddy uploads.

## State-changing PoCs — use a LOCAL THROWAWAY instance

Never POST/DELETE against the live service. Stand up an isolated copy on a spare port
with a throwaway data dir, so every write lands in a sandbox you own:

```sh
# from a COPY, not the live tree — or override the data dirs and port
cd ~/projects/printforge
mkdir -p /tmp/pf-sec-test/{library,uploads}
# run on a spare port with the local (no-secret) qwen backend, NOT codex, NOT real .env
LLM_BACKEND=http LLM_BASE_URL=http://127.0.0.1:4000/v1 LLM_MODEL=claude-brain-coder \
  BAMBUDDY_API_KEY= QA_CHECK=0 \
  uv run --with fastapi --with uvicorn --with httpx --with trimesh --with numpy \
         --with scipy --with python-multipart --with networkx --with lxml \
         --with shapely --with rtree --with manifold3d --with cascadio \
  uvicorn app:app --host 127.0.0.1 --port 8099
```
Notes:
- `--host 127.0.0.1` (loopback only, not `0.0.0.0`) — your test instance must not be
  LAN-reachable.
- `BAMBUDDY_API_KEY=` empty → `/send` returns 400 instead of touching the real VM.
- `QA_CHECK=0` and `LLM_BACKEND=http` → no codex/OpenAI spend, uses the free local brain.
- Point `LIB_DIR`/`UPLOADS_DIR` at throwaway dirs if you can (or run from a copied tree)
  so no test artifact pollutes the real `library/`.

## Canary standard for SSRF / file-read PoCs

To prove a read primitive (SSRF, path reach) **without touching real secrets**:
1. Create a file *you own* with obviously-non-secret contents:
   `echo 'CANARY-not-a-secret-'$RANDOM > /tmp/pf-canary.txt`.
2. Serve it from a loopback HTTP server you control (`python -m http.server 8055` in a
   dir containing the canary).
3. Point the PoC at your canary/loopback, not at `169.254.169.254`, not at the Bambuddy
   VM, not at any real service. Reaching the canary proves the class; stop there.

Never craft a PoC that reads `.env`, `BAMBUDDY_API_KEY`, or any real credential. "The path
reaches secrets" is the finding; dumping them is out of bounds (scope skill stop
condition).

## Non-destructive PoC standard

A PoC must be: **minimal** (proves the one claim, nothing more), **local/loopback**,
**reversible** (creates only throwaway data you then delete), and **non-exfiltrating**
(canaries, not real data). Record the exact command + observed output for the finding.

## Cleanup

```sh
pkill -f 'uvicorn app:app --host 127.0.0.1 --port 8099'   # stop your test instance
rm -rf /tmp/pf-sec-test /tmp/pf-canary.txt                # remove throwaway data
```
Confirm you left `library/`, `uploads/`, `.env`, and the live `:8093` untouched.

## When NOT to use this skill

- You only need to read code to prove the finding → skip dynamic testing entirely.
- You want to know *what* to test → `printforge-attack-surface-map` / the per-class skill.
- You want to know *if you're allowed* → `printforge-security-scope-and-rules`.
- You're deploying a fix → that's `printforge-change-control` (a different job).

## False-positive traps (testing artifacts)

- A 404/400 from the LIVE instance for a mutating probe you *didn't* send is expected —
  don't infer a control from a request you never made.
- Local-instance behavior can differ from live (backend = qwen not codex; empty Bambuddy
  key). Note the difference; don't claim live impact you only saw on the test box without
  reasoning about the delta.
- `curl` following redirects (`-L`) can itself hit out-of-scope hosts — omit `-L` unless
  you control the redirect target.

## Evidence checklist

- [ ] Exact command(s) run + captured output.
- [ ] Which instance (live-read-only vs local-throwaway) and why that was safe.
- [ ] Confirmation no live/real data was modified and no out-of-scope host was contacted.
- [ ] Cleanup done.

## Severity guidance

This skill sets no severity — it produces the *evidence*. Severity is assigned in
`printforge-security-validation-and-triage` with `printforge-finding-report-template`.

## Reporting template

Use `printforge-finding-report-template`; paste the PoC command + output into its
"Reproduction" and "Evidence" fields.

## Provenance and maintenance

Supported by `run.sh` (the exact uv/uvicorn invocation this playbook mirrors), `app.py`
(port 8093, `BAMBUDDY_API_KEY`/`QA_CHECK`/`LLM_BACKEND` env gates), and the scope skill's
authorized-host table.

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -n 'uvicorn' run.sh                       # confirm the invocation still matches
grep -nE 'QA_CHECK|LLM_BACKEND|BAMBUDDY_API_KEY' app.py   # env gates the playbook relies on
ss -ltnp 2>/dev/null | grep 8093 || echo "live instance not currently up"
```

Remaining uncertainty:
- Whether `LIB_DIR`/`UPLOADS_DIR` can be overridden by env in the current code — if not,
  run the test instance from a **copied tree** so writes can't reach the real `library/`.
  Verify: `grep -nE 'LIB_DIR *=|UPLOADS_DIR *=' app.py` (they are `Path(__file__).parent/...`,
  so copy the tree rather than relying on env override).
