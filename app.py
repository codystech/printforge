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
from prompts import SYSTEM_PROMPT, archetype_notes, qa_prompt, spec_prompt, user_prompt

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
    mesh_id: str | None = None  # uploaded base mesh to remix (legacy single)
    mesh_ids: list[str] | None = None  # multiple base meshes to integrate
    parent_id: str | None = None  # library model this refine builds on (intent lineage)


class RenderRequest(BaseModel):
    scad: str
    params: dict[str, float | str] = {}


class RenameRequest(BaseModel):
    name: str


class SpecRequest(BaseModel):
    prompt: str
    mesh_id: str | None = None
    mesh_ids: list[str] | None = None


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


def call_codex_edit(scad: str, instruction: str, images: list[str] | None = None,
                    effort: str | None = None) -> str:
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
    if effort:  # reviews don't need high reasoning; halves QA latency
        cmd += ["-c", f'model_reasoning_effort="{effort}"']
    for img in images or []:
        cmd += ["-i", img]
    cmd.append("-")
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        print(f"codex edit stderr:\n{proc.stderr[-4000:]}")  # full-ish tail to server log
        raise RuntimeError(f"codex edit failed (rc {proc.returncode}): {proc.stderr[-1500:]}")
    return f.read_text()


LAST_BACKEND = "none"  # which model produced the most recent generation (single-user app)


async def call_llm(messages: list[dict], images: list[str] | None = None) -> str:
    global LAST_BACKEND
    if LLM_BACKEND == "codex":
        try:
            result = await asyncio.to_thread(call_codex, messages, images)
            LAST_BACKEND = "codex/gpt-5.5"
            return result
        except Exception as e:
            if images:
                raise HTTPException(502, f"codex backend failed and image input needs codex: {e}")
            print(f"codex backend failed ({e}); falling back to {LLM_MODEL}")
    LAST_BACKEND = f"local/{LLM_MODEL}"
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


def print_report(stl_path: Path) -> dict:
    """Bounding box, weight estimate, connectivity — the numbers a maker checks first."""
    m = trimesh.load_mesh(stl_path)
    vol_cm3 = float(abs(m.volume)) / 1000.0 if m.is_volume else None
    return {
        "bbox_mm": [round(float(v), 1) for v in m.extents],
        "watertight": bool(m.is_watertight),
        "parts": len(m.split(only_watertight=False)),
        "est_grams_pla": round(vol_cm3 * 1.24, 1) if vol_cm3 else None,
    }


PRESETS_FILE = Path(__file__).parent / "presets.txt"
DEFAULT_PRESETS = """# printing assumptions (edit to match your printer/results)
nozzle = 0.4, layer height = 0.2
slip fit clearance = 0.2, loose fit = 0.4
minimum wall = 2.0
raised text depth = 1.2, engraved text depth = 0.8
# add measured objects you design around, e.g.:
# phone width = 78 (with case), phone thickness = 12
# desk edge thickness = 25"""


def presets_block() -> str:
    text = PRESETS_FILE.read_text().strip() if PRESETS_FILE.exists() else DEFAULT_PRESETS
    if not text:
        return ""
    return ("\nUSER DEFAULTS — measured values and printing assumptions for this user. "
            "Use an entry ONLY when the request involves that object or setting; "
            "entries irrelevant to the request must be completely ignored and never "
            "mentioned. The request always overrides these:\n" + text + "\n")


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
                    base_stl: Path | None = None,
                    intent: list[str] | None = None,
                    rules: list[str] | None = None) -> tuple[str, Path, str]:
    """One round of render-and-review; returns (scad, stl, qa_status)."""
    # oblique perspective views: straight-on orthographic hides low relief entirely
    scad_path = WORK_DIR / f"{stl.stem}.scad"
    views = []
    qa_notes = []
    if intent:
        qa_notes.append(intent_block(intent))
    if rules:
        qa_notes.append(rules_block(rules))
    # per-cluster close-ups of geometry that differs from the base mesh — whole-model
    # renders make a 10mm addition a smudge the reviewer cannot judge
    if base_stl and base_stl.exists():
        try:
            regions, removed = await asyncio.to_thread(mesh_changes, base_stl, stl)
        except Exception:
            regions, removed = [], None
        region_descs = []
        for i, (mn, mx) in enumerate(regions[:2]):
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
    views.append(render_png(scad_path, WORK_DIR / f"{stl.stem}_ob1.png",
                            camera="0,0,0,70,0,25,340", ortho=False, imgsize="1000,750"))
    if not any(views[:-2]):  # no closeups: add the wider set back
        views.append(render_png(scad_path, WORK_DIR / f"{stl.stem}_top.png", camera="0,0,0,0,0,0,340"))
        views.append(render_png(scad_path, WORK_DIR / f"{stl.stem}_ob2.png",
                                camera="0,0,0,70,0,205,340", ortho=False, imgsize="1000,750"))
    images = [str(p) for p in views if p]
    if not images:
        return scad, stl, "skipped"
    edited = await asyncio.to_thread(
        call_codex_edit, scad,
        qa_prompt(prompt, "(the code is model.scad in this directory)", qa_notes), images,
        "medium",
    )
    if edited.strip() == scad.strip():
        return scad, stl, "passed"
    try:
        fixed_stl = render_stl(edited, {})
    except HTTPException:
        return scad, stl, "passed"  # ponytail: fix didn't render, ship the original
    return edited, fixed_stl, "fixed"


