---
name: printforge-big-bug-hunting-campaign
description: >
  The decision-gated campaign for finding the HIGHEST-IMPACT security bugs in PrintForge,
  end to end, plus the research method for turning a hunch into an accepted finding. Load
  when asked to "do a full security assessment", "find the big bugs", "run a security
  campaign", "hunt for serious vulnerabilities", or when you have limited time and need to
  prioritize where impact actually lives. Numbered phases, exact commands, expected
  observations, branch conditions, and stop gates. Prioritizes SSRF (/import-url) and the
  codex sandbox because that is where real impact concentrates. Grounded in the attack-
  surface map and the per-class review skills.
---

# PrintForge big-bug hunting campaign

Read `printforge-security-scope-and-rules` first, then `printforge-attack-surface-map`.
This is the **prioritized, gated route** to the bugs that matter, plus the method to make
a finding stick. Follow the phases in order; each has a stop gate.

Working directory: `~/projects/printforge`.

## Where impact actually lives (spend time here)

On a no-auth LAN tool, the "big bugs" are the ones that break *even without* an auth
boundary: reaching **other machines** (SSRF), running **host code** (sandbox escape),
reading **real secrets**, or **destroying data** remotely. Rank:

1. **SSRF via `/import-url`** — reaches the internal network. Highest confirmed lead.
2. **codex sandbox integrity** — if the `-s` flag ever loosens, prompt→host code.
3. **Parser RCE via upload** — version-matched CVE in trimesh/cascadio/imagemagick.
4. **Unauthenticated destructive CSRF** — remote deletion of the collaborator's models.
5. **Secret exposure** — git-tracked `.env`, endpoint/log leak.

Everything else (headers, deps hygiene, OpenSCAD `-D`) is hardening — do it after 1–5.

## The research method (apply to every lead)

Turn a hunch into an accepted finding with this loop (owned here so campaigns are self-
contained):
1. **Hypothesis** — one sentence: "X input reaches Y sink with no Z check → impact I."
2. **Prediction** — "if true, command C produces observation O."
3. **Controlled test** — run C safely (playbook: loopback/throwaway/canary). One variable.
4. **Evidence** — capture C's output; compare to O.
5. **Adversarial refutation** — actively try to disprove: is there a guard you missed
   upstream? a regex? a sandbox? Would it fail against a real target for a benign reason?
6. **Verdict + severity** — run `printforge-security-validation-and-triage`, then write it
   with `printforge-finding-report-template`.
Skipping step 5 is how false Criticals get shipped. Do not skip it.

## Phase 0 — orient (5 min, read-only)

```sh
grep -nE '@app\.(get|post|put|patch|delete)' app.py        # routes
grep -nE 'subprocess|Popen' app.py                          # sinks (expect argv, no shell=True)
grep -n 'follow_redirects' app.py                           # SSRF fetch
grep -nE 'Depends|HTTPBearer|login|session' app.py          # confirm no-auth model
git ls-files | grep -iE 'env|secret|key|token' || echo clean # tracked secrets?
```
**Gate:** if the route/sink shape differs from `printforge-attack-surface-map`, update
your mental model before proceeding (the app changed).

## Phase 1 — SSRF (`/import-url`) [highest priority]

Hypothesis: `/import-url` fetches attacker-chosen URLs into the LAN with redirects and no
host allowlist. Read `printforge-ssrf-and-fetch-review`.
```sh
sed -n '739,766p' app.py
grep -nE 'is_private|ipaddress|127\.0\.0\.1|169\.254|allowlist' app.py   # expect: no guard
```
**Branch:**
- No guard present → **prove it locally** (loopback canary, playbook Phase "canary"). Do
  NOT hit the Bambuddy VM or metadata. Loopback reach = **Confirmed High**.
- A guard is present → re-read it for bypasses (redirect-based, DNS-rebind); if solid →
  downgrade/close.
**Stop gate:** the moment a PoC would touch a real internal service, STOP and report
reachability only (scope stop condition).

## Phase 2 — codex sandbox integrity

