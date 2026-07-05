import asyncio
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import httpx
import trimesh
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from parts import floating_starts, split_parts, write_3mf
from prompts import SYSTEM_PROMPT, qa_prompt, user_prompt

# "codex" shells out to the codex CLI (gpt-5.5, host only) and falls back to HTTP on failure
LLM_BACKEND = os.environ.get("LLM_BACKEND", "http")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:4000/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-brain-coder")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "dummy")
QA_CHECK = os.environ.get("QA_CHECK", "1") == "1"  # vision self-check, codex backend only
QA_ROUNDS = int(os.environ.get("QA_ROUNDS", "2"))  # max look-fix-rerender iterations
BAMBUDDY_URL = os.environ.get("BAMBUDDY_URL", "http://192.168.1.50:8000")
BAMBUDDY_API_KEY = os.environ.get("BAMBUDDY_API_KEY", "")
OPENSCAD_TIMEOUT = 60  # seconds; complex models can be slow
# needs openscad 2024+ (nixpkgs#openscad-unstable); set empty for old 2021.01 builds
OPENSCAD_ARGS = os.environ.get("OPENSCAD_ARGS", "--enable=textmetrics --enable=manifold").split()
WORK_DIR = Path(tempfile.gettempdir()) / "printforge"
WORK_DIR.mkdir(exist_ok=True)
LIB_DIR = Path(__file__).parent / "library"
LIB_DIR.mkdir(exist_ok=True)
UPLOADS_DIR = Path(__file__).parent / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)

app = FastAPI(title="PrintForge")

# name = 12.5; // [10:100] or [10:5:100]  → slider
# name = "text"; // free text             → text input
PARAM_RE = re.compile(
    r'^(\w+)\s*=\s*([\d.]+|"[^"]*")\s*;\s*//\s*(?:\[([\d.:\-]+)\]|(free text))',
    re.MULTILINE,
)


class GenerateRequest(BaseModel):
    prompt: str
    current_scad: str | None = None
    image: str | None = None  # data URL (photo/sketch reference)
    mesh_id: str | None = None  # uploaded base mesh to remix


class RenderRequest(BaseModel):
    scad: str
    params: dict[str, float | str] = {}


class RenameRequest(BaseModel):
    name: str


def parse_params(scad: str) -> list[dict]:
    params = []
    for m in PARAM_RE.finditer(scad):
        name, value, rng, free = m.groups()
        if free or value.startswith('"'):
            params.append({"name": name, "type": "text", "value": value.strip('"')})
        else:
            parts = [float(p) for p in (rng or "0:100").split(":")]
            lo, step, hi = (parts[0], parts[1], parts[2]) if len(parts) == 3 else (parts[0], 0, parts[-1])
            params.append({
                "name": name, "type": "number", "value": float(value),
                "min": lo, "max": hi, "step": step or (1 if float(value) == int(float(value)) else 0.1),
            })
    return params


def strip_fences(text: str) -> str:
    # LLMs sometimes wrap output in ``` despite instructions
    text = text.strip()
    text = re.sub(r"^```[a-z]*\n", "", text)
    text = re.sub(r"\n```$", "", text)
    return text


def call_codex(messages: list[dict], images: list[str] | None = None) -> str:
    prompt = "\n\n".join(
        m["content"] if m["role"] == "system" else f"[{m['role']}]\n{m['content']}"
        for m in messages
    )
    out = WORK_DIR / f"codex-{uuid.uuid4().hex}.txt"
    cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral",
           "-o", str(out)]
    for img in images or []:
        cmd += ["-i", img]
    cmd.append("-")
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=420)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"codex exec failed: {proc.stderr[-500:]}")
    return strip_fences(out.read_text())


