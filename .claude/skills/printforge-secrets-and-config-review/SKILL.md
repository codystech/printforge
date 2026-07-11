---
name: printforge-secrets-and-config-review
description: >
  Review PrintForge's secrets handling, configuration, logging, and deployment defaults
  for exposure. Load when asked "are there hardcoded secrets", "is .env safe", "secret
  in logs", "does /config leak keys", "Docker/compose security", "binds 0.0.0.0",
  "insecure defaults", "security headers", "is data exposed", or "Bambuddy/LLM/
  Thingiverse token handling". KEY FACTS: secrets come from env/.env (git-ignored), the
  default LLM_API_KEY is the literal 'dummy', /config returns only booleans, and the
  service binds 0.0.0.0 with no TLS and no security headers. Grounded in app.py:27-31,
  :1312, .gitignore, compose.yaml.
---

# PrintForge secrets & config review

Read `printforge-security-scope-and-rules` first. This skill covers where secrets live,
whether they leak (logs, `/config`, backups), and the deployment defaults.

Working directory: `~/projects/printforge`.

## Secret inventory (all from env / `.env`, git-ignored)

`grep -nE 'API_KEY|TOKEN|SECRET|BAMBUDDY|_KEY' app.py`:

| Secret | Source | Default | Notes |
|---|---|---|---|
| `LLM_API_KEY` | env (`:27`) | **`"dummy"`** | fine for the local LiteLLM brain (no real auth); becomes a risk only if pointed at a paid API without overriding |
| `BAMBUDDY_URL` | env (`:30`) | `http://192.168.1.50:8000` | internal staging VM (owned) |
| `BAMBUDDY_API_KEY` | env (`:31`) | `""` (empty) | real bearer token to the Bambuddy archive; set via `.env` |
| `THINGIVERSE_TOKEN` | env (`:725`) | `""` | optional third-party token |

- **`.env` is git-ignored** (`.gitignore`: `.env`, plus `library/`, `uploads/`,
  `presets.txt`, `profiles.json`). Confirm nothing secret is tracked:
  `git ls-files | grep -E '\.env|secret|key|token'` → expect empty. (If `.env` ever
  shows in `git ls-files`, that is a **High** finding.)
- **No hardcoded real secrets in source** — the only literal is `LLM_API_KEY="dummy"`
  (a non-secret placeholder). Confirm with the grep in Provenance before claiming clean.

## Does anything leak the secrets? (the real review)

1. **`/config` (`:1312`)** returns `{"bambuddy": bool(BAMBUDDY_API_KEY), "organic": ...}`
   — **booleans only**, no key value. **Safe.** Do not report `/config` as a leak; the
   `bool(...)` is deliberate. (If a future change returns the raw key, that flips to High.)
2. **Server logs (`print`)**: `:143` prints codex-edit stderr (up to 4000 chars) to the
   server log; `:161` prints the fallback model name; `:1101` prints a QA error. codex
   runs with `-s read-only` and its stderr is unlikely to contain `BAMBUDDY_API_KEY`, but
   **user prompts and file paths do get logged**. Rate: **Low** (prompts aren't secrets;
   but note if a prompt could carry a pasted credential the user typed). No structured
   logging framework is used (`grep -n 'logging\|logger' app.py` → none) — logs are
   stdout/journald under the systemd user unit.
3. **Bambuddy egress (`/send`, `:1244`)** sends `Authorization: Bearer BAMBUDDY_API_KEY`
   to `BAMBUDDY_URL`. Since `BAMBUDDY_URL` is env-controlled (not request-controlled),
   the token only goes to the configured host — **unless** an attacker can influence the
   env, which they can't from HTTP. Not a request-driven leak. (Contrast with SSRF, which
   is request-driven — different skill.)
4. **Error responses** echo tails of `proc.stderr` to the client (`:188` render, `:144`
   codex) — check these tails can't contain secrets. OpenSCAD/codex stderr is compiler
   output, not env; **Low**, but confirm.

## Deployment defaults (config hardening)

