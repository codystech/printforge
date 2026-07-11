---
name: printforge-file-upload-download-review
description: >
  Review PrintForge's file upload and download paths: /upload-mesh, /import-url
  registration, mesh/SVG storage, and the /stl /export /zip /thumb download routes.
  Load when asked "file upload abuse", "can I upload a malicious file", "extension/
  content-type validation", "where do uploads land", "archive/zip extraction", "download
  authorization", "path traversal on download", or "parser bomb". KEY FACTS: uploaded
  files are stored under uuid names (no filename reaches the path), extensions are
  allowlisted against MESH_EXTS, there is a 100MB cap, and download ids are regex-
  validated — so the residual risk is parser-level (trimesh/cascadio/imagemagick), not
  path or overwrite. Grounded in app.py _register_mesh (:592), _trace_to_svg (:576).
---

# PrintForge file upload / download review

Read `printforge-security-scope-and-rules` first. This skill covers files entering
(`/upload-mesh`, `/import-url`) and leaving (`/stl`, `/export`, `/zip`, `/thumb`).

Working directory: `~/projects/printforge`.

## Upload path — what's already safe (don't re-report)

Confirmed in `_register_mesh` (`app.py:592-650`) and `_trace_to_svg` (`:576`):
- **Extension allowlist**: `ext not in MESH_EXTS` → 415 (`:596`); `MESH_EXTS =
  {.stl,.3mf,.obj,.glb,.gltf,.step,.stp}` plus `.svg` routed to `_register_svg`. Unknown
  extensions rejected.
- **No filename in the path**: stored as `UPLOADS_DIR / f"{mesh_id}{ext}"` where
  `mesh_id = uuid4().hex[:12]` (`:607-608`). The user's `filename` only contributes its
  suffix; it never becomes a path component. → **no path traversal, no overwrite of
  other files.**
- **Size cap**: `len(raw) > 100_000_000` → 413 (`:598`).
- **Content is validated by parsing**: trimesh/cascadio must produce non-empty geometry
  (`:640-644`) or it's rejected — so a `.stl` that is actually a script is just a parse
  failure, not stored-and-served-as-HTML.

## Upload path — residual risks (where to actually look)

1. **Parser bombs / parser CVEs (Medium/Low).** trimesh, `cascadio`/OpenCascade,
   `manifold3d`, and `magick`/`potrace` parse fully attacker-controlled bytes. A crafted
   STEP/3MF/OBJ or image can trigger excessive memory/CPU (DoS) or hit a library CVE. The
   100MB cap limits size but not a small decompression/parse bomb. → cross-check the
   installed versions against advisories in `printforge-dependency-and-supply-chain-
   review`. PoC only as a **local** resource-usage demonstration, never against `:8093`.
2. **STEP originals persist on disk (Low, info).** For `.step/.stp` the original is kept
   (`:646-648`, "true CAD source worth preserving") in `UPLOADS_DIR` (git-ignored). Not a
   vuln by itself; note it for the data-exposure picture (`printforge-secrets-and-config-
   review` covers whether `uploads/` is ever served or backed up somewhere public).
3. **`/import-url` feeds the same registration** — its SSRF risk is owned by
   `printforge-ssrf-and-fetch-review`; the *file* handling once bytes arrive is this skill.
4. **imagemagick on uploaded images** (`_trace_to_svg`, `trace=true`) — ImageMagick has a
   long CVE history (delegate/coder abuse). Confirm the installed `magick` version and
   whether a policy.xml restricts coders. Rate per dependency skill.

## Download path — what's already safe

- `/stl/{id}` (`:1164`) → `_stl_path` requires `[0-9a-f]{32}` and `path.exists()` (`:1155`).
- `/export/{id}` (`:1176`), `/models/{id}/zip` (`:1211`), `/thumb` (`:1345`) → id regex-
  validated via `_stl_path`/`_model_dir`. **No path traversal on downloads.**