def call_codex_edit(scad: str, instruction: str, images: list[str] | None = None) -> str:
    """Have codex EDIT the file in a scratch workspace instead of re-printing it —
    wholesale rewrites of long files reliably drop unrelated features."""
    job = WORK_DIR / f"edit-{uuid.uuid4().hex}"
    job.mkdir()
    f = job / "model.scad"
    f.write_text(scad)
    prompt = (
        f"{instruction}\n\n"
        "Apply your changes by EDITING model.scad in this directory with precise, "
        "minimal edits. Every module, feature and parameter not affected by the request "
        "must survive byte-for-byte. Never rewrite the file from scratch. If no change "
        "is needed, edit nothing."
    )
    cmd = ["codex", "exec", "-C", str(job), "-s", "workspace-write",
           "--skip-git-repo-check", "--ephemeral"]
    for img in images or []:
        cmd += ["-i", img]
    cmd.append("-")
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=420)
    if proc.returncode != 0:
        raise RuntimeError(f"codex edit failed: {proc.stderr[-500:]}")
    return f.read_text()


async def call_llm(messages: list[dict], images: list[str] | None = None) -> str:
    if LLM_BACKEND == "codex":
        try:
            return await asyncio.to_thread(call_codex, messages, images)
        except Exception as e:
            if images:
                raise HTTPException(502, f"codex backend failed and image input needs codex: {e}")
            print(f"codex backend failed ({e}); falling back to {LLM_MODEL}")
    if images:
        raise HTTPException(422, "image input needs the codex backend (LLM_BACKEND=codex)")
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            f"{LLM_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            json={"model": LLM_MODEL, "messages": messages, "temperature": 0.2},
        )
        r.raise_for_status()
        return strip_fences(r.json()["choices"][0]["message"]["content"])


def render_stl(scad: str, params: dict) -> Path:
    job = WORK_DIR / uuid.uuid4().hex
    scad_file, stl_file = job.with_suffix(".scad"), job.with_suffix(".stl")
    scad_file.write_text(scad)
    cmd = ["openscad", *OPENSCAD_ARGS, "-o", str(stl_file), "--export-format", "binstl"]
    for k, v in params.items():
        if not re.fullmatch(r"\w+", k):
            raise HTTPException(400, f"bad param name: {k}")
        val = f'"{v}"' if isinstance(v, str) else str(v)
        cmd += ["-D", f"{k}={val}"]
    cmd.append(str(scad_file))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=OPENSCAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "OpenSCAD render timed out")
    if proc.returncode != 0 or not stl_file.exists():
        raise HTTPException(422, f"OpenSCAD error:\n{proc.stderr[-2000:]}")
    if stl_file.stat().st_size > 100_000_000:
        raise HTTPException(413, "STL too large")
    return stl_file