- **Binds `0.0.0.0`** (`run.sh --host 0.0.0.0`, `compose.yaml network_mode: host`) with
  **no auth and no TLS** → the whole LAN can reach it in cleartext. This is the documented
  trust model (see scope skill). Report once as an **accepted-risk / hardening** item
  (bind `127.0.0.1` or put behind Authelia — README "Later" already plans the gateway),
  not as a fresh Critical.
- **No security headers** — `grep -niE 'X-Frame-Options|Content-Security-Policy|Strict-
  Transport|X-Content-Type' app.py` → none. Missing CSP/HSTS/etc. Relevant mainly to the
  frontend (clickjacking, XSS mitigation) → `printforge-frontend-security-review` owns
  the CSP recommendation; note here that no header middleware exists.
- **Docker**: `Dockerfile` runs uvicorn as **root** (no `USER` directive) and
  `network_mode: host` shares the host loopback. For the qwen-only container path this
  widens blast radius if the parser layer is exploited. Rate **Low/Medium** hardening
  (add a non-root `USER`, drop host networking if the LLM base URL can be reached
  otherwise). Confirm the `Dockerfile` still lacks `USER` before reporting.

## When NOT to use this skill

- The server *fetches* an attacker URL → `printforge-ssrf-and-fetch-review`.
- Client-side storage of tokens / CSP enforcement in the browser →
  `printforge-frontend-security-review`.
- A vulnerable *dependency* pulled by the Dockerfile/`run.sh` →
  `printforge-dependency-and-supply-chain-review`.
- Homelab-wide exposure / public ingress / Cloudflare → `labops-security-operations`.

## False-positive traps

- **"`/config` leaks the Bambuddy key."** It returns `bool(...)`, not the key. Read `:1313`.
- **"Hardcoded API key `dummy`."** `dummy` is a placeholder for the local no-auth brain,
  not a real secret. Not a finding.
- **"`.env` committed."** It's git-ignored; verify with `git ls-files` before claiming.
- **"Secrets in logs."** Prompts/paths are logged, not env secrets — unless you show an
  env value reaching a `print`. Be precise.
- **"Binds 0.0.0.0 = Critical."** It's the documented model; report as hardening once.

## Evidence checklist

- [ ] `git ls-files | grep -iE 'env|secret|key|token'` result (expect empty).
- [ ] The exact `print`/response line and proof (or disproof) a secret value reaches it.
- [ ] For deployment findings: quote `run.sh`/`compose.yaml`/`Dockerfile` lines.
- [ ] Suggested regression test / fix (e.g. `.gitignore` includes `.env`; a test that
      `/config` response contains no value matching the key).

## Severity guidance

- A real secret committed to git or returned by an endpoint: **High/Critical**.
- Secret in logs (prompts only, not env): **Low**.
- Binds `0.0.0.0` / no TLS / no headers / root Docker: **Low/Medium hardening** (accepted-
  risk framing per scope skill).

## Reporting template

Use `printforge-finding-report-template`. Separate *exposed secret* (vuln) from *hardening
default* (accepted risk) — they get different severities and different audiences.

## Provenance and maintenance

Supported by `app.py:27-31` (secret env vars), `:1312-1313` (`/config` booleans),
`:143/:161/:188/:1101` (logging), `.gitignore`, `compose.yaml`, `Dockerfile`, `run.sh`.

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -nE 'API_KEY|TOKEN|SECRET|BAMBUDDY|_KEY' app.py     # secret sources
sed -n '1312,1314p' app.py                               # /config returns booleans
git ls-files | grep -iE 'env|secret|key|token' || echo "clean"   # nothing secret tracked
grep -niE 'X-Frame|Content-Security|Strict-Transport|X-Content-Type' app.py  # expect none
grep -n 'USER ' Dockerfile || echo "runs as root"        # Docker user
```

Remaining uncertainty:
- Whether `library/`/`uploads/` are backed up anywhere that leaves the box (the memory
  index mentions website backups fixed to not be public — different repo; confirm no
  PrintForge data is synced off-box).
- Whether codex stderr can, in some failure mode, echo an env secret — treat as Unverified
  until you see it.