def _parent_meta(parent_id: str | None) -> dict:
    if not parent_id or not re.fullmatch(r"[0-9a-f]{12}", parent_id):
        return {}
    meta_file = LIB_DIR / parent_id / "meta.json"
    return json.loads(meta_file.read_text()) if meta_file.exists() else {}


def load_intent(parent_id: str | None) -> list[str]:
    """Accepted design decisions inherited from the parent model's lineage."""
    return _parent_meta(parent_id).get("intent", [])


def load_rules(parent_id: str | None) -> list[str]:
    """User-authored hard constraints inherited from the parent model."""
    return _parent_meta(parent_id).get("rules", [])


def load_part_state(parent_id: str | None) -> dict:
    """Per-part lock/suppress/alias settings inherited from the parent model."""
    return _parent_meta(parent_id).get("part_state", {})


def part_state_block(ps: dict) -> str:
    locked = [p for p, s in ps.items() if s.get("locked")]
    suppressed = [p for p, s in ps.items() if s.get("suppressed")]
    out = ""
    if locked:
        out += ("\nLOCKED PARTS: " + ", ".join(locked) + " — their modules, parameters "
                "and placement are FROZEN. Do not modify, move, resize or restyle them "
                "in any way; their code must remain byte-identical.\n")
    if suppressed:
        out += ("\nSUPPRESSED PARTS: " + ", ".join(suppressed) + " — the user removed "
                "these. Keep their *_enabled defaults at 0, never re-enable them, and "
                "do not recreate equivalent geometry under a new name.\n")
    return out


def module_block(scad: str, name: str) -> str | None:
    """Extract the full text of `module <name>...{...}` by brace counting."""
    m = re.search(rf"module\s+{re.escape(name)}\s*\(", scad)
    if not m:
        return None
    i = scad.find("{", m.start())
    if i < 0:
        return None
    depth = 0
    for j in range(i, len(scad)):
        if scad[j] == "{":
            depth += 1
        elif scad[j] == "}":
            depth -= 1
            if depth == 0:
                return scad[m.start():j + 1]
    return None


def lock_violations(old_scad: str, new_scad: str, ps: dict) -> list[str]:
    out = []
    for part, s in ps.items():
        if not s.get("locked"):
            continue
        for candidate in (part, f"{part}_module"):
            before = module_block(old_scad, candidate)
            if before is not None:
                after = module_block(new_scad, candidate)
                if after is None or " ".join(before.split()) != " ".join(after.split()):
                    out.append(part)
                break
    return out


def rules_block(rules: list[str]) -> str:
    if not rules:
        return ""
    lines = "\n- ".join(rules)
    return (
        "\nPROJECT RULES — hard constraints the user set for this project. These are "
        f"design law, not suggestions; every edit must satisfy all of them:\n- {lines}\n"
    )


def save_to_library(prompt: str, scad: str, stl: Path, intent: list[str],
                    parent_id: str | None = None) -> str:
    model_id = uuid.uuid4().hex[:12]
    mdir = LIB_DIR / model_id
    mdir.mkdir()
    (mdir / "model.scad").write_text(scad)
    (mdir / "meta.json").write_text(json.dumps({
        "id": model_id,
        "name": prompt.strip()[:60],
        "prompt": prompt,
        "intent": intent[-12:],
        "rules": load_rules(parent_id),
        "parent": parent_id,
        "created": time.time(),
    }))
    render_png(WORK_DIR / f"{stl.stem}.scad", mdir / "thumb.png", imgsize="400,300")
    return model_id


def intent_block(intent: list[str]) -> str:
    if not intent:
        return ""
    lines = "\n- ".join(intent)
    return (
        "\nDESIGN HISTORY — changes the user already made and accepted. Every one of "
        "them MUST still hold after your edit unless the new request explicitly undoes "
        f"it. Never revert these to satisfy another goal:\n- {lines}\n"
    )


