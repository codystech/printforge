---
name: printforge-security-failure-archaeology
description: >
  The ledger of PrintForge things that LOOK like security bugs but aren't (already-
  mitigated, by-design, or dead ends), plus the one real security-adjacent incident on
  record. Load BEFORE reporting a finding or re-investigating a suspected bug, when asked
  "has this been looked at", "is this already handled", "known false positive", "why
  isn't X a bug", or "past security incident". Saves you from re-reporting the intentional
  no-auth model, the regex-guarded ids, the argv-no-shell sinks, and the textContent-safe
  UI. Grounded in app.py, the existing printforge-failure-archaeology and change-control
  skills.
---

# PrintForge security failure archaeology

Read `printforge-security-scope-and-rules` first. This is the **memory of what's already
settled** so you don't burn time re-deriving it or file a finding that's a known non-
issue. Two parts: the real incident on record, and the "looks-like-a-bug, isn't" ledger.

Working directory: `~/projects/printforge`. Anchors drift — re-verify with the commands
in each row / Provenance.

## Real security-adjacent incident on record

| Incident | Status | What happened | Lesson (now a rule) | Anchor |
|---|---|---|---|---|
| **Inline secret → classifier-block** | settled | A secret was pasted inline in code/chat once and got **classifier-blocked**, jamming work. | Secrets live ONLY in the git-ignored `.env`; `run.sh` sources it. Never inline, never in chat. | `printforge-change-control` rule **N3** (`:110-113`); `.env` sourcing `run.sh:7`; operator-reported, partially unverified in repo. |

That is the *only* security-shaped incident captured in the existing archaeology. There is
**no recorded history** of an exploited SSRF, injection, auth bypass, or data-leak in
PrintForge — treat that as "not yet investigated", not "proven safe."

## The "looks like a bug, isn't" ledger (known false positives)

Do **not** report these without first showing the stated mitigation is broken/bypassable:

| Suspected bug | Why it's NOT a finding | Re-verify |
|---|---|---|
| **Path traversal via `/models/{id}`, `/stl/{id}`, `/uploads/{id}`** | ids are regex-validated: `[0-9a-f]{12}` (`_model_dir` `app.py:1317`, mesh `:793/:1200`), `[0-9a-f]{32}` (`_stl_path` `:1155`) before any path join. | `grep -nE 'fullmatch\(r"\[0-9a-f\]' app.py` |
| **Command injection via filename/prompt/param** | every subprocess is an **argv list, no `shell=True`**; metacharacters are inert. Uploaded filename only contributes its suffix to a uuid path. | `grep -n 'shell=True\|os\.system' app.py` (expect none) |
| **Stored XSS via model name / prompt** | UI renders via `document.createElement` + **`textContent`** (`static/index.html:289,:705`); the 4 `innerHTML` uses are `= ''` clears only. | `grep -nE 'innerHTML.*(\$\{|`)' static/index.html` (expect none) |
| **`/config` leaks the Bambuddy/LLM key** | returns `bool(BAMBUDDY_API_KEY)`, booleans only, never the value (`app.py:1312-1313`). | `sed -n '1312,1314p' app.py` |
| **Hardcoded API key `dummy`** | `LLM_API_KEY` default `"dummy"` is a placeholder for the local no-auth brain, not a real secret. | `grep -n 'LLM_API_KEY' app.py` |
| **SSRF via printables/thingiverse/`/send`** | printables/thingiverse hit **fixed hosts** (only a numeric id varies); `/send` targets the **env** `BAMBUDDY_URL`, not a request URL — none is request-driven SSRF. (The real SSRF is the `/import-url` **else-branch**.) | `sed -n '739,766p' app.py` |
| **`.env` committed to git** | git-ignored; verify tracked files. | `git ls-files \| grep -iE 'env\|secret\|key\|token'` (expect empty) |
| **IDOR: reading another user's model by id** | no users exist; single-trust-zone LAN model is documented (README/AGENTS.md). Report the *systemic* CSRF/destructive framing, not per-object IDOR. | `README.md`, `AGENTS.md` |
| **Zip-slip** | PrintForge **creates** zips from its own dirs; it does not **extract** attacker archives (`/zip` `app.py:1211`). | `grep -nE 'extractall\|ZipFile' app.py` |

## Deliberately-deferred (by design, not a bug)

- **No auth / binds `0.0.0.0`** — README "Later" lists "Deploy to a lab CT behind
  NPM/Authelia once it proves useful" as **deliberately not built yet**. Accepted risk
  today; do not report the base model as a vuln (see `printforge-security-validation-and-
  triage`). It becomes reportable only if the deploy assumption changes.
- **STEP originals persisted in `uploads/`** — kept on purpose ("true CAD source worth
  preserving", `app.py:646-648`); git-ignored. Note for data-exposure, not a vuln alone.

## What IS still open (not yet disproven — worth hunting)

- **`/import-url` else-branch SSRF** (`app.py:763`) — the one lead the ledger does NOT
  clear. Owned by `printforge-ssrf-and-fetch-review`.
- **codex sandbox flag drift** — only safe while `-s read-only`/`workspace-write`; watch it.
- **Parser CVEs** in trimesh/cascadio/imagemagick at the installed version — unresolved
  until you check live versions (`printforge-dependency-and-supply-chain-review`).

## When NOT to use this skill

- You're triaging a *new* lead not in the ledger → `printforge-security-validation-and-
  triage`.
- You want the surface map → `printforge-attack-surface-map`.
- You want non-security incident history (geometry, prompts, ops) → the existing
  `printforge-failure-archaeology` skill (this one is security-only).

## False-positive traps

- **Trusting this ledger without re-verifying.** Every mitigation here is a code fact that
  can regress. Run the re-verify command before relying on a "not a finding" row.
- **Assuming "no recorded incident" = "secure."** It means un-investigated. Hunt anyway.

## Evidence checklist

- [ ] Before filing a finding, checked it isn't a ledger row.
- [ ] If it is, re-ran the row's re-verify command and confirmed the mitigation still holds.
- [ ] If the mitigation is broken/bypassable, THEN it's a real finding — report with proof.

## Severity guidance

Ledger rows carry no severity (they're non-findings). A *regression* of a mitigation is
scored fresh per `printforge-finding-report-template`.

## Reporting template

Only for regressions — `printforge-finding-report-template`.

## Provenance and maintenance

Supported by the existing `printforge-failure-archaeology` (incident row, `:99`,`:154`) and
`printforge-change-control` (secret rule N3, `:110-113`), plus `app.py` id/argv/config
facts and `static/index.html` textContent rendering.

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -nE 'fullmatch\(r"\[0-9a-f\]' app.py            # id guards still present
grep -n 'shell=True\|os\.system' app.py              # expect none
grep -nE 'innerHTML.*(\$\{|`)' static/index.html || echo "no data-bearing innerHTML"
sed -n '1312,1314p' app.py                           # /config booleans
git ls-files | grep -iE 'env|secret|key|token' || echo clean
```

Remaining uncertainty:
- The inline-secret classifier-block incident is operator-reported and only partially
  verified in-repo — treat the *rule* (N3) as authoritative, the *narrative* as testimony.
- No security assessment has been formally completed before this library existed, so the
  "open" list is a starting point, not an exhaustive prior-art record.
