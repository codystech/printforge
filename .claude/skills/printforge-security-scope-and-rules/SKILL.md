---
name: printforge-security-scope-and-rules
description: >
  READ THIS FIRST before any PrintForge security testing. Defines what is authorized,
  what is off-limits, the hard safety rules, and the stop conditions for an
  authorized-only security assessment of PrintForge. Load when you are asked to
  "find security bugs", "do a security review", "pentest PrintForge", "check for
  vulnerabilities", "is this exploitable", or when any other printforge-security-*
  skill sends you here for scope. Covers: the single trust zone (LAN, no auth by
  design), the exact hosts you may touch (localhost:8093, the owned Bambuddy staging
  VM, local LiteLLM/ollama), what you must NEVER do (scan public hosts, exfiltrate
  real secrets, run destructive payloads, attack Printables/Thingiverse/Bambu cloud),
  and when to STOP and ask Cody. This is the doctrine skill; every finding must comply
  with it.
---

# PrintForge security scope and rules

This is the **authorization and safety gate** for security work on PrintForge. It does
not teach you how to find bugs — the sibling `printforge-attack-surface-map` and the
per-class review skills do that. This skill tells you **what you are allowed to touch,
what you must never do, and when to stop.** If any other security skill's command
conflicts with a rule here, **this skill wins.**

Jargon, defined once:
- **In scope / owned** — a host Cody owns and has authorized for testing.
- **Read-only** — a command that does not change state, upload, delete, or send data
  off the box (`curl` a GET, `grep`, `git log`). POSTs that create/modify/delete are
  NOT read-only.
- **PoC (proof of concept)** — the *minimum* non-destructive demonstration that a bug
  is real. Never a working exploit tuned for abuse.
- **Candidate / Unverified** — a suspected bug not yet proven. Say so; never call it
  confirmed.

## What PrintForge is (the trust model you are testing against)

PrintForge is a **single-file FastAPI app** (`app.py`) on **port 8093, bound
`0.0.0.0`** (`compose.yaml` `network_mode: host`; `run.sh` `--host 0.0.0.0`), run as a
systemd **user** service on Cody's workstation. It has **no authentication, no login,
no sessions, no CSRF tokens, no per-user data** — verified: `grep -nE
'Depends|HTTPBearer|login|session|cookie' app.py` returns only outbound `Authorization`
headers, never an inbound auth check. The README and `AGENTS.md` state it is a **LIVE
service with a second real user ("the collaborator")** on the LAN. The filesystem is the
database (`library/`, `uploads/`).

**Consequence for testing:** "any LAN user can call any endpoint" is the *documented,
accepted design*, not a bug to re-report. Your job is to find bugs that break even
within that model — SSRF into internal hosts, injection, secret leakage, data
destruction, RCE — and to flag hardening gaps as hardening gaps, not as criticals.
See `printforge-security-validation-and-triage` for that distinction.

## Authorized scope (Cody confirmed: local + owned staging VMs)

| Target | Address | May I test it? | How |
|---|---|---|---|
| PrintForge itself | `http://localhost:8093` / `127.0.0.1:8093` | YES | static review + read-only GETs; state-changing POSTs only against a **local throwaway instance** (see playbook) |
| Bambuddy staging | `http://192.168.1.50:8000` (VM104, `BAMBUDDY_URL` default, `app.py:30`) | YES, read-only only | it is Cody-owned staging; confirm impact of `/send`, do NOT flood or corrupt its archive |
| Local LiteLLM brain | `127.0.0.1:4000` | YES, read-only | liveness only; it is Cody's local model gateway |
| Local ollama | `127.0.0.1:11434` | YES, read-only | `/api/ps` only; `_free_gpu` already talks to it |
| Source tree | `~/projects/printforge` | YES | static analysis is the primary method |

**Everything else is OUT OF SCOPE. Do not touch:**
- Printables (`api.printables.com`), Thingiverse (`api.thingiverse.com`), MakerWorld,
  Cults3D, MyMiniFactory, Bambu cloud — these are **third-party production services**.
  PrintForge calls them; you may READ the code paths but you may NOT send crafted
  requests, fuzz, or scan them.
- Any public IP, any host not in the table above, anything on other VLANs unless a
  human explicitly authorizes it in-session.
- The Paperclip agent server, Hermes, the arrs VM, NetBox, the edge CT — those are
  LabOps surfaces, not PrintForge. Use the LabOps skills for those; they are NOT part
  of this assessment.