def _register_svg(raw: bytes, filename: str) -> dict:
    mesh_id = uuid.uuid4().hex[:12]
    path = UPLOADS_DIR / f"{mesh_id}.svg"
    path.write_bytes(raw)
    import xml.etree.ElementTree as ET
    size = None
    try:
        root = ET.fromstring(raw)
        vb = root.get("viewBox")
        if vb:
            _, _, w, h = (float(v) for v in vb.replace(",", " ").split())
            size = [round(w, 1), round(h, 1)]
        elif root.get("width") and root.get("height"):
            size = [round(float(re.sub(r"[a-z%]+$", "", root.get(a))), 1)
                    for a in ("width", "height")]
    except Exception:
        pass
    meta = {"id": mesh_id, "name": filename, "kind": "svg", "size": size}
    (UPLOADS_DIR / f"{mesh_id}.json").write_text(json.dumps(meta))
    return meta


def _trace_to_svg(raw: bytes, filename: str) -> bytes:
    """Bitmap logo → SVG silhouette via imagemagick + potrace."""
    job = WORK_DIR / f"trace-{uuid.uuid4().hex}"
    src, pgm, svg = job.with_suffix(Path(filename).suffix or ".png"), job.with_suffix(".pgm"), job.with_suffix(".svg")
    src.write_bytes(raw)
    subprocess.run(["magick", str(src), "-colorspace", "gray", "-resize", "1000x1000>",
                    "-threshold", "50%", str(pgm)], check=True, capture_output=True, timeout=60)
    subprocess.run(["nix", "shell", "nixpkgs#potrace", "--command",
                    "potrace", "-s", "-o", str(svg), str(pgm)],
                   check=True, capture_output=True, timeout=120)
    return svg.read_bytes()


MESH_EXTS = {".stl", ".3mf", ".obj", ".glb", ".gltf", ".step", ".stp"}


def _register_mesh(raw: bytes, filename: str) -> dict:
    ext = Path(filename or "m.stl").suffix.lower()
    if ext == ".svg":
        return _register_svg(raw, filename)
    if ext not in MESH_EXTS:
        raise HTTPException(415, f"unsupported format '{ext}' — use STL, 3MF, OBJ, "
                                 "GLB/GLTF, STEP/STP, or SVG outlines")
    if len(raw) > 100_000_000:
        raise HTTPException(413, "mesh too large (100MB cap)")
    if ext in {".step", ".stp"}:
        try:
            import cascadio  # noqa: F401 — feature detection for the CAD kernel
        except ImportError:
            raise HTTPException(415, "STEP import requires a CAD conversion backend — "
                                     "add '--with cascadio' to run.sh deps and restart")
    mesh_id = uuid.uuid4().hex[:12]
    src = UPLOADS_DIR / f"{mesh_id}{ext}"
    src.write_bytes(raw)
    try:
        m = trimesh.load(src, force="mesh")
        if ext in {".step", ".stp"}:
            m.apply_scale(1000)  # cascadio emits GLB meters; STEP dimensions are mm
    except Exception as e:
        src.unlink(missing_ok=True)
        raise HTTPException(422, f"could not read {ext} file: {e}")
    stl_path = UPLOADS_DIR / f"{mesh_id}.stl"
    m.export(stl_path)
    # keep the original only for STEP: it's true CAD source worth preserving
    if src != stl_path and ext not in {".step", ".stp"}:
        src.unlink()
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
    maxdim = float(max(m.extents))
    warning = None
    if maxdim < 5:
        warning = (f"model is only {maxdim:.1f}mm across — units may be inches; "
                   "if so, ask a refine to scale it by 25.4")
    elif maxdim > 400:
        warning = f"model is {maxdim:.0f}mm across — bigger than the bed; check units/scale"
    meta = {"id": mesh_id, "name": filename, "format": ext.lstrip("."),
            "role": "printable",
            "bounds_min": [round(v, 1) for v in m.bounds[0]],
            "bounds_max": [round(v, 1) for v in m.bounds[1]],
            "tris": int(len(m.faces)), "watertight": bool(m.is_watertight),
            "warning": warning,
            "sections": sections}
    (UPLOADS_DIR / f"{mesh_id}.json").write_text(json.dumps(meta))
    return meta


