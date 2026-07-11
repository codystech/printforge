---
name: printforge-security-validation-and-triage
description: >
  Decide what a suspected PrintForge security issue actually IS: confirmed vulnerability,
  hardening gap, theoretical risk, false positive, duplicate, or accepted risk (the
  documented no-auth model). Load BEFORE writing up a finding, when asked "is this a real
  bug", "confirmed or not", "is this exploitable", "should I report this", "false
  positive", or "is this just the design". This is the gate that stops the biggest waste
  in PrintForge security work: reporting the intentional single-trust-zone design as a
  stream of Criticals. Grounded in the app's documented no-auth model and the per-class
  false-positive traps.
---

# PrintForge security validation & triage

Read `printforge-security-scope-and-rules` first. This skill turns a *hunch or a raw
scanner hit* into one of six verdicts, so only real findings reach the report. It is the
counterweight to over-reporting.

Working directory: `~/projects/printforge`.

## The six verdicts

| Verdict | Definition | What to do |
|---|---|---|
| **Confirmed vulnerability** | Reproduced (PoC) or unambiguous code + missing check; breaks even within the documented trust model. | Report via `printforge-finding-report-template`, `Confirmed`. |
| **Hardening gap** | Real weakness, not currently exploitable for impact, reduces defense-in-depth. | Report as Low/Medium hardening; label as such. |
| **Theoretical / Candidate** | Plausible, not proven; impact or reachability unconfirmed. | Either prove it (playbook) or report `Candidate` with the exact missing proof. |
| **False positive** | Matches a known trap; not actually a bug. | Do NOT report. Record why in your notes so it isn't re-raised. |
| **Duplicate** | Same root cause as an existing finding. | Merge into the original; don't inflate the count. |
| **Accepted risk** | The documented design (no auth, LAN, `0.0.0.0`, filesystem DB). | Record ONCE in an accepted-risk list; never as a numbered vuln. |

## The decision procedure (run in order)

1. **Is it the documented trust model?** No-auth access, IDOR-by-id, any-LAN-user-can-
   call-any-route, binds `0.0.0.0`, no per-user data → **Accepted risk**. `README.md`/
   `AGENTS.md` state PrintForge is a LAN tool shared with one collaborator. Stop; do not
   report as a vuln. (You may report a *specific* consequence like CSRF-driven data
   deletion — that breaks even within the model — but the bare "no auth" is accepted.)
2. **Does a per-class false-positive trap apply?** Check the owning skill's trap list:
   - traversal via `{id}` → blocked by regex (authn-authz / input skills) → **FP**.
   - command injection via filename/param → argv lists, no `shell=True` (input skill) → **FP**.
   - stored XSS via model name → `textContent` rendering (frontend skill) → **FP**.
   - `/config` leaks a key → returns booleans (secrets skill) → **FP**.
   - dependency "vulnerable" without a version+advisory match (dependency skill) → **FP/Candidate**.
   - SSRF on printables/thingiverse (fixed host) / `/send` (env host) → **FP** (not request-driven).
3. **Is impact reachable and demonstrated?** If not proven → **Candidate**; name the exact
   test that would confirm it (playbook). Don't upgrade a Candidate to Confirmed on vibes.
4. **Does it break something even within the accepted model?** SSRF into internal hosts,
   parser RCE, secret exfil, unauthenticated destructive CSRF, loosened codex sandbox →
   **Confirmed vulnerability** (if proven) — these harm even a single-trust-zone LAN tool.
5. **Is it just defense-in-depth?** Missing CSP/headers, unpinned deps, Docker-as-root →
   **Hardening gap** (Low/Medium), not a vuln.
6. **Seen before?** Cross-check `printforge-security-failure-archaeology` and existing
   findings → **Duplicate** if same root cause.

## Worked examples (from this repo)

- "Anyone can `DELETE /models/{id}` without auth." → The *no-auth* part is **Accepted
  risk**. But "a malicious web page can trigger that DELETE via CSRF from the
  collaborator's browser" → **Confirmed/Candidate vulnerability** (prove the simple-request
  fires). Report the CSRF framing, note the accepted-risk base.
- "`/import-url` fetches attacker URLs into the LAN." → Not the trust model (it reaches
  *other* hosts) → prove loopback reach → **Confirmed** SSRF, High.
- "Path traversal in `/stl/{id}`." → regex `[0-9a-f]{32}` guards it → **False positive**.
- "No CSP." → **Hardening gap**, Medium at most, unless paired with a real XSS sink.
- "trimesh is vulnerable." → no version+advisory match yet → **Candidate**; resolve the
  installed version first.

## When NOT to use this skill

- You've already triaged and are writing it up → `printforge-finding-report-template`.
- You're hunting for new issues → the per-class review skills.
- You need to run a PoC to move Candidate→Confirmed → `printforge-security-testing-playbook`.

## False-positive traps (about triage itself)

- **Scanner output is not a finding.** A generic tool flag is a *lead*; run this procedure
  before it becomes a finding.
- **"It's insecure by design" is a real category**, not an excuse to hide a genuine bug —
  distinguish "the model" (accepted) from "a bug the model doesn't cover" (report).
- **Don't over-correct into dismissing everything as accepted risk.** SSRF, RCE, and
  secret exfil are real regardless of the auth model.

## Evidence checklist

- [ ] Verdict assigned from the six, with the deciding step named.
- [ ] For FP: which trap + the code that makes it a non-issue.
- [ ] For Candidate: the exact proof still needed.
- [ ] For Confirmed: the PoC/code evidence.
- [ ] Duplicate/accepted-risk items pointed at their single home.

## Severity guidance

Severity is assigned only for Confirmed/Hardening/Candidate, using the rubric in
`printforge-finding-report-template`. Accepted-risk and FP get no severity.

## Reporting template

Use `printforge-finding-report-template` for anything that survives triage.

## Provenance and maintenance

Supported by the documented trust model (`README.md`, `AGENTS.md`), and the per-class
false-positive traps in the sibling review skills (which are themselves grounded in
`app.py`).

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -nE 'Depends|HTTPBearer|login|session' app.py     # confirm no-auth model still holds
ls .claude/skills | grep printforge-security            # sibling skills to route to
```

Remaining uncertainty:
- **Open question for Cody:** what severity bar counts as a "big security bug" here, and
  is the no-auth LAN model still the intended design (vs a planned Authelia gateway)? Until
  answered, treat SSRF/RCE/secret-exfil as the "big bug" bar and the no-auth base as
  accepted. Re-verify this assumption before a major report.
