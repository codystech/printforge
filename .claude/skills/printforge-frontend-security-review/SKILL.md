---
name: printforge-frontend-security-review
description: >
  Review PrintForge's browser frontend (static/index.html + vendored three.js) for XSS,
  missing CSP/security headers, client-side trust mistakes, and unsafe local storage.
  Load when asked "XSS", "stored/reflected XSS", "CSP", "content security policy",
  "clickjacking", "does the UI escape model names", "localStorage secrets", or "client-
  side auth". KEY FACT: the UI renders user/model data via document.createElement +
  textContent (not innerHTML with interpolation), so stored XSS via model names/prompts
  is largely NOT present — the real gap is the absence of any CSP or security headers.
  Grounded in static/index.html (textContent at :705, innerHTML only as '' clears).
---

# PrintForge frontend security review

Read `printforge-security-scope-and-rules` first. Frontend = `static/index.html` (~797
lines) plus vendored `static/vendor/three.module.js`, `STLLoader.js`, `OrbitControls.js`.

Working directory: `~/projects/printforge`.

## The good news (don't invent XSS that isn't there)

The library and parts rendering build DOM with `document.createElement` and set
attacker-influenced text via **`textContent`**, not `innerHTML`:
- `name.textContent = m.name` (`:705`), `nameSpan.textContent = p.name...` (`:289`) — model
  names and part names are escaped by the DOM, not injected as HTML.
- The 4 `innerHTML` uses (`:228,:231,:608,:695`) are all **`= ''`** (clearing a container),
  never `innerHTML = <data>` or a template literal with `${userdata}`. Verified:
  `grep -nE 'innerHTML.*(\$\{|`)' static/index.html` → no data-bearing innerHTML.
- `textContent` appears ~26× vs `innerHTML` 4× — the code consistently prefers the safe
  sink.
- `img.src = ` uses `/models/${m.id}/thumb` where `m.id` is server-issued (`:700`), and
  `imgprev.src = dataUrl` (`:512`) is a client-local upload preview.

**Therefore:** do NOT report "stored XSS via model name/prompt" without first showing a
NEW data-bearing `innerHTML`/`insertAdjacentHTML`/`document.write`/`eval` sink. As written,
it isn't there. This is the top false-positive trap for this surface.

## The real gaps (report these)

1. **No Content-Security-Policy, no security headers (Medium hardening).** Neither
   `app.py` nor `index.html` sets CSP, `X-Frame-Options`, `X-Content-Type-Options`, or
   `Referrer-Policy` (`grep` in Provenance). Consequences: (a) **clickjacking** — the UI
   can be framed; on a no-auth app that mostly enables CSRF-style trickery (pair with
   `printforge-authn-authz-review`); (b) **no defense-in-depth** if an XSS sink is ever
   introduced. Recommend a strict CSP (`default-src 'self'`, no inline unless hashed) and
   `X-Frame-Options: DENY`. Frame as hardening, not an active exploit.
2. **Inline scripts** — `index.html` uses a large inline `<script>`. A strict CSP would
   need hashes/nonces or externalizing it. Note this as the reason CSP isn't trivially
   droppable; it's a real remediation cost, not a blocker.
3. **localStorage contents (informational)** — `pf_seen_help` (`:471`), `pf_profile`
   (`:774,:783`). **No tokens or secrets** are stored (there are no tokens in this app).
   Don't report "sensitive data in localStorage" — there's nothing sensitive there.

## Client-side trust boundary

The UI calls the same no-auth API everyone else can hit; there is no client-side "auth"
to bypass and no secret shipped to the browser (`/config` returns booleans only). So
"client-side auth mistake" findings don't apply here — the trust model is server-side-
absent-by-design (scope skill). Don't manufacture a client-auth finding.

## When NOT to use this skill

- The bug is server-side (route, sink, fetch) → the relevant server skill.
- The bug is a missing header enforced server-side that you want to *frame as config* →
  `printforge-secrets-and-config-review` also notes header absence; put the CSP
  *recommendation* here, the config observation there, don't double-count.
- Clickjacking-enabled CSRF → the *impact* (state change) is `printforge-authn-authz-
  review`; this skill owns the *frame-ability* (missing `X-Frame-Options`).

## False-positive traps

- **"Stored XSS via model name."** Rendered with `textContent`. Not present unless a new
  data-bearing HTML sink is added.
- **"Secrets in localStorage."** Only `pf_seen_help` / `pf_profile` — non-sensitive.
- **"DOM XSS via `.src`."** Sources are server-issued ids or local data URLs, not raw
  attacker HTML/JS. Check any new `.src =` before claiming.
- **"No CSP = Critical."** It's Medium hardening (defense-in-depth); severity rises only
  if a real XSS sink coexists.

## Evidence checklist

- [ ] For any XSS claim: the exact `static/index.html:line` of a data-bearing HTML sink
      (`innerHTML =`/`insertAdjacentHTML`/`document.write`/`eval` with `${data}`), and the
      data's server origin.
- [ ] For headers: `curl -sI localhost:8093/` output showing the missing headers (run
      against a **local** instance).
- [ ] Suggested fix: add response-header middleware (CSP + `X-Frame-Options` +
      `X-Content-Type-Options`); regression: a test asserting the headers are present.

## Severity guidance

- Real stored/DOM XSS with a data-bearing sink: **High** (script exec in the operator's
  browser on a no-auth app → can drive every endpoint).
- Missing CSP/headers with no current XSS sink: **Medium/Low** hardening.
- localStorage / client-auth: **Informational** / N/A.

## Reporting template

Use `printforge-finding-report-template`. For XSS, include the exact sink line and a
non-executing PoC payload (e.g. show the string is reflected into HTML context) rather
than a live drive-by.

## Provenance and maintenance

Supported by `static/index.html` (`textContent` rendering at `:289,:705`; `innerHTML`
only as `''` clears at `:228,:231,:608,:695`; `localStorage` at `:471,:774,:783`), and the
absence of CSP/headers in `app.py` and the HTML.

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -nE 'innerHTML|insertAdjacentHTML|document\.write|eval\(' static/index.html   # data-bearing?
grep -nE 'innerHTML.*(\$\{|`)' static/index.html || echo "no data-bearing innerHTML"
grep -niE 'content-security-policy|x-frame-options|x-content-type' app.py static/index.html || echo "no CSP/headers"
grep -nE 'localStorage' static/index.html
```

Remaining uncertainty:
- Future edits could add a data-bearing `innerHTML`; the grep above is the tripwire.
- Whether the vendored three.js files match upstream (supply-chain) — verify hashes vs
  the three.js release if that matters; out of scope for XSS review.