@app.post("/upload-mesh")
async def upload_mesh(file: UploadFile = File(...), trace: bool = False):
    raw = await file.read()
    name = file.filename or "m.stl"
    if trace:
        try:
            raw = await asyncio.to_thread(_trace_to_svg, raw, name)
            name = Path(name).stem + ".svg"
        except subprocess.CalledProcessError as e:
            raise HTTPException(422, f"tracing failed: {(e.stderr or b'')[-300:].decode(errors='replace')}")
    return _register_mesh(raw, name)


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


def _mesh_ids(req) -> list[str]:
    return req.mesh_ids or ([req.mesh_id] if req.mesh_id else [])


def _all_mesh_notes(ids: list[str]) -> str | None:
    if not ids:
        return None
    notes = [f"[MESH {chr(65 + i)}] {_mesh_note(mid)}" for i, mid in enumerate(ids[:3])]
    if len(notes) > 1:
        notes.append(
            "MULTIPLE base meshes: position them relative to each other exactly as the "
            "request describes (e.g. a board mounted inside a case). Translate/rotate "
            "each import into place, then ADD the connecting geometry — standoffs, "
            "screw bosses, brackets — sized from the meshes' real dimensions above and "
            "fused into the part that carries them. For well-known hardware (Raspberry "
            "Pi, Arduino, SSDs) use their standard mounting-hole layouts from your "
            "knowledge. Keep each import and all connectors parameterized."
        )
    return "\n\n".join(notes)


