---
name: printforge-ssrf-and-fetch-review
description: >
  Review PrintForge's server-side outbound HTTP for SSRF (Server-Side Request Forgery)
  and webhook/integration abuse. Load when reviewing /import-url, /send (Bambuddy), the
  Printables/Thingiverse importers, or any place PrintForge fetches a URL on the
  server. Trigger phrases: "SSRF", "can it fetch internal hosts", "import from URL",
  "server-side request", "metadata service", "fetch a link", "webhook", "does it call
  out". This is the HIGHEST-VALUE surface in PrintForge — the /import-url else-branch
  fetches an attacker-supplied URL with redirects followed and no host allowlist.
  Grounded in app.py:739-766, :1236-1250.
---

# PrintForge SSRF and outbound-fetch review

Read `printforge-security-scope-and-rules` first. **SSRF** = you make the server fetch a
URL of *your* choosing, so the request comes from the server's network position — which
can reach internal hosts, cloud metadata endpoints, and loopback services that you
cannot reach directly. On this box that means the Bambuddy VM, the LiteLLM brain, ollama,
and anything else on Cody's LAN.

Working directory: `~/projects/printforge`.

## The primary candidate — `/import-url` else-branch (`app.py:739-766`)

Confirmed from code (`:763`):
```python
elif url.lower().split("?")[0].endswith((".stl", ".3mf", ".obj")):
    dl = await client.get(url, headers=UA, follow_redirects=True)
    raw, name = dl.content, Path(httpx.URL(url).path).name
```
`req.url` is attacker-controlled. The only gate is the branch order in `import_url`:
`printables.com`/`thingiverse.com`/`makerworld`/`cults3d`/`myminifactory` hostnames are
handled by fixed-host importers; **everything else that ends in `.stl/.3mf/.obj` is
fetched directly.** There is:
- **no allowlist** of hosts,
- **no block** on `127.0.0.1`, `localhost`, RFC1918 (`10./172.16-31./192.168.`),
  link-local `169.254.169.254` (cloud metadata), or `[::1]`,
- **`follow_redirects=True`** — so even if the *initial* URL's path must end in
  `.stl/.3mf/.obj`, an attacker's own server can 302-redirect to any internal URL.
- the fetched **body is returned to the caller** (registered as a mesh) → this is a
  **full-read SSRF**, not blind.

Why it matters here: `BAMBUDDY_URL` defaults to `http://192.168.1.50:8000` (`:30`) —
proof internal HTTP services exist on this network. A crafted import can make PrintForge
read from them and hand the response back.

### Hypothesis to test (Candidate until proven, LOCAL ONLY)

Prediction: `POST /import-url {"url": "http://<attacker>/x.stl"}` where `<attacker>`
redirects to an internal URL causes PrintForge to fetch the internal URL and return its
body as a "mesh". **Do NOT test this against the real Bambuddy VM or any host outside the
authorized table.** Prove it entirely locally:

1. Stand up a **local throwaway PrintForge** on a spare port (see
   `printforge-security-testing-playbook`) — never the live `:8093`.
2. Run a tiny local HTTP server you own on `127.0.0.1` that serves a **canary file you
   created** (e.g. `CANARY-not-a-secret`) at a path with no `.stl` suffix.
3. From the same box, run a local redirector `http://127.0.0.1:PORT/x.stl` → the canary.
4. `curl -s -X POST localhost:<throwaway>/import-url -H 'content-type: application/json'
   -d '{"url":"http://127.0.0.1:PORT/x.stl"}'` and observe whether the canary bytes come
   back / are registered.

Success = the server fetched a loopback URL it should never reach on your behalf. That
proves SSRF **without touching any real internal service or secret.** Stop there; do not
pivot to `169.254.169.254` or the Bambuddy VM to "prove impact harder" — the loopback
canary already proves the class. Escalate to Cody per the stop conditions.

## Secondary surfaces (lower priority)

