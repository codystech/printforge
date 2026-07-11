---
name: printforge-input-and-injection-review
description: >
  Review how PrintForge handles untrusted input for injection and unsafe parsing:
  OpenSCAD `-D` parameter injection, the codex/LLM code-execution pipeline, subprocess
  argument handling, path traversal, GraphQL/query building, and deserialization of
  uploaded meshes (STL/3MF/STEP via trimesh/cascadio, XML via ElementTree). Load when
  asked "command injection", "code injection", "can input reach a shell", "OpenSCAD
  injection", "unsafe deserialization", "XXE", "prompt injection into generation", "path
  traversal", or "is this input validated". KEY FACT: all subprocess calls use argv
  lists (no shell=True) and ids are regex-validated — so classic shell/traversal
  injection is largely blocked; this skill teaches where residual injection actually
  lives. Grounded in app.py render_stl (:175), call_codex (:102), _trace_to_svg (:576).
---

# PrintForge input handling & injection review

Read `printforge-security-scope-and-rules` first. This skill covers **content-of-input**
bugs: what happens when attacker-controlled *values* flow into a command, a parser, a
query, or a filesystem path. (The *server fetching a URL* is SSRF → different skill.)

Working directory: `~/projects/printforge`.

Jargon: **argv list** = a subprocess invoked as `["cmd","arg1","arg2"]` — the OS runs
`cmd` directly with those exact args, **no shell**, so `;`, `|`, `$()`, backticks are
inert. **`shell=True`** = the string is handed to `/bin/sh`, where metacharacters inject.
PrintForge uses argv lists everywhere (verified) — that is the single most important
fact for triaging "command injection" here.

## Sink-by-sink

### 1. OpenSCAD `-D` parameter injection (`render_stl`, `app.py:175-193`)
```python
if not re.fullmatch(r"\w+", k):        # :181  key: letters/digits/underscore only
    raise HTTPException(400, ...)
val = f'"{v}"' if isinstance(v, str) else str(v)   # :183
cmd += ["-D", f"{k}={val}"]            # :184  argv element, NOT a shell string
```
- Keys are `\w+` validated → safe.
- Values: strings are wrapped in double quotes but embedded `"` are **not escaped**. A
  value like `a"; <openscad-expr>` could break out of the string literal *inside the
  OpenSCAD expression* passed via `-D`. Because it is an argv element, this is **not host
  command injection** — it is at most **OpenSCAD expression injection** into the render.
  Impact ceiling: OpenSCAD has no shell/exec; worst case is `import("<path>")` reading a
  local file into geometry (info-leak-ish, but output is a mesh, not the file bytes) or a
  DoS via a heavy expression. Rate this **Low** (Candidate) unless you can show a
  concrete impact. Suggested hardening: reject `"` in string values or use `--` / a
  params file.
- `/render`'s param source is slider overrides from the UI; still attacker-reachable
  since there's no auth.

### 2. The codex / LLM code pipeline (`call_codex` :102, `call_codex_edit` :119)
The LLM writes OpenSCAD from the user's prompt; `openscad` then executes that OpenSCAD.
This is a **generate-then-run** pipeline — the classic worry is "prompt injection →
malicious OpenSCAD → RCE". Mitigations in place:
- `call_codex` runs `codex exec -s read-only --ephemeral` (`:108`) — codex's **read-only
  sandbox**.
- `call_codex_edit` runs `-s workspace-write -C <job>` (`:134`) — writes only in a
  per-job scratch dir under `WORK_DIR`.
- OpenSCAD itself cannot spawn processes.
So a prompt-injected model *cannot* trivially get host RCE. **Review focus:** confirm the
sandbox flags are still `read-only` / `workspace-write` (not `danger-full-access`), and
that the scratch `-C` dir is inside `WORK_DIR` and not a sensitive path. If a future
commit loosens the sandbox flag, that is a **High** finding. Prompt-injection that makes
the model emit attacker-chosen OpenSCAD is real but bounded by the sandbox + "output is a
mesh" — rate the *content* risk **Low/Medium**, the *sandbox-loosening* risk **High**.

### 3. Subprocess argv sinks (magick/potrace/openscad/nix)
`_trace_to_svg` (`:576-584`): `subprocess.run(["magick", str(src), ...], ...)` and
`nix shell nixpkgs#potrace --command potrace ...` — argv lists, no shell. The uploaded
**filename** only contributes its suffix via `Path(filename).suffix` into a uuid-named
`job` path (`:580`) — the filename does **not** reach a shell or a path prefix. Not
command injection. (The *bytes* go to imagemagick/potrace — that's a parser-CVE surface,
covered by dependency + file-upload skills.)

### 4. GraphQL / API id interpolation (Printables)
`_printables_import` (`~:709`) builds `query {{ print(id: "{model_id}") ... }}` by string
interpolation. `model_id` comes from `re.search(r"/model/(\d+)", url)` (`:745`) → digits
only, so no GraphQL injection. Confirm the `\d+` extraction still guards it before
calling this safe. If a future change passes an unfiltered id, it becomes injectable.