def _mesh_note(mesh_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{12}", mesh_id):
        raise HTTPException(400, "bad mesh id")
    meta_file = UPLOADS_DIR / f"{mesh_id}.json"
    if not meta_file.exists():
        raise HTTPException(404, "mesh not found")
    meta = json.loads(meta_file.read_text())
    if meta.get("kind") == "svg":
        path = (UPLOADS_DIR / f"{mesh_id}.svg").resolve()
        size = meta.get("size")
        size_note = f" Its natural size is {size[0]}x{size[1]} units." if size else ""
        return (
            f'2D SVG OUTLINE provided ("{meta["name"]}"): use it as real curved geometry '
            f'via linear_extrude(height) import("{path}", center=true);{size_note} '
            "Wrap it in resize([target_w, target_h, 0], auto=true) to hit requested "
            "dimensions, keep extrusion height and scale as customizer parameters, and "
            "build any mounts/bosses relative to the extrusion."
        )
    path = (UPLOADS_DIR / f"{mesh_id}.stl").resolve()
    role = meta.get("role", "printable")
    role_note = {
        "reference": "\nROLE = REFERENCE ONLY: derive fits, clearances, cutout positions "
                     "and mounting geometry from this object, but its geometry must NOT "
                     "appear in the printable output. It may be shown only under "
                     "`if (assembled_preview > 0.5)` with its own *_enabled toggle "
                     "defaulting to 0.",
        "negative": "\nROLE = NEGATIVE SPACE: subtract this object's envelope plus the "
                    "requested clearance from surrounding printed parts. Never include "
                    "it as printed geometry.",
    }.get(role, "")
    info_note = (f"\n(format: {meta.get('format', 'stl')}, {meta.get('tris', '?')} "
                 f"triangles, watertight: {meta.get('watertight')})")
    secs = "".join(
        f"\n  at z={s['z']}: x {s['x'][0]}..{s['x'][1]}, y {s['y'][0]}..{s['y'][1]}"
        for s in meta.get("sections", [])
    )
    return (
        f'BASE MESH provided ("{meta["name"]}"): import("{path}");{info_note}\n'
        f"Bounding box: min {meta['bounds_min']} to max {meta['bounds_max']} mm (x,y,z).\n"
        f"Horizontal cross-section extents (the body only exists within these at each height):{secs}\n"
        "Place features ONLY where these sections show material at that height, and start "
        "them inside the section extents so they fuse." + role_note
    )


@app.post("/spec")
async def spec(req: SpecRequest):
    mesh_note = _all_mesh_notes(_mesh_ids(req))
    text = await call_llm([{"role": "user",
                            "content": spec_prompt(req.prompt, mesh_note) + presets_block()}])
    return {"spec": text}


CALIBRATION_SCAD = """// PrintForge tolerance calibration coupon
// Print flat, no supports. Test each peg in each hole; the clearance that slides
// in snugly without force is YOUR slip-fit number - record it in My presets.
hole_d = 5.0; // [4:0.5:8]
peg_h = 8; // [6:1:12]
$fn = 48;
clearances = [0.1, 0.15, 0.2, 0.3, 0.4];
// --- model ---
plate_w = 16 * len(clearances) + 8;
difference() {
    cube([plate_w, 26, 4]);
    for (i = [0:len(clearances)-1])
        translate([12 + i*16, 16, -0.1]) cylinder(d=hole_d, h=4.2);
}
for (i = [0:len(clearances)-1]) {
    translate([12 + i*16, 6, 4]) linear_extrude(0.6)
        text(str(clearances[i]), size=3.5, halign="center", valign="center");
    // pegs on their own bases, in front of the plate
    translate([12 + i*16, -12, 0]) {
        cylinder(d=10, h=2);
        cylinder(d=hole_d - clearances[i], h=peg_h);
        translate([0, -8, 0]) linear_extrude(0.6)
            text(str(clearances[i]), size=3.5, halign="center", valign="center");
    }
}
"""


@app.get("/calibration")
async def calibration():
    stl = await asyncio.to_thread(render_stl, CALIBRATION_SCAD, {})
    return {"scad": CALIBRATION_SCAD, "params": parse_params(CALIBRATION_SCAD),
            "stl_id": stl.stem}


@app.get("/models/{model_id}/diff")
async def model_diff(model_id: str):
    mdir = _model_dir(model_id)
    meta = json.loads((mdir / "meta.json").read_text())
    parent = meta.get("parent")
    if not parent or not (LIB_DIR / parent / "meta.json").exists():
        raise HTTPException(404, "no parent version")
    pmeta = json.loads((LIB_DIR / parent / "meta.json").read_text())

    def defaults(mid):
        return {p["name"]: p["value"]
                for p in parse_params((LIB_DIR / mid / "model.scad").read_text())}

    a, b = defaults(parent), defaults(model_id)
    return {"parent_name": pmeta.get("name", "previous"),
            "qa": [pmeta.get("qa"), meta.get("qa")],
            "report": [pmeta.get("report") or {}, meta.get("report") or {}],
            "params_changed": [f"{k}: {a[k]} → {b[k]}" for k in a if k in b and a[k] != b[k]][:12],
            "params_added": [k for k in b if k not in a][:12],
            "params_removed": [k for k in a if k not in b][:12]}


@app.get("/presets")
async def get_presets():
    return {"text": PRESETS_FILE.read_text() if PRESETS_FILE.exists() else DEFAULT_PRESETS}


class PresetsRequest(BaseModel):
    text: str


@app.put("/presets")
async def set_presets(req: PresetsRequest):
    PRESETS_FILE.write_text(req.text[:4000])
    return {"ok": True}


async def _autoname(model_id: str, prompt: str):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json={"model": LLM_MODEL, "temperature": 0.4, "messages": [{
                    "role": "user",
                    "content": "Reply with ONLY a short 2-4 word name for this 3D print "
                               f"design, nothing else: {prompt[:300]}"}]},
            )
        name = r.json()["choices"][0]["message"]["content"].strip().strip('"').splitlines()[0][:40]
        meta_file = LIB_DIR / model_id / "meta.json"
        if name and meta_file.exists():
            meta = json.loads(meta_file.read_text())
            meta["name"] = name
            meta_file.write_text(json.dumps(meta))
    except Exception:
        pass  # naming is best-effort garnish


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
    mesh_note = _all_mesh_notes(_mesh_ids(req))
    lock_issues: list[str] = []
    if req.current_scad and LLM_BACKEND == "codex":
        # refines edit the existing file in place — reprinting long files wholesale
        # reliably drops unrelated features
        float_note = ""
        try:
            prev_stl = render_stl(req.current_scad, {})
            floats = await asyncio.to_thread(floating_starts, prev_stl)
            if floats:
                desc = "; ".join(f"z={f['z']} at ({f['x']},{f['y']})" for f in floats[:10])
                float_note = (
                    f"\nKnown printability problems in the CURRENT model — features that "
                    f"begin in mid-air when sliced bottom-up: {desc}. While applying the "
                    "request, also seat these (embed 2-3mm downward into whatever is below, "
                    "or give a >=45-degree self-supporting underside). Ball-on-post tips "
                    "are fine."
                )
        except Exception:
            pass
        instruction = SYSTEM_PROMPT + archetype_notes(req.prompt) + presets_block() + "\n\n" + (f"{mesh_note}\n\n" if mesh_note else "") + (
            intent_block(load_intent(req.parent_id)) +
            rules_block(load_rules(req.parent_id)) +
            part_state_block(load_part_state(req.parent_id)) +
            "The attached images (if any) are renders of the current model and/or a "
            f"user-provided reference photo.\nModification request: {req.prompt}{float_note}"
        )
        global LAST_BACKEND
        scad = await asyncio.to_thread(call_codex_edit, req.current_scad, instruction, images)
        LAST_BACKEND = "codex/gpt-5.5 (edit)"
        # deterministic lock enforcement: diff the locked modules, force a fix if touched
        ps = load_part_state(req.parent_id)
        broken = lock_violations(req.current_scad, scad, ps)
        if broken:
            scad = await asyncio.to_thread(
                call_codex_edit, scad,
                "You modified LOCKED parts that must remain byte-identical: "
                + ", ".join(broken) + ". Restore their module code exactly as in this "
                "original version, and re-apply your change without touching them:\n\n"
                + req.current_scad)
            broken = lock_violations(req.current_scad, scad, ps)
        lock_issues = broken
        try:
            stl = render_stl(scad, {})
        except HTTPException as e:
            scad = await asyncio.to_thread(
                call_codex_edit, scad,
                f"The file fails to render with this error:\n{e.detail}\nFix it.")
            stl = render_stl(scad, {})  # second failure propagates to the UI
    else:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + archetype_notes(req.prompt)
                                          + presets_block() + _taste_example(req.prompt)},
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
        first_mesh = next(iter(_mesh_ids(req)), None)
        base_stl = (UPLOADS_DIR / f"{first_mesh}.stl") if first_mesh else None
        if req.current_scad:
            try:
                base_stl = render_stl(req.current_scad, {})
            except HTTPException:
                pass  # unrenderable previous state; fall back to the upload
        fixes = 0
        for _ in range(QA_ROUNDS):
            try:
                ps_note = part_state_block(load_part_state(req.parent_id))
                scad, stl, status = await vision_qa(
                    req.prompt, scad, stl, base_stl,
                    load_intent(req.parent_id) + ([ps_note] if ps_note else []),
                    load_rules(req.parent_id))
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

    model_id = save_to_library(req.prompt, scad, stl,
                               load_intent(req.parent_id) + [req.prompt], req.parent_id)
    try:
        report_stl, measured = stl, "print layout"
        if any(p["name"] == "assembled_preview" for p in parse_params(scad)):
            try:
                report_stl = await asyncio.to_thread(render_stl, scad, {"assembled_preview": 1})
                measured = "assembled"
            except HTTPException:
                pass
        report = await asyncio.to_thread(print_report, report_stl)
        report["measured"] = measured
    except Exception:
        report = {}
    meta_file = LIB_DIR / model_id / "meta.json"
    meta = json.loads(meta_file.read_text())
    meta.update({"qa": qa, "backend": LAST_BACKEND, "report": report})
    if load_part_state(req.parent_id):
        meta["part_state"] = load_part_state(req.parent_id)
    meta_file.write_text(json.dumps(meta))
    asyncio.create_task(_autoname(model_id, req.prompt))
    try:
        floats_list = await asyncio.to_thread(floating_starts, stl)
    except Exception:
        floats_list = []
    warnings = len(floats_list)
    warning_details = [f"a feature starts in mid-air at z={f['z']}mm near x={f['x']}, y={f['y']}"
                       for f in floats_list[:6]]
    return {"scad": scad, "params": parse_params(scad), "stl_id": stl.stem,
            "qa": qa, "model_id": model_id, "print_warnings": warnings,
            "backend": LAST_BACKEND, "rules": load_rules(req.parent_id),
            "part_state": load_part_state(req.parent_id),
            "lock_violations": lock_issues, "report": report,
            "print_warning_details": warning_details}


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
async def export_model(stl_id: str, fmt: str = "3mf"):
    if fmt == "3mf":
        return FileResponse(_build_3mf(stl_id), media_type="model/3mf",
                            filename="printforge-model.3mf")
    if fmt in {"obj", "glb"}:
        out = WORK_DIR / f"{stl_id}.{fmt}"
        trimesh.load_mesh(_stl_path(stl_id)).export(out)
        return FileResponse(out, filename=f"printforge-model.{fmt}")
    if fmt == "step":
        raise HTTPException(400, "STEP export requires CAD/BRep output; this model is "
                                 "mesh-only. Use STL, 3MF, OBJ or GLB.")
    raise HTTPException(400, f"unknown export format '{fmt}' (3mf, obj, glb)")


