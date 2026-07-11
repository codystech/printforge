---
name: printforge-finding-report-template
description: >
  The canonical severity rubric and finding report format for PrintForge security work.
  Load whenever you are about to WRITE UP a security finding, "report a vulnerability",
  "assign severity", "how do I document this bug", or "what evidence is required". This
  skill owns the ONE report template and the ONE severity rubric — every other
  printforge-security-* skill points here instead of redefining them. A finding is not
  done until it fills every required field and states its confirmed/candidate status.
---

# PrintForge finding report template

Read `printforge-security-scope-and-rules` first. This skill is the **single home** for
how a PrintForge security finding is written and scored. Other skills give surface-
specific severity hints; the rubric and template here are authoritative.

## Severity rubric (calibrated to PrintForge's reality)

PrintForge is a **personal/LAN, no-auth, single-trust-zone** tool with a second real
user and a filesystem database. Calibrate impact to *that*, not to a public SaaS:

| Severity | Bar for PrintForge |
|---|---|
| **Critical** | Host RCE reachable from an HTTP request; or full read of real secrets/credentials that unlock other systems (e.g. the Bambuddy API) from a single request; or destruction of all library data by an unauthenticated remote request with no interaction. |
| **High** | SSRF that reads internal services; loosened codex sandbox → attacker-chosen host code; version-matched parser RCE via upload; real stored XSS driving every endpoint; a real secret committed to git or returned by an endpoint. |
| **Medium** | CSRF that destroys/overwrites the collaborator's saved models (simple cross-origin request proven); parser DoS reachable remotely; missing CSP with a plausible XSS pairing; version-matched parser DoS CVE. |
| **Low** | OpenSCAD `-D` expression injection (no host exec); prompts/paths logged; missing security headers alone; unpinned deps / no lockfile; Docker-as-root. |
| **Informational / Accepted risk** | The documented no-auth LAN model; ID guessability; binds `0.0.0.0` as designed; non-sensitive localStorage. Record once; do not re-litigate as a vuln. |

Two dimensions to state explicitly, because they move severity:
- **Confidence**: `Confirmed` (PoC or unambiguous code), `Candidate` (strong hypothesis,
  not yet proven), `Unverified` (needs a check you haven't run). Never label a hypothesis
  Confirmed.
- **Trust-model framing**: is this a *vulnerability within* the documented model, or the
  *documented model itself*? The latter is accepted-risk, not a numbered vuln. See
  `printforge-security-validation-and-triage`.

## The report template (fill every field)

```
### [SEVERITY] [Confirmed|Candidate|Unverified] — <one-line title with route + app.py:line>

**Class:** <SSRF | authz/CSRF | injection | file-upload | secrets/config | dependency | frontend/XSS>
**Affected code:** <file:line(s)>, function/route name
**Trust-model framing:** <vulnerability within the model | the documented model itself (accepted risk)>

**Summary:** <2-3 sentences: what the bug is and why it matters here.>

**Impact:** <Concrete, PrintForge-specific. Who/what is harmed and how. Tie to the rubric
row. If impact depends on the no-auth model, say so.>

**Reproduction (safe, local):**
<Exact commands from printforge-security-testing-playbook. Loopback/throwaway only.
Canaries, not real secrets. State which instance (live-read-only vs local-throwaway).>

**Evidence:**
<Captured output / code excerpt proving the claim. For Candidate/Unverified, state exactly
what remains to prove and the command that would prove it.>

**Root cause:** <The missing check / unsafe sink, in one sentence.>

**Suggested fix:** <Minimal change. Note if the "fix" is a design decision for Cody
(e.g. add auth) rather than an obvious patch.>

**Suggested regression test:** <One runnable check that fails if the bug returns — e.g. a
test asserting /import-url rejects RFC1918 hosts; a test asserting /config exposes no key
value. Match the repo's style: app.py has no test suite except parts.py __main__, so
propose the smallest self-contained check.>

**False-positive check:** <Which trap from the class skill you ruled out, and how.>
```

## Rules for a valid finding

1. **No Confirmed without evidence.** Code-only findings need the exact sink + the
   absent check quoted. Behavioral findings need the PoC command + output.
2. **One home per fact.** Cite `app.py:line`; don't paraphrase code you could quote.
3. **Rank by real impact, not by category prestige.** A Low that's real beats a "Critical"
   that's the documented model.
4. **Every finding names its regression test.** A fix without a test to hold it is
   incomplete (matches how the rest of this repo's skills gate changes).
5. **Accepted-risk items go in a separate list**, not the vuln list, and say "documented
   trust model."

## When NOT to use this skill

- You're still hunting, not writing up → the per-class review skill.
- You're deciding *whether* something is a real finding vs FP/accepted →
  `printforge-security-validation-and-triage` (do that first, then write it up here).
- You're running the PoC → `printforge-security-testing-playbook`.

## False-positive traps

- Writing a finding before triage → you may be documenting the accepted no-auth model as
  a Critical. Triage first.
- Copy-pasting a generic OWASP severity → recalibrate to the rubric above (personal LAN
  tool, not public SaaS).

## Evidence checklist

- [ ] Every template field filled (no `TODO`/blank).
- [ ] Severity + Confidence + trust-model framing all stated.
- [ ] Repro is safe/local and matches the playbook.
- [ ] A concrete regression test is proposed.

## Severity guidance

This skill *is* the severity guidance. Defer to the rubric table above.

## Reporting template

(Above — this skill owns it.)

## Provenance and maintenance

Supported by the trust model in `printforge-security-scope-and-rules` (README/AGENTS.md),
the repo's existing gate-and-test doctrine (`printforge-change-control`,
`printforge-validation-and-qa`), and the observation that `app.py` has no test suite
except `parts.py`'s `__main__` self-check.

Re-verify (read-only, from `~/projects/printforge`):
```sh
ls .claude/skills | grep printforge-security   # sibling skills exist to route to
grep -rn '__main__' parts.py                    # confirm the repo's minimal-test convention
```

Remaining uncertainty:
- The severity bar should be confirmed with Cody for borderline cases (see the open
  question in `printforge-security-validation-and-triage`).
