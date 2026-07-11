---
name: printforge-authn-authz-review
description: >
  Review PrintForge's authentication, authorization, access control, IDOR, and CSRF
  posture. Load when asked "can one user reach another's data", "is there auth", "IDOR",
  "broken access control", "who can delete/rename models", "session/token handling",
  "CSRF", or "multi-tenant boundary". CRITICAL CONTEXT: PrintForge has NO authentication
  by design (single LAN trust zone, one collaborator) — so most "IDOR" here is the
  documented model, not a bug. This skill teaches you to tell a real access-control bug
  from the accepted no-auth design, and where CSRF/destructive-no-auth actually bites.
  Grounded in app.py (no Depends/session), _model_dir/_stl_path id validation.
---

# PrintForge authn / authz review

Read `printforge-security-scope-and-rules` first — it establishes the trust model this
skill depends on. Jargon: **IDOR** (Insecure Direct Object Reference) = you access an
object by guessing/changing its id because the server doesn't check you own it. **CSRF**
(Cross-Site Request Forgery) = a malicious web page makes the victim's browser send a
state-changing request to PrintForge using the victim's network position.

Working directory: `~/projects/printforge`.

## The load-bearing fact: there is no auth, and that is the documented design

Confirmed: `grep -nE 'Depends|HTTPBearer|login|session|cookie|@requires' app.py` returns
**only outbound `Authorization` headers** to codex/Bambuddy/Thingiverse — **no inbound
auth check exists.** Every route is reachable by anyone who can reach port 8093. The app
binds `0.0.0.0`. `README.md` + `AGENTS.md` state this is intentional: a LAN service
shared with "a second real user (the collaborator)", filesystem-as-database, single
trust zone.

**Therefore:** "user A can read/delete user B's model via its id" is **NOT a reportable
IDOR** — there are no users, no per-object ownership, and that is by design. Reporting it
as a Critical wastes Cody's time. See `printforge-security-validation-and-triage`:
this is an **accepted risk / hardening gap**, framed correctly, not a vuln.

What you CAN legitimately report in this area:

### 1. Destructive actions with no auth AND no confirmation (hardening / risk)
`DELETE /models/{id}` (`:1484`) permanently removes a model; `PATCH /models/{id}`
(`:1392`) renames; `PUT /models/{id}/parts` (`:1418`), `/rules` (`:1405`) mutate. On a
no-auth `0.0.0.0` service, *anything on the LAN* — including a malicious web page via
CSRF (below) — can destroy the collaborator's saved work. Frame as: **CSRF-driven data
destruction on an unauthenticated LAN service.** That is a real, defensible finding even
though "no auth" itself is accepted.

### 2. CSRF (real, because no auth + no CSRF token + state-changing GET-adjacent POSTs)
There is no CSRF token, no `SameSite` cookie (no cookies at all), and no `Origin`/
`Referer` check. State-changing endpoints are plain JSON/form POSTs. A web page the
collaborator visits could `fetch('http://<printforge-ip>:8093/models/<id>', {method:
'DELETE', ...})` or drive `/import-url` / `/send`. Note the mitigating nuance: with **no
CORS middleware** (`grep -n CORS app.py` → none), the browser blocks the attacker from
*reading* the response cross-origin, and JSON-body requests trigger CORS preflight that
PrintForge won't answer — but **simple requests** (form-encoded POST, or DELETE that the
app accepts without a custom content-type) may still *execute* the side effect. Test
which state-changing routes accept a simple/no-preflight request; those are the CSRF-
exploitable ones. This is a **Candidate** until you show a specific route fires from a
cross-origin simple request.

### 3. ID guessability (informational)
Model ids are `uuid4().hex[:12]` (`:1223`, `:1317`), STL ids `[0-9a-f]{32}`. 48 bits for
models — not enumerable by brute force in practice. Do **not** report "IDs are
guessable"; they aren't meaningfully. Mentioned only so you don't chase it.

## What is genuinely well-handled (don't re-report)

- **Path traversal via ids is blocked**: `_model_dir` requires `[0-9a-f]{12}` (`:1317`),
  `_stl_path` requires `[0-9a-f]{32}` (`:1155`), mesh handlers require `[0-9a-f]{12}`
  (`:793,:1200`), openscad param keys `\w+` (`:181`), part names `\w+` (`:1424`). These
  are consistent and correct. See the false-positive traps below.

## When NOT to use this skill

- The bug is the *server fetching a URL* → `printforge-ssrf-and-fetch-review`.
- The bug is *content* of user input reaching a sink (injection) →
  `printforge-input-and-injection-review`.
- The bug is a secret in config/logs → `printforge-secrets-and-config-review`.
- You want to argue whether "no auth" is acceptable → that's a triage/accepted-risk call
  in `printforge-security-validation-and-triage`, and ultimately Cody's decision.

## False-positive traps

- **"IDOR: I can read another model by id."** By design — no users exist. Not a vuln.
  Report the *systemic* framing (unauthenticated destructive actions + CSRF), not a
  per-object IDOR.
- **"Path traversal via `/models/{id}`."** Blocked by `[0-9a-f]{12}` regex. Only real if
  you demonstrate a bypass of the regex.
- **"No CORS = vulnerability."** Missing CORS here mostly *protects* against cross-origin
  reads. The CSRF risk is about state-changing *simple* requests, not CORS reads. Be
  precise.
- **"Session fixation / JWT bug."** There are no sessions or JWTs. N/A.

## Evidence checklist

- [ ] Confirmed no inbound auth (`grep` above) — cite it.
- [ ] For a CSRF finding: the exact route, its method, and proof it executes from a
      cross-origin **simple** request (no preflight) — a minimal local HTML PoC page
      served from a different origin on your own box.
- [ ] For destructive-no-auth: the route + that it mutates/deletes real `library/` data.
- [ ] Suggested regression test (e.g. an `Origin`-header check test, or a require-token
      test) — note that the *fix* is a design decision for Cody, not an obvious patch.

## Severity guidance

- CSRF that deletes/overwrites the collaborator's saved models, proven with a simple
  cross-origin request: **Medium→High** (real data loss, unauthenticated, browser-
  driven). High only if you show it fires cross-origin without preflight.
- "No authentication" as a standalone observation: **not a numbered finding** — it is the
  documented trust model. Record it once as an accepted-risk note in triage.
- ID guessability: **Informational**.

## Reporting template

Use `printforge-finding-report-template`. For anything in the "accepted design" bucket,
report it in the *accepted-risk / hardening* section, not as a vulnerability, and say
explicitly "this is the documented single-trust-zone model."

## Provenance and maintenance

Supported by: absence of inbound auth (grep), `app.py:1155/:1317` id validation,
`app.py:1392/:1418/:1484` state-changing routes, `README.md`+`AGENTS.md` trust model, and
absence of CORS middleware.

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -nE 'Depends|HTTPBearer|login|session|cookie|@requires' app.py  # expect: no inbound auth
grep -niE 'CORS|SameSite|Origin|Referer' app.py                      # expect: none → confirm CSRF nuance
grep -nE 'fullmatch\(r"\[0-9a-f\]' app.py                            # id validation present
```

Remaining uncertainty:
- Whether specific state-changing routes accept a no-preflight simple request must be
  tested empirically (locally) before calling CSRF confirmed.
- If PrintForge is later put behind Authelia/NPM (README "Later"), re-do this entire
  review — the trust model changes.