def render_png(scad_path: Path, out_png: Path, camera: str | None = None,
               imgsize: str = "800,600", ortho: bool = True, fit: bool = True) -> Path | None:
    cmd = ["openscad", *OPENSCAD_ARGS, "-o", str(out_png), "--imgsize", imgsize]
    if fit:
        cmd += ["--autocenter", "--viewall"]
    if camera:
        cmd += ["--camera", camera, "--projection", "o" if ortho else "p"]
    cmd.append(str(scad_path))
    try:
        subprocess.run(cmd, capture_output=True, timeout=OPENSCAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    return out_png if out_png.exists() else None


def _cluster_bboxes(pts, cell=8.0, max_regions=3):
    """Group points into spatial clusters via a voxel grid; biggest clusters first."""
    import numpy as np
    from scipy import ndimage
    mn = pts.min(axis=0)
    idx = np.floor((pts - mn) / cell).astype(int)
    grid = np.zeros(idx.max(axis=0) + 3, dtype=bool)
    grid[tuple((idx + 1).T)] = True
    lab, n = ndimage.label(grid, structure=np.ones((3, 3, 3)))
    labels = lab[tuple((idx + 1).T)]
    out = []
    for i in range(1, n + 1):
        p = pts[labels == i]
        if len(p) >= 8:
            out.append((len(p), p.min(axis=0), p.max(axis=0)))
    out.sort(key=lambda t: -t[0])
    return [(a, b) for _, a, b in out[:max_regions]]


def mesh_changes(base_stl: Path, new_stl: Path):
    """Added-geometry cluster bboxes and removed-material info vs the base mesh."""
    import numpy as np
    base = trimesh.load_mesh(base_stl)
    new = trimesh.load_mesh(new_stl)
    bc = np.round(base.triangles_center, 1)
    nc = np.round(new.triangles_center, 1)
    bset, nset = set(map(tuple, bc)), set(map(tuple, nc))
    added = new.triangles_center[[tuple(c) not in bset for c in nc]]
    removed = base.triangles_center[[tuple(c) not in nset for c in bc]]
    added_regions = _cluster_bboxes(added) if len(added) else []
    removed_info = None
    if len(removed) > 0.02 * len(bc):  # >2% of base surface gone = suspicious
        clusters = _cluster_bboxes(removed, max_regions=1)
        if clusters:
            removed_info = {"fraction": len(removed) / len(bc), "bbox": clusters[0]}
    return added_regions, removed_info


async def vision_qa(prompt: str, scad: str, stl: Path,
                    base_stl: Path | None = None) -> tuple[str, Path, str]:
    """One round of render-and-review; returns (scad, stl, qa_status)."""
    # oblique perspective views: straight-on orthographic hides low relief entirely
    scad_path = WORK_DIR / f"{stl.stem}.scad"
    views = []
    qa_notes = []
    # per-cluster close-ups of geometry that differs from the base mesh — whole-model
    # renders make a 10mm addition a smudge the reviewer cannot judge
    if base_stl and base_stl.exists():
        try:
            regions, removed = await asyncio.to_thread(mesh_changes, base_stl, stl)
        except Exception:
            regions, removed = [], None
        region_descs = []
        for i, (mn, mx) in enumerate(regions):
            c = [(a + b) / 2 for a, b in zip(mn, mx)]
            dist = max(8.0, 2.2 * max(b - a for a, b in zip(mn, mx)))
            region_descs.append(
                f"region {i + 1}: x {mn[0]:.0f}..{mx[0]:.0f}, y {mn[1]:.0f}..{mx[1]:.0f}, "
                f"z {mn[2]:.0f}..{mx[2]:.0f}")
            for az, tag in ((30, f"r{i}a"), (210, f"r{i}b")):
                views.append(render_png(
                    scad_path, WORK_DIR / f"{stl.stem}_{tag}.png",
                    camera=f"{c[0]:.1f},{c[1]:.1f},{c[2]:.1f},65,0,{az},{dist:.0f}",
                    ortho=False, imgsize="1000,750", fit=False))
        if region_descs:
            qa_notes.append(
                "Visible NEW geometry was detected only in these regions (union erases "
                "anything placed inside an existing solid): " + "; ".join(region_descs) +
                ". Match every requested element to one of these regions — a requested "
                "element with no region of its own is buried inside other geometry or "
                "missing entirely; return a corrected file that moves it into open space."
            )
        if removed:
            mn, mx = removed["bbox"]
            qa_notes.append(
                f"NOTE: {removed['fraction']:.0%} of the BASE mesh surface was removed, "
                f"concentrated around x {mn[0]:.0f}..{mx[0]:.0f}, y {mn[1]:.0f}..{mx[1]:.0f}, "
                f"z {mn[2]:.0f}..{mx[2]:.0f}. Decide from the request whether the user wanted "
                "the base changed there (cuts/engraving/creative reshaping are legitimate "
                "when asked for). If they did not, that is damage — return a corrected file "
                "that preserves the base."
            )
    try:
        floats = await asyncio.to_thread(floating_starts, stl)
    except Exception:
        floats = []
    if floats:
        desc = "; ".join(f"z={f['z']} at ({f['x']},{f['y']}) ~{f['area']}mm2" for f in floats[:8])
        qa_notes.append(
            f"PRINTABILITY: {len(floats)} feature(s) begin in MID-AIR when sliced bottom-up: "
            f"{desc}. Slicers reject these. Fix each by extending the feature downward until "
            "it fuses with whatever is below it (embed 2-3mm into the surface), or give it a "
            ">=45-degree self-supporting underside. A ball sitting on a post tip is fine; "
            "ignore those."
        )
    views.append(render_png(scad_path, WORK_DIR / f"{stl.stem}_iso.png"))
    views.append(render_png(scad_path, WORK_DIR / f"{stl.stem}_top.png", camera="0,0,0,0,0,0,340"))
    views.append(render_png(scad_path, WORK_DIR / f"{stl.stem}_ob1.png",
                            camera="0,0,0,70,0,25,340", ortho=False, imgsize="1000,750"))
    views.append(render_png(scad_path, WORK_DIR / f"{stl.stem}_ob2.png",
                            camera="0,0,0,70,0,205,340", ortho=False, imgsize="1000,750"))
    images = [str(p) for p in views if p]
    if not images:
        return scad, stl, "skipped"
    edited = await asyncio.to_thread(
        call_codex_edit, scad,
        qa_prompt(prompt, "(the code is model.scad in this directory)", qa_notes), images
    )
    if edited.strip() == scad.strip():
        return scad, stl, "passed"
    try:
        fixed_stl = render_stl(edited, {})
    except HTTPException:
        return scad, stl, "passed"  # ponytail: fix didn't render, ship the original
    return edited, fixed_stl, "fixed"


def save_to_library(prompt: str, scad: str, stl: Path) -> str:
    model_id = uuid.uuid4().hex[:12]
    mdir = LIB_DIR / model_id
    mdir.mkdir()
    (mdir / "model.scad").write_text(scad)
    (mdir / "meta.json").write_text(json.dumps({
        "id": model_id,
        "name": prompt.strip()[:60],
        "prompt": prompt,
        "created": time.time(),
    }))
    render_png(WORK_DIR / f"{stl.stem}.scad", mdir / "thumb.png", imgsize="400,300")
    return model_id


def _register_mesh(raw: bytes, filename: str) -> dict:
    ext = Path(filename or "m.stl").suffix.lower()
    if ext not in {".stl", ".3mf", ".obj"}:
        raise HTTPException(415, "only .stl, .3mf or .obj meshes")
    if len(raw) > 100_000_000:
        raise HTTPException(413, "mesh too large")
    mesh_id = uuid.uuid4().hex[:12]
    tmp = UPLOADS_DIR / f"{mesh_id}{ext}"
    tmp.write_bytes(raw)
    try:
        m = trimesh.load(tmp, force="mesh")
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(422, f"could not read mesh: {e}")
    stl_path = UPLOADS_DIR / f"{mesh_id}.stl"
    m.export(stl_path)
    if tmp != stl_path:
        tmp.unlink()
    # horizontal cross-sections so the LLM knows where surfaces actually are per height
    sections = []
    z0, z1 = m.bounds[0][2], m.bounds[1][2]
    for frac in (0.1, 0.3, 0.5, 0.7, 0.9):
        z = z0 + frac * (z1 - z0)
        try:
            s = m.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
            if s is not None:
                b = s.bounds
                sections.append({"z": round(z, 1),
                                 "x": [round(b[0][0], 1), round(b[1][0], 1)],
                                 "y": [round(b[0][1], 1), round(b[1][1], 1)]})
        except Exception:
            pass
    meta = {"id": mesh_id, "name": filename,
            "bounds_min": [round(v, 1) for v in m.bounds[0]],
            "bounds_max": [round(v, 1) for v in m.bounds[1]],
            "sections": sections}
    (UPLOADS_DIR / f"{mesh_id}.json").write_text(json.dumps(meta))
    return meta


@app.post("/upload-mesh")
async def upload_mesh(file: UploadFile = File(...)):
    return _register_mesh(await file.read(), file.filename or "m.stl")


class ImportUrlRequest(BaseModel):
    url: str


PRINTABLES_GQL = "https://api.printables.com/graphql/"
UA = {"User-Agent": "Mozilla/5.0 (PrintForge)"}


async def _printables_import(model_id: str, client: httpx.AsyncClient) -> tuple[bytes, str]:
    q = f'query {{ print(id: "{model_id}") {{ name stls {{ id name fileSize }} }} }}'
    r = await client.post(PRINTABLES_GQL, headers=UA, json={"query": q})
    data = (r.json().get("data") or {}).get("print")
    if not data or not data.get("stls"):
        raise HTTPException(404, "no STL files on that Printables model")
    stl = max(data["stls"], key=lambda s: s.get("fileSize") or 0)
    mq = (f'mutation {{ getDownloadLink(id: "{stl["id"]}", printId: "{model_id}", '
          f'fileType: stl, source: model_detail) {{ ok output {{ link }} }} }}')
    r = await client.post(PRINTABLES_GQL, headers=UA, json={"query": mq})
    link = ((r.json().get("data") or {}).get("getDownloadLink") or {}).get("output", {}).get("link")
    if not link:
        raise HTTPException(502, "Printables did not return a download link")
    dl = await client.get(link, headers=UA, follow_redirects=True)
    return dl.content, stl["name"]


async def _thingiverse_import(thing_id: str, client: httpx.AsyncClient) -> tuple[bytes, str]:
    token = os.environ.get("THINGIVERSE_TOKEN", "")
    if not token:
        raise HTTPException(400, "Thingiverse needs THINGIVERSE_TOKEN — create a free app "
                                 "token at thingiverse.com/developers and add it to .env")
    auth = {"Authorization": f"Bearer {token}", **UA}
    r = await client.get(f"https://api.thingiverse.com/things/{thing_id}/files", headers=auth)
    files = [f for f in r.json() if f.get("name", "").lower().endswith((".stl", ".obj", ".3mf"))]
    if not files:
        raise HTTPException(404, "no mesh files on that thing")
    f = max(files, key=lambda x: x.get("size") or 0)
    dl = await client.get(f["download_url"], headers=auth, follow_redirects=True)
    return dl.content, f["name"]


@app.post("/import-url")
async def import_url(req: ImportUrlRequest):
    url = req.url.strip()
    host = (httpx.URL(url).host or "").lower()
    async with httpx.AsyncClient(timeout=120) as client:
        if "printables.com" in host:
            m = re.search(r"/model/(\d+)", url)
            if not m:
                raise HTTPException(400, "use a printables.com/model/<id>-… link")
            raw, name = await _printables_import(m.group(1), client)
        elif "thingiverse.com" in host:
            m = re.search(r"thing:(\d+)|/thing/(\d+)", url)
            if not m:
                raise HTTPException(400, "use a thingiverse.com/thing:<id> link")
            raw, name = await _thingiverse_import(m.group(1) or m.group(2), client)
        elif "makerworld" in host:
            raise HTTPException(400, "MakerWorld has no public API — download the model "
                                     "with Bambu Studio or your browser, then attach the "
                                     "file with the \U0001F4E6 button")
        elif "cults3d.com" in host or "myminifactory.com" in host:
            raise HTTPException(400, "that site needs a login/API key for downloads — "
                                     "download the file in your browser, then attach it "
                                     "with the \U0001F4E6 button")
        elif url.lower().split("?")[0].endswith((".stl", ".3mf", ".obj")):
            dl = await client.get(url, headers=UA, follow_redirects=True)
            raw, name = dl.content, Path(httpx.URL(url).path).name
        else:
            raise HTTPException(400, "unsupported link — paste a Printables/Thingiverse "
                                     "model URL or a direct .stl/.3mf/.obj link")
    return _register_mesh(raw, name)


def _mesh_note(mesh_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", mesh_id):
        raise HTTPException(400, "bad mesh id")
    meta_file = UPLOADS_DIR / f"{mesh_id}.json"
    if not meta_file.exists():
        raise HTTPException(404, "mesh not found")
    meta = json.loads(meta_file.read_text())
    path = (UPLOADS_DIR / f"{mesh_id}.stl").resolve()
    secs = "".join(
        f"\n  at z={s['z']}: x {s['x'][0]}..{s['x'][1]}, y {s['y'][0]}..{s['y'][1]}"
        for s in meta.get("sections", [])
    )
    return (
        f'BASE MESH provided ("{meta["name"]}"): import("{path}");\n'
        f"Bounding box: min {meta['bounds_min']} to max {meta['bounds_max']} mm (x,y,z).\n"
        f"Horizontal cross-section extents (the body only exists within these at each height):{secs}\n"
        "Place features ONLY where these sections show material at that height, and start "
        "them inside the section extents so they fuse."
    )


@app.post("/generate")
async def generate(req: GenerateRequest):
    images = []
    if req.image:
        try:
            _, b64 = req.image.split(",", 1)
            img_path = WORK_DIR / f"ref-{uuid.uuid4().hex}.png"
            img_path.write_bytes(base64.b64decode(b64))
            images = [str(img_path)]
        except Exception:
            raise HTTPException(400, "bad image data URL")
    # refinements get renders of the current model so the LLM isn't editing blind
    if req.current_scad and LLM_BACKEND == "codex":
        cur = WORK_DIR / f"cur-{uuid.uuid4().hex}.scad"
        cur.write_text(req.current_scad)
        for cam, tag in ((None, "iso"), ("0,0,0,0,0,0,340", "top")):
            p = render_png(cur, cur.with_name(f"{cur.stem}_{tag}.png"), camera=cam)
            if p:
                images.append(str(p))
    mesh_note = _mesh_note(req.mesh_id) if req.mesh_id else None
    if req.current_scad and LLM_BACKEND == "codex":
        # refines edit the existing file in place — reprinting long files wholesale
        # reliably drops unrelated features
        instruction = SYSTEM_PROMPT + "\n\n" + (f"{mesh_note}\n\n" if mesh_note else "") + (
            "The attached images (if any) are renders of the current model and/or a "
            f"user-provided reference photo.\nModification request: {req.prompt}"
        )
        scad = await asyncio.to_thread(call_codex_edit, req.current_scad, instruction, images)
        try:
            stl = render_stl(scad, {})
        except HTTPException as e:
            scad = await asyncio.to_thread(
                call_codex_edit, scad,
                f"The file fails to render with this error:\n{e.detail}\nFix it.")
            stl = render_stl(scad, {})  # second failure propagates to the UI
    else:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt(req.prompt, req.current_scad, mesh_note)},
        ]
        scad = await call_llm(messages, images)
        # validate by rendering; on failure, one automatic retry with the error fed back
        try:
            stl = render_stl(scad, {})
        except HTTPException as e:
            messages += [
                {"role": "assistant", "content": scad},
                {"role": "user", "content": f"That code failed to render:\n{e.detail}\nFix it and return the complete corrected file."},
            ]
            scad = await call_llm(messages, images)
            stl = render_stl(scad, {})  # second failure propagates to the UI

    qa = "skipped"
    if LLM_BACKEND == "codex" and QA_CHECK:
        # diff against the PREVIOUS state for refines, not the pristine upload —
        # otherwise QA re-litigates (and reverts) intentional changes from earlier turns
        base_stl = (UPLOADS_DIR / f"{req.mesh_id}.stl") if req.mesh_id else None
        if req.current_scad:
            try:
                base_stl = render_stl(req.current_scad, {})
            except HTTPException:
                pass  # unrenderable previous state; fall back to the upload
        fixes = 0
        for _ in range(QA_ROUNDS):
            try:
                scad, stl, status = await vision_qa(req.prompt, scad, stl, base_stl)
            except Exception as e:
                print(f"vision QA failed: {e}")
                qa = f"fixed x{fixes}" if fixes else "skipped"
                break
            if status != "fixed":
                qa = f"fixed x{fixes}" if fixes else status
                break
            fixes += 1
        else:
            qa = f"fixed x{fixes}"

    model_id = save_to_library(req.prompt, scad, stl)
    try:
        warnings = len(await asyncio.to_thread(floating_starts, stl))
    except Exception:
        warnings = 0
    return {"scad": scad, "params": parse_params(scad), "stl_id": stl.stem,
            "qa": qa, "model_id": model_id, "print_warnings": warnings}