### 5. Path traversal
All object ids are regex-validated before being joined to a directory: `_stl_path`
`[0-9a-f]{32}` (`:1155`), `_model_dir` `[0-9a-f]{12}` (`:1317`), mesh ids `[0-9a-f]{12}`
(`:793,:1200`), part names `\w+` (`:1424`). Traversal via these is **blocked**. Only
report traversal if you find a filesystem join whose component is **not** regex-guarded
(grep for `LIB_DIR /`, `UPLOADS_DIR /`, `WORK_DIR /` and check each variable).

### 6. Deserialization of uploads (trimesh / cascadio / ElementTree)
`_register_mesh` (`:592`) loads uploaded STL/3MF/OBJ/GLB/STEP via `trimesh` and STEP via
`cascadio`/OpenCascade; `xml.etree.ElementTree` is imported (`:558`) for XML handling.
- **Unsafe deserialization**: these are binary/mesh parsers, not `pickle`/`yaml.load` —
  grep confirms no `pickle`/`yaml.load`/`eval`/`marshal` on untrusted input. So classic
  object-injection deserialization is **not** present. Don't report it unless you find
  such a call.
- **XXE (XML External Entity)**: 3MF is zip+XML; SVG is XML. `xml.etree.ElementTree` in
  modern CPython does **not** resolve external entities by default, so XXE is unlikely —
  but confirm no `lxml` parser with `resolve_entities=True` is used on untrusted input
  (`grep -n lxml app.py`; the Dockerfile installs `lxml` but app.py imports stdlib ET at
  `:558`). Rate any real XXE **High**; absent evidence, mark **not present / Unverified**.
- **Parser memory/DoS**: a malformed or huge STEP/mesh can exhaust memory/CPU. 100MB cap
  exists (`:598`) but a small file can still be a decompression/parse bomb. Rate **Low/
  Medium** DoS; note it, don't overstate.

## When NOT to use this skill

- The server fetches a URL → `printforge-ssrf-and-fetch-review`.
- The question is "who is allowed to call this" → `printforge-authn-authz-review`.
- The question is a vulnerable *dependency version* → `printforge-dependency-and-supply-
  chain-review`.
- The bug is a secret in a log/config → `printforge-secrets-and-config-review`.

## False-positive traps

- **"Command injection via filename/prompt/param."** All sinks are argv lists, no
  `shell=True`. Show a shell sink or it's not command injection. (Re-verify:
  `grep -n 'shell=True' app.py` → expect none.)
- **"Path traversal via {id}."** Regex-guarded. Show a bypass or an unguarded join.
- **"Unsafe deserialization."** No `pickle`/`yaml.load`/`eval` on input. It's a mesh
  parser.
- **"RCE via LLM prompt injection."** Bounded by the codex sandbox; not host RCE unless
  the sandbox flag is loosened. Report the *flag*, not a hypothetical.
- OpenSCAD `-D` value injection is **OpenSCAD-level**, not host-level. Don't inflate it.

## Evidence checklist

- [ ] Exact sink `app.py:line` and the untrusted value's path to it.
- [ ] For "injection": show the metacharacter actually reaches an interpreter (shell,
      OpenSCAD, GraphQL) — with a **local** PoC, non-destructive.
- [ ] Confirm no argv→shell conversion (`shell=True`, `os.system`, `f"...{x}..."` into a
      shell string).
- [ ] Suggested regression test (e.g. reject `"` in `/render` string params; assert the
      codex sandbox flag stays `read-only`/`workspace-write`).

## Severity guidance

- Loosened codex sandbox flag (`danger-full-access`/no sandbox) reachable from a prompt:
  **High/Critical**.
- Real XXE reading local files: **High**.
- OpenSCAD `-D` expression injection: **Low** (bounded by OpenSCAD's lack of exec).
- Parser DoS: **Low/Medium**.

## Reporting template

Use `printforge-finding-report-template`. Name the interpreter reached (shell vs OpenSCAD
vs GraphQL) precisely — the severity turns on it.

## Provenance and maintenance

Supported by `app.py:175-193` (`render_stl`), `:102-144` (codex sandbox flags), `:576-584`
(`_trace_to_svg`), `:592-598` (`_register_mesh`), `:709` (GraphQL), `:558` (stdlib ET),
and id-validation regexes.

Re-verify (read-only, from `~/projects/printforge`):
```sh
grep -n 'shell=True\|os\.system\|os\.popen' app.py        # expect: none
grep -nE 'codex.*-s (read-only|workspace-write|danger)' app.py  # expect: read-only / workspace-write
grep -nE 'pickle|yaml\.load|eval\(|marshal|resolve_entities' app.py  # expect: none on input
grep -n 'lxml' app.py                                     # expect: none (stdlib ET used)
sed -n '175,193p' app.py                                  # -D param quoting
```

Remaining uncertainty:
- OpenSCAD's exact behavior on a crafted `-D` string was not exercised; treat as Candidate.
- Whether trimesh/cascadio have a known parser CVE at the installed version → dependency
  skill.