class MeshRoleRequest(BaseModel):
    role: str


@app.patch("/uploads/{mesh_id}")
async def set_mesh_role(mesh_id: str, req: MeshRoleRequest):
    if req.role not in {"printable", "reference", "negative"}:
        raise HTTPException(400, "role must be printable, reference or negative")
    if not re.fullmatch(r"[0-9a-f]{12}", mesh_id):
        raise HTTPException(400, "bad id")
    meta_file = UPLOADS_DIR / f"{mesh_id}.json"
    if not meta_file.exists():
        raise HTTPException(404, "mesh not found")
    meta = json.loads(meta_file.read_text())
    meta["role"] = req.role
    meta_file.write_text(json.dumps(meta))
    return meta


@app.get("/models/{model_id}/zip")
async def model_zip(model_id: str):
    import zipfile
    mdir = _model_dir(model_id)
    out = WORK_DIR / f"{model_id}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in mdir.iterdir():
            z.write(f, f.name)
    return FileResponse(out, filename=f"printforge-{model_id}.zip")


@app.post("/models/{model_id}/duplicate")
async def model_duplicate(model_id: str):
    mdir = _model_dir(model_id)
    new_id = uuid.uuid4().hex[:12]
    shutil.copytree(mdir, LIB_DIR / new_id)
    meta_file = LIB_DIR / new_id / "meta.json"
    meta = json.loads(meta_file.read_text())
    meta["id"] = new_id
    meta["name"] = (meta.get("name", "model") + " (copy)")[:60]
    meta["created"] = time.time()
    meta_file.write_text(json.dumps(meta))
    return meta


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