@app.post("/render")
async def render(req: RenderRequest):
    stl = render_stl(req.scad, req.params)
    return {"stl_id": stl.stem}


def _stl_path(stl_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", stl_id):
        raise HTTPException(400, "bad id")
    path = WORK_DIR / f"{stl_id}.stl"
    if not path.exists():
        raise HTTPException(404, "not found")
    return path


@app.get("/stl/{stl_id}")
async def get_stl(stl_id: str, download: bool = False):
    kwargs = {"filename": "printforge-model.stl"} if download else {}
    return FileResponse(_stl_path(stl_id), media_type="model/stl", **kwargs)


def _build_3mf(stl_id: str) -> Path:
    out = WORK_DIR / f"{stl_id}.3mf"
    write_3mf(split_parts(_stl_path(stl_id)), out)
    return out


@app.get("/export/{stl_id}")
async def export_3mf(stl_id: str):
    return FileResponse(_build_3mf(stl_id), media_type="model/3mf",
                        filename="printforge-model.3mf")


@app.post("/send/{stl_id}")
async def send_to_bambuddy(stl_id: str, name: str = "printforge-model"):
    if not BAMBUDDY_API_KEY:
        raise HTTPException(400, "BAMBUDDY_API_KEY not set")
    out = _build_3mf(stl_id)
    safe = re.sub(r"[^\w\- ]", "", name)[:60] or "printforge-model"
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            f"{BAMBUDDY_URL}/api/v1/archives/upload",
            headers={"Authorization": f"Bearer {BAMBUDDY_API_KEY}"},
            files={"file": (f"{safe}.3mf", out.read_bytes(), "model/3mf")},
        )
    if r.status_code >= 300:
        raise HTTPException(502, f"Bambuddy upload failed ({r.status_code}): {r.text[:300]}")
    return r.json()