| Surface | Line | Attacker control | Verdict guidance |
|---|---|---|---|
| `_printables_import` | `~:713` | model id only (`\d+` from URL) | host fixed → not SSRF; check the id is not injected into the GraphQL query unsafely (input skill) |
| `_thingiverse_import` | `~:730` | thing id only | host fixed → not SSRF |
| `/send/{id}` → Bambuddy | `:1244` | destination is `BAMBUDDY_URL` env, **not** the request | not request-driven SSRF; but a compromised/renamed env var would redirect uploads — config concern, see secrets skill |
| LLM HTTP backend | `:168,:971` | `LLM_BASE_URL` env | config, not request-driven |
| `_free_gpu` | `~:1290` | fixed `127.0.0.1:11434` | server-controlled, not a finding |

## When NOT to use this skill

- The fetch destination is a **fixed hostname or an env var**, not derived from the
  request → it is not request-driven SSRF; route config concerns to
  `printforge-secrets-and-config-review`.
- You are reviewing how an *uploaded file's bytes* are parsed → that is
  `printforge-file-upload-download-review`, not this.
- You are reviewing the OpenSCAD/codex sinks → `printforge-input-and-injection-review`.

## False-positive traps

- The `printables`/`thingiverse` branches look like they fetch user URLs but the
  **host is fixed** and only a numeric id varies — not SSRF. Don't report them as SSRF.
- `/send` uploads to `BAMBUDDY_URL` — that's an *outbound integration to owned staging*,
  configured by env, not chosen per-request. Not SSRF. (It IS a data-egress + secret
  path — cover that in secrets/data-exposure, not here.)
- If a future commit adds an IP/host allowlist or blocks RFC1918 before the fetch,
  re-test — the finding may be fixed. Show the current code, not memory.
- A fetch that only ever hits `127.0.0.1` services the server already owns
  (`_free_gpu`) is not a finding.

## Evidence checklist for an SSRF finding

- [ ] Exact route + `app.py:line` of the fetch.
- [ ] Proof the destination is derived from request input (show the code path).
- [ ] Proof no allowlist / no internal-IP block sits before the fetch.
- [ ] A **local, loopback-only** PoC hitting a canary you created (never a real secret).
- [ ] Note whether it is full-read (body returned) or blind — full-read is worse.
- [ ] Suggested regression test (e.g. a unit test asserting `/import-url` rejects
      `127.0.0.1`/RFC1918/`169.254.169.254` hosts and does not follow redirects to them).

## Severity guidance

- Full-read SSRF reaching internal services on a no-auth LAN service: **High** (can read
  internal HTTP responses, incl. potentially the Bambuddy API). If a metadata/credential
  endpoint is demonstrably reachable in this environment, argue up toward **Critical** —
  but only with a *reachability* proof done safely, never by dumping real credentials.
- Blind SSRF with no useful internal target: **Medium**.
- Downgrade if a network control (firewall/no internal HTTP targets) already blocks it —
  but on this LAN, internal HTTP targets demonstrably exist. Note the control if present.

## Reporting template

Use `printforge-finding-report-template`. Title format: "SSRF: /import-url fetches
attacker-controlled URLs into the internal network (app.py:763)".

## Provenance and maintenance

Supported by `app.py:739-766` (`import_url`, the else-branch fetch with
`follow_redirects=True`), `app.py:30` (`BAMBUDDY_URL` internal default), and
`app.py:1236-1250` (`/send`).

Re-verify (read-only, from `~/projects/printforge`):
```sh
sed -n '739,766p' app.py                         # the branch logic + else-fetch
grep -n 'follow_redirects' app.py                # expect True on the import fetch
grep -nE '127\.0\.0\.1|169\.254|RFC1918|ipaddress|is_private|allowlist' app.py  # expect: NO SSRF guard
```

Remaining uncertainty:
- Whether httpx or an upstream proxy blocks loopback in this deployment (unlikely; verify
  with the local PoC).
- Whether a future commit adds host filtering — the grep above will show it.