ORGANIC_DIR = Path(__file__).parent / "organic"
_organic_lock = asyncio.Lock()


def _organic_ready() -> bool:
    return (ORGANIC_DIR / ".venv/bin/python").exists()


class OrganicRequest(BaseModel):
    image: str  # data URL
    target_mm: float = 80


async def _free_gpu():
    """Ask ollama to unload whatever it has loaded; the brain reloads lazily later."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            ps = (await client.get("http://127.0.0.1:11434/api/ps")).json()
            for mdl in ps.get("models", []):
                await client.post("http://127.0.0.1:11434/api/generate",
                                  json={"model": mdl["name"], "keep_alive": 0})
    except Exception:
        pass


@app.post("/organic")
async def organic(req: OrganicRequest):
    if not _organic_ready():
        raise HTTPException(503, "organic mode not installed — run organic/setup.sh once")
    try:
        _, b64 = req.image.split(",", 1)
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(400, "bad image data URL")
    img = WORK_DIR / f"org-{uuid.uuid4().hex}.png"
    img.write_bytes(raw)
    out = img.with_suffix(".stl")
    async with _organic_lock:  # never share the 3090 between two generations
        await _free_gpu()
        env = {**os.environ,
               "LD_LIBRARY_PATH": os.environ.get("ORGANIC_LIBS", "/run/opengl-driver/lib")}
        proc = await asyncio.to_thread(
            subprocess.run,
            [str(ORGANIC_DIR / ".venv/bin/python"), str(ORGANIC_DIR / "generate.py"),
             "--image", str(img), "--out", str(out),
             "--target-mm", str(max(10.0, min(250.0, req.target_mm)))],
            capture_output=True, text=True, timeout=720, env=env)
    if proc.returncode != 0 or not out.exists():
        raise HTTPException(502, f"organic generation failed:\n{(proc.stderr or '')[-800:]}")
    meta = _register_mesh(out.read_bytes(), "organic.stl")
    # instantly viewable/exportable — no LLM round-trip needed to see the sculpt
    stl_id = uuid.uuid4().hex
    shutil.copy(UPLOADS_DIR / f"{meta['id']}.stl", WORK_DIR / f"{stl_id}.stl")
    wrapper = (f'base_mesh_path = "{(UPLOADS_DIR / (meta["id"] + ".stl")).resolve()}"; // free text\n'
               "// --- model ---\nimport(base_mesh_path, convexity=10);\n")
    meta.update({"stl_id": stl_id, "scad": wrapper, "params": parse_params(wrapper)})
    return meta


@app.get("/config")
async def config():
    return {"bambuddy": bool(BAMBUDDY_API_KEY), "organic": _organic_ready()}


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


class RateRequest(BaseModel):
    rating: int  # 1 thumbs-up, -1 thumbs-down, 0 clear


@app.post("/models/{model_id}/rate")
async def rate_model(model_id: str, req: RateRequest):
    mdir = _model_dir(model_id)
    meta = json.loads((mdir / "meta.json").read_text())
    meta["rating"] = max(-1, min(1, req.rating))
    (mdir / "meta.json").write_text(json.dumps(meta))
    return meta


def _taste_example(prompt: str) -> str:
    """SCAD of the best-matching thumbs-up model — the user's own taste as a few-shot."""
    words = set(re.findall(r"[a-z]{3,}", prompt.lower()))
    best, best_score = None, 1  # require >=2 overlapping words
    for mdir in LIB_DIR.iterdir():
        mf = mdir / "meta.json"
        if not mf.exists():
            continue
        meta = json.loads(mf.read_text())
        if meta.get("rating", 0) <= 0:
            continue
        mwords = set(re.findall(r"[a-z]{3,}",
                                f"{meta.get('prompt', '')} {meta.get('name', '')}".lower()))
        score = len(words & mwords)
        if score > best_score:
            best, best_score = mdir, score
    if not best:
        return ""
    scad = (best / "model.scad").read_text()
    if len(scad) > 8000:
        return ""
    return ("\n\nUSER-APPROVED EXAMPLE — the user rated this result highly for a similar "
            "request; match its conventions, printability choices and quality bar:\n"
            + scad)