Hypothesis: LLM-authored OpenSCAD can't get host code because codex runs sandboxed.
```sh
grep -nE 'codex.*-s (read-only|workspace-write|danger)' app.py   # expect read-only / workspace-write
sed -n '102,145p' app.py
```
**Branch:** flag still `read-only`/`workspace-write` with `-C <job under WORK_DIR>` →
sandbox intact, **not** a big bug (note as reviewed). Flag loosened to `danger-full-access`
or sandbox removed → **Confirmed High/Critical**; write it immediately.

## Phase 3 — parser RCE via upload

Read `printforge-file-upload-download-review` + `printforge-dependency-and-supply-chain-
review`.
```sh
uv pip list 2>/dev/null | grep -iE 'trimesh|cascadio|lxml|manifold3d|shapely'   # live versions
pip-audit 2>/dev/null || echo "resolve versions vs OSV manually"
```
**Branch:** a live version matches a known parser-RCE advisory reachable via `/upload-mesh`
→ **Confirmed High** (prove locally with a resource/version demonstration, no destructive
payload). No version match → **Candidate/close**.

## Phase 4 — unauthenticated destructive CSRF

Read `printforge-authn-authz-review`. Determine which state-changing routes accept a
**simple** cross-origin request (no preflight). Test with a local HTML page served from a
*different* origin against a **local throwaway** instance (playbook), never live.
**Branch:** a destructive route (`DELETE /models/{id}`) fires cross-origin without
preflight → **Confirmed Medium/High CSRF**. Only preflighted JSON → **downgrade** (browser
blocks it), note it.

## Phase 5 — secret exposure

Read `printforge-secrets-and-config-review`.
```sh
git ls-files | grep -iE 'env|secret|key|token' || echo clean
sed -n '1312,1314p' app.py                                  # /config booleans
grep -nE 'API_KEY|TOKEN|BAMBUDDY' app.py
```
**Branch:** a real secret is tracked in git or returned by an endpoint → **Confirmed
High/Critical**. Otherwise → close (env/.env/git-ignored is the expected clean state).

## Phase 6 — hardening sweep (only after 1–5)

CSP/headers (frontend skill), unpinned deps/lockfile (dependency skill), Docker-as-root,
OpenSCAD `-D` value escaping (input skill). All **Low/Medium hardening**; batch them.

## Final gate — assemble the report

For every surviving lead: run the research method's refutation step, triage
(`printforge-security-validation-and-triage`), and write with `printforge-finding-report-
template`. Separate Confirmed vulns / Candidates / Hardening / Accepted-risk. Rank by real
impact, not category.

## When NOT to use this skill

- You want to review one specific surface → go straight to that per-class skill.
- You just need the report format → `printforge-finding-report-template`.
- You need to know if a host is in scope → `printforge-security-scope-and-rules`.

## False-positive traps

- Skipping the adversarial-refutation step → false Criticals. Don't.
- Treating Phase 0's "no auth" observation as the campaign's headline finding — it's the
  accepted model; the headline is SSRF/sandbox/RCE.
- Chasing hardening (Phase 6) before impact (Phases 1–5) when time is limited.

## Evidence checklist

- [ ] Each phase's gate evaluated with the exact command output.
- [ ] Each surviving lead ran the 6-step research loop incl. refutation.
- [ ] Report separates Confirmed / Candidate / Hardening / Accepted-risk.
- [ ] Every Confirmed has a PoC + a regression test.

## Severity guidance

Per `printforge-finding-report-template`. Phase order already reflects expected severity.

## Reporting template

`printforge-finding-report-template`.

## Provenance and maintenance

Supported by `printforge-attack-surface-map` (routes/sinks/fetches) and the per-class
review skills, all grounded in `app.py`.

Re-verify (read-only, from `~/projects/printforge`):
```sh
sed -n '739,766p' app.py                                    # SSRF branch
grep -nE 'codex.*-s ' app.py                                # sandbox flags
grep -n 'follow_redirects' app.py
```

Remaining uncertainty:
- Priorities assume the no-auth LAN model; if PrintForge moves behind Authelia, re-rank
  (authz bugs become primary again). Confirm with Cody per the triage open question.