@app.get("/config")
async def config():
    return {"bambuddy": bool(BAMBUDDY_API_KEY)}


def _model_dir(model_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", model_id):
        raise HTTPException(400, "bad id")
    mdir = LIB_DIR / model_id
    if not mdir.exists():
        raise HTTPException(404, "not found")
    return mdir


@app.get("/models")
async def list_models():
    out = []
    for mdir in LIB_DIR.iterdir():
        meta = mdir / "meta.json"
        if meta.exists():
            out.append(json.loads(meta.read_text()))
    return sorted(out, key=lambda m: -m["created"])


@app.get("/models/{model_id}")
async def get_model(model_id: str):
    mdir = _model_dir(model_id)
    scad = (mdir / "model.scad").read_text()
    stl = render_stl(scad, {})
    return {"meta": json.loads((mdir / "meta.json").read_text()),
            "scad": scad, "params": parse_params(scad), "stl_id": stl.stem}


@app.get("/models/{model_id}/thumb")
async def model_thumb(model_id: str):
    thumb = _model_dir(model_id) / "thumb.png"
    if not thumb.exists():
        raise HTTPException(404, "no thumbnail")
    return FileResponse(thumb, media_type="image/png")


@app.patch("/models/{model_id}")
async def rename_model(model_id: str, req: RenameRequest):
    mdir = _model_dir(model_id)
    meta = json.loads((mdir / "meta.json").read_text())
    meta["name"] = req.name.strip()[:60]
    (mdir / "meta.json").write_text(json.dumps(meta))
    return meta


@app.delete("/models/{model_id}")
async def delete_model(model_id: str):
    shutil.rmtree(_model_dir(model_id))
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))