## Hard rules — never, under any circumstances

1. **No scanning, exploiting, or enumerating anything not in the authorized table.**
   No nmap/masscan/ffuf against public hosts. Ever.
2. **No destructive payloads.** No `rm`, no `DROP`, no filling the disk, no deleting
   `library/` models, no corrupting the Bambuddy archive. The database IS the
   filesystem — a destructive test destroys the collaborator's real work.
3. **No real-secret exfiltration.** If a PoC *would* read `.env`, `BAMBUDDY_API_KEY`,
   `LLM_API_KEY`, or `THINGIVERSE_TOKEN`, prove reachability with a **non-secret canary
   file you created**, or stop at "this path reaches secrets" without dumping them.
4. **No persistence, no stealth, no detection-evasion, no credential-theft tooling.**
   This is an authorized audit, not an intrusion. Findings go in a report, not a shell.
5. **No writing outside `.claude/skills/`.** Do not modify `app.py`, `prompts.py`,
   `parts.py`, `static/`, config, or the running service. You are reviewing, not
   patching, unless Cody separately asks.
6. **No restarting or editing the live service.** It has a real second user. Deploys
   are gated by `printforge-change-control` — that is a different job from finding bugs.
7. **PoCs are minimal and local.** Run state-changing PoCs against a **local throwaway
   PrintForge** on a spare port (see `printforge-security-testing-playbook`), never the
   live `:8093` instance the collaborator uses.

## Stop conditions — halt and ask Cody

Stop immediately and ask before continuing if:
- A test would touch anything not in the authorized table.
- You find a bug that reaches **live secrets or the collaborator's real data**, and
  proving it further requires reading that data. Report reachability; do not extract.
- A PoC could plausibly change state on the live instance or the Bambuddy VM.
- Authorization or ownership of a host is unclear.
- You are about to run any command whose blast radius you cannot predict.
- You discover an *actively exploited* issue or evidence of real compromise — that is
  an incident, not an audit; escalate to Cody at once.

## When NOT to use this skill

- You already know the scope and just need the map of what to test →
  `printforge-attack-surface-map`.
- You want to run a PoC safely → `printforge-security-testing-playbook`.
- You want to decide if a hit is real → `printforge-security-validation-and-triage`.
- The question is about the *homelab's* public exposure / ingress / Cloudflare / Authelia
  posture (not PrintForge's own code) → `labops-security-operations`.
- You are doing a normal (non-security) change or deploy → `printforge-change-control`.

## Related sibling skills

- `printforge-attack-surface-map` — the map of what to test (routes, sinks, files).
- `printforge-security-testing-playbook` — how to run safe local PoCs.
- `printforge-security-validation-and-triage` — confirmed vs hardening vs false positive.
- `printforge-finding-report-template` — how to write a finding.
- Per-class review skills: `printforge-authn-authz-review`,
  `printforge-input-and-injection-review`, `printforge-ssrf-and-fetch-review`,
  `printforge-file-upload-download-review`, `printforge-secrets-and-config-review`,
  `printforge-dependency-and-supply-chain-review`, `printforge-frontend-security-review`.
- `labops-security-operations` — for anything about the *homelab* posture (edge,
  CrowdSec, Authelia, exposure). PrintForge is not currently exposed publicly; if that
  changes, that skill owns the ingress question.

## Provenance and maintenance

Supported by:
- `README.md` (features, `/generate`,`/render`,`/send` behavior, "second real user").
- `AGENTS.md` (SAFETY block: "LIVE service with a second real user").
- `app.py:30-31` (`BAMBUDDY_URL` default `http://192.168.1.50:8000`, `BAMBUDDY_API_KEY`).
- `compose.yaml` (`network_mode: host`), `run.sh` (`--host 0.0.0.0`).
- Absence of auth confirmed by the grep below.

Re-verify (all read-only, run from `~/projects/printforge`):
```sh
grep -nE 'Depends|HTTPBearer|login|session|cookie|@requires' app.py   # expect: no inbound auth
grep -nE 'host 0\.0\.0\.0|network_mode' run.sh compose.yaml            # expect: binds 0.0.0.0
grep -n 'BAMBUDDY_URL' app.py                                          # expect: 192.168.1.50:8000 default
```

Remaining uncertainty:
- Whether PrintForge has since been deployed behind NPM/Authelia (README "Later"
  lists this as not-yet-done). If it has, re-scope: an auth gateway changes the trust
  model. Verify with Cody before assuming the LAN-only model still holds.