@app.patch("/models/{model_id}")
async def rename_model(model_id: str, req: RenameRequest):
    mdir = _model_dir(model_id)
    meta = json.loads((mdir / "meta.json").read_text())
    meta["name"] = req.name.strip()[:60]
    (mdir / "meta.json").write_text(json.dumps(meta))
    return meta


class RulesRequest(BaseModel):
    rules: list[str]


@app.put("/models/{model_id}/rules")
async def set_rules(model_id: str, req: RulesRequest):
    mdir = _model_dir(model_id)
    meta = json.loads((mdir / "meta.json").read_text())
    meta["rules"] = [r.strip() for r in req.rules if r.strip()][:20]
    (mdir / "meta.json").write_text(json.dumps(meta))
    return meta


class PartStateRequest(BaseModel):
    part_state: dict[str, dict]


@app.put("/models/{model_id}/parts")
async def set_part_state(model_id: str, req: PartStateRequest):
    mdir = _model_dir(model_id)
    meta = json.loads((mdir / "meta.json").read_text())
    clean = {}
    for part, s in list(req.part_state.items())[:30]:
        if re.fullmatch(r"\w+", part):
            clean[part] = {"locked": bool(s.get("locked")),
                           "suppressed": bool(s.get("suppressed")),
                           "alias": str(s.get("alias", ""))[:40]}
    meta["part_state"] = clean
    (mdir / "meta.json").write_text(json.dumps(meta))
    return meta


class ValidateRequest(BaseModel):
    scad: str
    params: dict[str, float | str] = {}


@app.post("/validate")
async def validate(req: ValidateRequest):
    """Render each *_enabled part in its assembled position; check collisions/clearance."""
    from itertools import combinations
    all_params = parse_params(req.scad)
    toggles = [p["name"] for p in all_params if p["name"].endswith("_enabled")]
    if not toggles:
        raise HTTPException(400, "this model has no <part>_enabled toggles to validate "
                                 "(older models predate assembly discipline — regenerate "
                                 "or refine once to gain them)")
    has_asm = any(p["name"] == "assembled_preview" for p in all_params)
    meshes = {}
    for name in toggles:
        overrides = dict(req.params)
        overrides.update({t: 0 for t in toggles})
        overrides[name] = 1
        if has_asm:
            overrides["assembled_preview"] = 1
        try:
            stl = await asyncio.to_thread(render_stl, req.scad, overrides)
            meshes[name.removesuffix("_enabled")] = trimesh.load_mesh(stl)
        except HTTPException:
            pass  # a part can legitimately render empty when others are disabled
    issues = []
    for (na, ma), (nb, mb) in combinations(meshes.items(), 2):
        # bbox prefilter with 5mm margin
        if any(ma.bounds[1][i] + 5 < mb.bounds[0][i] or mb.bounds[1][i] + 5 < ma.bounds[0][i]
               for i in range(3)):
            continue
        try:
            inter = trimesh.boolean.intersection([ma, mb], engine="manifold")
            vol = float(inter.volume) if inter is not None and not inter.is_empty else 0.0
        except Exception:
            vol = 0.0
        if vol > 0.5:
            issues.append(f"COLLISION: {na} and {nb} overlap by ~{vol:.0f}mm³")
        else:
            pts, dist, _ = trimesh.proximity.closest_point(mb, ma.sample(200))
            gap = float(dist.min())
            if gap < 0.15:
                issues.append(f"TOUCHING: {na} and {nb} (gap {gap:.2f}mm) — intentional joint or fused mistake?")
            elif gap < 0.4:
                issues.append(f"TIGHT FIT: {na} vs {nb} gap {gap:.2f}mm — below 0.4mm prints fused")
    return {"parts": list(meshes), "assembled_check": has_asm, "issues": issues}


@app.delete("/models/{model_id}")
async def delete_model(model_id: str):
    shutil.rmtree(_model_dir(model_id))
    return {"ok": True}


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))