- `/zip` (`:1211`) zips `mdir.iterdir()` of a validated model dir — it archives an
  existing library dir, it does not extract an attacker archive, so **no zip-slip on the
  server side** (zip-slip is an *extraction* bug; PrintForge only *creates* zips).

## Download path — residual risks

- **No download authz** — same no-auth reality as everywhere: any LAN user can download
  any model/STL by id. This is the documented trust model, not a per-route bug. Route to
  `printforge-authn-authz-review` for framing; don't double-report.
- **3MF/OBJ/GLB are built into `WORK_DIR` and served** — confirm nothing sensitive shares
  `WORK_DIR` naming (it uses uuid/id-named files; codex output is `codex-<uuid>.txt`).

## When NOT to use this skill

- The risk is the server *fetching* a URL → `printforge-ssrf-and-fetch-review`.
- The risk is a *vulnerable parser version* → `printforge-dependency-and-supply-chain-
  review` (this skill only says "a parser is in the path").
- The risk is "who may download" → `printforge-authn-authz-review`.
- The risk is a secret on disk / in backups → `printforge-secrets-and-config-review`.

## False-positive traps

- **"Path traversal / overwrite via uploaded filename."** The filename never enters the
  path — files are uuid-named. Only the suffix is used. Not exploitable.
- **"Unrestricted file upload → webshell."** Uploads are parsed as meshes and re-exported
  to `.stl`; they are never served as executable/HTML. No web-exec path.
- **"Zip slip."** PrintForge *creates* zips from its own dirs; it does not *extract*
  attacker zips. (3MF is unzipped by trimesh internally — that's a parser concern, item 1,
  not classic zip-slip writing to arbitrary paths. Verify trimesh's 3MF loader doesn't
  write outside temp before claiming zip-slip.)
- **"Content-type spoofing."** Validation is by extension + successful parse, not the
  client's Content-Type header, so a spoofed MIME doesn't bypass anything meaningful.

## Evidence checklist

- [ ] For a parser-bomb/CVE finding: the parser + `app.py:line` it's called from, the
      installed version (dependency skill), and a **local** resource-usage or version-vs-
      advisory proof.
- [ ] For any "traversal/overwrite" claim: show the filename actually reaching a path
      component (it doesn't, per `:607-608` — so you must show a real bypass).
- [ ] Suggested regression test (e.g. assert `_register_mesh` rejects a >100MB or non-
      MESH_EXTS input; assert stored path is uuid-named).

## Severity guidance

- Parser CVE with known RCE at the installed version, reachable via upload: **High** —
  but only with a version match to a real advisory; otherwise **Candidate/Medium**.
- Parser DoS (memory/CPU exhaustion): **Low/Medium** (no auth means anyone on LAN can
  trigger it, but impact is availability of a personal tool).
- Persisted STEP originals / no download authz: fold into data-exposure / authz, not new
  criticals.

## Reporting template

Use `printforge-finding-report-template`. Name the parser and the trigger file precisely.

## Provenance and maintenance

Supported by `app.py:592-650` (`_register_mesh`: extension allowlist, uuid naming, size
cap, parse validation), `:576-584` (`_trace_to_svg`), `:1155/:1176/:1211/:1345` (download
routes with id validation).

Re-verify (read-only, from `~/projects/printforge`):
```sh
sed -n '592,650p' app.py                    # upload handling
grep -n 'MESH_EXTS' app.py                  # allowlist definition
grep -nE 'uuid4\(\)\.hex' app.py            # uuid file naming
grep -nE 'iterdir|extractall|ZipFile' app.py  # confirm create-only, no extractall of uploads
```

Remaining uncertainty:
- Whether trimesh's internal 3MF (zip) loader can be induced to write outside temp —
  verify against trimesh source/advisories before claiming zip-slip.
- Exact installed versions of trimesh/cascadio/manifold3d/imagemagick → dependency skill.
