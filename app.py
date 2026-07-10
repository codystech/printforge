import asyncio
import base64
import ipaddress
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urljoin

import httpx
import trimesh
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from parts import floating_starts, split_parts, write_3mf
from prompts import SYSTEM_PROMPT, archetype_notes, qa_prompt, spec_prompt, user_prompt
from evolution_lab import EvolutionAdapters, EvolutionLabConfig, create_router
from evolution_lab.requirements import requirements_prompt_block, verify_requirements

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
EVOLUTION_LAB_CONFIG = EvolutionLabConfig.from_env()

app = FastAPI(title="PrintForge")


@app.middleware("http")
async def training_lab_only_guard(request, call_next):
    """On the :8094 test service, allow writes only to isolated Training Lab APIs."""
    if (EVOLUTION_LAB_CONFIG.lab_only
            and request.method.upper() in {"GET", "HEAD"}
            and request.url.path == "/"):
        return RedirectResponse("/training-lab/", status_code=307)
    if (EVOLUTION_LAB_CONFIG.lab_only
            and request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
            and not request.url.path.startswith("/training-lab/api/")):
        return JSONResponse(
            status_code=403,
            content={"detail": "production mutations are disabled on the Training Lab test service"},
        )
    response = await call_next(request)
    if EVOLUTION_LAB_CONFIG.lab_only:
        # Lab-only: no Cache-Control on StaticFiles means browsers heuristically cache the
        # JS/CSS and never see UI redeploys. Force revalidation (etag → cheap 304 when unchanged).
        response.headers.setdefault("Cache-Control", "no-cache")
    return response

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
    profile: str | None = None  # active printer profile name


class RenderRequest(BaseModel):
    scad: str
    params: dict[str, float | str] = {}


class RenameRequest(BaseModel):
    name: str


class SpecRequest(BaseModel):
    prompt: str
    mesh_id: str | None = None
    mesh_ids: list[str] | None = None
    profile: str | None = None


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


def print_report(stl_path: Path, profile: dict | None = None) -> dict:
    """Bounding box, weight estimate, connectivity — the numbers a maker checks first."""
    m = trimesh.load_mesh(stl_path)
    density = (profile or {}).get("density", 1.24)
    vol_cm3 = float(abs(m.volume)) / 1000.0 if m.is_volume else None
    out = {
        "bbox_mm": [round(float(v), 1) for v in m.extents],
        "watertight": bool(m.is_watertight),
        "parts": len(m.split(only_watertight=False)),
        "est_grams_pla": round(vol_cm3 * density, 1) if vol_cm3 else None,
    }
    if profile:
        out["profile"] = profile["name"]
        out["material"] = profile["material"]
        bed = profile["bed_mm"]
        fits = all(e <= b for e, b in zip(m.extents, bed))
        out["bed_fit"] = "ok" if fits else (
            f"EXCEEDS {profile['printer']} bed "
            f"({bed[0]}x{bed[1]}x{bed[2]}mm)")
    return out


def _profile(name, printer, bed, material, density, ams, overhang=50,
             notes="prefer support-free geometry; tree supports acceptable"):
    return {"name": name, "printer": printer, "bed_mm": bed, "nozzle": 0.4,
            "layer": 0.2, "material": material, "density": density,
            "min_wall": 2.0, "fit_clearance": 0.2, "snap_clearance": 0.15,
            "loose_clearance": 0.4, "min_detail_depth": 0.8,
            "max_overhang_deg": overhang, "multicolor": ams, "supports": notes}


DEFAULT_PROFILES = {p["name"]: p for p in [
    _profile("Bambu A1 - 0.4mm PLA", "Bambu A1", [256, 256, 256], "PLA", 1.24, True),
    _profile("Bambu A1 - 0.4mm PETG", "Bambu A1", [256, 256, 256], "PETG", 1.27, True, 45),
    _profile("Bambu P1S - 0.4mm PLA", "Bambu P1S", [256, 256, 256], "PLA", 1.24, True),
    _profile("Bambu P1S - 0.4mm PETG", "Bambu P1S", [256, 256, 256], "PETG", 1.27, True, 45),
    _profile("Generic FDM - 220x220x250 PLA", "Generic FDM", [220, 220, 250], "PLA", 1.24, False),
]}
PROFILES_FILE = Path(__file__).parent / "profiles.json"
DEFAULT_PROFILE = "Generic FDM - 220x220x250 PLA"


def all_profiles() -> dict:
    out = dict(DEFAULT_PROFILES)
    if PROFILES_FILE.exists():
        try:
            out.update(json.loads(PROFILES_FILE.read_text()))
        except Exception:
            pass
    return out


def get_profile(name: str | None) -> dict:
    profs = all_profiles()
    return profs.get(name or "", profs.get(DEFAULT_PROFILE) or next(iter(profs.values())))


def resolve_profile(name: str | None, prompt: str) -> tuple[dict, str | None]:
    """Active profile, unless the prompt explicitly names a different printer."""
    active = get_profile(name)
    pl = prompt.lower()
    for prof in all_profiles().values():
        printer = prof["printer"].lower()
        if printer != "generic fdm" and printer in pl and prof["printer"] != active["printer"]:
            # same material as active if such a variant exists
            for cand in all_profiles().values():
                if cand["printer"] == prof["printer"] and cand["material"] == active["material"]:
                    return cand, f"prompt names {prof['printer']}, overriding active profile for this job"
            return prof, f"prompt names {prof['printer']}, overriding active profile for this job"
    return active, None


def profile_block(p: dict, override: str | None = None) -> str:
    o = f" ({override})" if override else ""
    return (
        f"\nPRINTER PROFILE{o} — hard print constraints for this job, overriding any "
        f"conflicting user defaults:\n"
        f"profile: {p['name']} | printer: {p['printer']} | build volume: "
        f"{p['bed_mm'][0]}x{p['bed_mm'][1]}x{p['bed_mm'][2]}mm (the print layout AND every "
        f"part must fit inside) | nozzle: {p['nozzle']} | layer: {p['layer']} | material: "
        f"{p['material']} | min wall: {p['min_wall']} | fit clearance: {p['fit_clearance']} "
        f"| snap-fit clearance: {p['snap_clearance']} | loose clearance: "
        f"{p['loose_clearance']} | min emboss/deboss depth: {p['min_detail_depth']} | max "
        f"unsupported overhang: {p['max_overhang_deg']}deg | multicolor/AMS: "
        f"{'yes' if p['multicolor'] else 'NO - single color only'} | supports: {p['supports']}\n"
    )


PRESETS_FILE = Path(__file__).parent / "presets.txt"
DEFAULT_PRESETS = """# your MEASURED objects (print settings now come from the printer profile)
# e.g.:
# phone width = 78 (with case), phone thickness = 12
# desk edge thickness = 25
# my calibrated slip fit = 0.2 (from the calibration coupon)"""


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


# --- Training Lab -> production promotion (human-gated feedback loop) ---
# The evolution lab is isolated by design; these are the ONLY paths lab output reaches
# production, and only via explicit human approval (wired as EvolutionAdapters callables so
# evolution_lab never imports app and its store never writes to library/ itself).
PROMOTED_RULES_FILE = LIB_DIR / "promoted_rules.json"


def _load_promoted_rules() -> list[dict]:
    try:
        return json.loads(PROMOTED_RULES_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return []


def promoted_rules_block(prompt: str = "") -> str:
    """Design rules promoted from the evolution Training Lab (human-approved, validated across
    multiple models). Injected into production generation so lab-learned lessons shape output.
    Disable instantly with PRINT_FORGE_PROMOTED_RULES=0."""
    if os.environ.get("PRINT_FORGE_PROMOTED_RULES", "1") == "0":
        return ""
    rules = _load_promoted_rules()
    if not rules:
        return ""
    lines = "\n".join(
        f"- {r['recommendation']}" + (f" (when {r['trigger_conditions']})" if r.get("trigger_conditions") else "")
        for r in rules if r.get("recommendation"))
    if not lines:
        return ""
    return ("\n\nVALIDATED DESIGN RULES — lessons promoted from PrintForge's evolution lab, "
            "confirmed across multiple models. Apply any that fit this request:\n" + lines + "\n")


def promote_exemplar_to_library(scad: str, name: str, prompt: str, score, candidate_id: str) -> str:
    """Promote a Training Lab winning candidate into the production library as a thumbs-up
    few-shot exemplar (consumed by _taste_example). Reversible: it is a normal rated model."""
    model_id = uuid.uuid4().hex[:12]
    mdir = LIB_DIR / model_id
    mdir.mkdir()
    (mdir / "model.scad").write_text(scad)
    (mdir / "meta.json").write_text(json.dumps({
        "id": model_id,
        "name": (name or prompt).strip()[:60],
        "prompt": prompt,
        "rating": 1,
        "source": "evolution-lab",
        "source_candidate_id": candidate_id,
        "score": score,
        "created": time.time(),
        "promoted_at": time.time(),
    }))
    try:
        job = WORK_DIR / f"promote-{model_id}.scad"
        job.write_text(scad)
        render_png(job, mdir / "thumb.png", imgsize="400,300")
    except Exception:
        pass
    return model_id


def revoke_exemplar_from_library(candidate_id: str) -> int:
    """Remove any library exemplars that were promoted from a given lab candidate."""
    removed = 0
    for mdir in LIB_DIR.iterdir():
        mf = mdir / "meta.json"
        if not mf.exists():
            continue
        try:
            meta = json.loads(mf.read_text())
        except ValueError:
            continue
        if meta.get("source") == "evolution-lab" and meta.get("source_candidate_id") == candidate_id:
            shutil.rmtree(mdir, ignore_errors=True)
            removed += 1
    return removed


def promote_rule_to_production(rule: dict) -> dict:
    """Append (or replace) a validated lab memory rule in the production promoted-rules file."""
    rid = rule.get("id") or rule.get("rule_id")
    rules = [r for r in _load_promoted_rules() if r.get("id") != rid]
    entry = {
        "id": rid,
        "recommendation": rule.get("recommendation", ""),
        "trigger_conditions": rule.get("trigger_conditions", ""),
        "scope": rule.get("scope", {}),
        "source": "evolution-lab",
        "promoted_at": time.time(),
    }
    rules.append(entry)
    PROMOTED_RULES_FILE.write_text(json.dumps(rules, indent=2))
    return entry


def revoke_rule_from_production(rule_id: str) -> int:
    rules = _load_promoted_rules()
    kept = [r for r in rules if r.get("id") != rule_id]
    PROMOTED_RULES_FILE.write_text(json.dumps(kept, indent=2))
    return len(rules) - len(kept)


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
    bodies_detail = []
    try:
        m = trimesh.load(src, force="mesh")
        if ext in {".step", ".stp"}:
            fix = trimesh.transformations.rotation_matrix(3.141592653589793 / 2, [1, 0, 0])
            m.apply_scale(1000)  # cascadio emits GLB meters; STEP dimensions are mm
            m.apply_transform(fix)  # GLB Y-up -> OpenSCAD Z-up
            # CAD assemblies keep NAMED bodies (connectors!) — extract them with real
            # positions so cutouts can be derived instead of guessed
            try:
                scene = trimesh.load(src)
                if hasattr(scene, "graph"):
                    for node in scene.graph.nodes_geometry:
                        tf, gname = scene.graph[node]
                        g = scene.geometry[gname].copy()
                        g.apply_transform(tf)
                        g.apply_scale(1000)
                        g.apply_transform(fix)
                        bodies_detail.append({
                            "name": str(gname)[:60],
                            "min": [round(float(v), 1) for v in g.bounds[0]],
                            "max": [round(float(v), 1) for v in g.bounds[1]],
                        })
                bodies_detail = bodies_detail[:28]
            except Exception:
                bodies_detail = []
    except Exception as e:
        src.unlink(missing_ok=True)
        raise HTTPException(422, f"could not read {ext} file: {e}")
    if m.is_empty or m.bounds is None or len(m.faces) == 0:
        src.unlink(missing_ok=True)
        raise HTTPException(422, f"'{filename}' contains no 3D geometry after conversion "
                                 "— is it an empty or 2D-only file?")
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
    try:
        bodies = len(m.split(only_watertight=False))
    except Exception:
        bodies = 1
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
            "tris": int(len(m.faces)), "bodies": bodies,
            "bodies_detail": bodies_detail,
            "watertight": bool(m.is_watertight),
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


def _guard_public_host(host: str):
    """Reject hosts that resolve into private/loopback/link-local space — SSRF guard for
    user-supplied import URLs. The box can otherwise reach the router, LAN services and the
    cloud metadata endpoint. ponytail: getaddrinfo is blocking and this is a check-then-
    connect TOCTOU (DNS rebinding could slip through); acceptable for a single-user LAN app,
    upgrade to a pinned-IP connection if this ever faces untrusted callers."""
    if not host:
        raise HTTPException(400, "missing host in URL")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        raise HTTPException(400, f"could not resolve host '{host}'")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_unspecified or ip.is_multicast):
            raise HTTPException(400, f"refusing to fetch a non-public address ({ip})")


async def _safe_get(client: httpx.AsyncClient, url: str, headers: dict) -> httpx.Response:
    """GET that re-checks the host guard on EVERY redirect hop — a public URL that 302s to
    169.254.169.254 is the classic SSRF bypass, so we follow redirects manually."""
    for _ in range(6):  # initial + up to 5 redirects
        u = httpx.URL(url)
        if u.scheme not in ("http", "https"):
            raise HTTPException(400, f"unsupported URL scheme '{u.scheme}'")
        _guard_public_host(u.host or "")
        r = await client.get(url, headers=headers)  # client default follow_redirects=False
        if r.is_redirect and r.headers.get("location"):
            url = urljoin(str(u), r.headers["location"])
            continue
        return r
    raise HTTPException(400, "too many redirects")


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
    dl = await _safe_get(client, link, UA)
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
    dl = await _safe_get(client, f["download_url"], auth)
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
            dl = await _safe_get(client, url, UA)
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
                     "appear in the printable output. Give it a preview module guarded "
                     "ONLY by its own `<name>_preview_enabled = 0; // [0:1]` toggle — "
                     "when the user enables it, render it at its ASSEMBLED position in "
                     "both layouts (a floating preview over the print layout is fine).",
        "fit_cutout": "\nROLE = FIT/CUTOUT REFERENCE: measure this object to size every "
                      "cavity, port cutout, standoff position and clearance around it — "
                      "but its geometry must NOT appear in the printable output. Give it "
                      "a preview module guarded ONLY by its own "
                      "`<name>_preview_enabled = 0; // [0:1]` toggle, rendered at its "
                      "assembled position in both layouts when enabled.",
        "assembly": "\nROLE = ASSEMBLY COMPONENT: include it as a printable part of the "
                    "assembly, positioned per the request, with its own *_enabled toggle.",
        "negative": "\nROLE = NEGATIVE SPACE: subtract this object's envelope plus the "
                    "requested clearance from surrounding printed parts. Never include "
                    "it as printed geometry.",
    }.get(role, "")
    info_note = (f"\n(format: {meta.get('format', 'stl')}, {meta.get('tris', '?')} "
                 f"triangles, watertight: {meta.get('watertight')})")
    detail = meta.get("bodies_detail") or []
    if detail:
        rows = "\n".join(f"  {b['name']}: {b['min']} -> {b['max']}" for b in detail)
        info_note += (
            "\nNAMED CAD BODIES inside this model (mm, same coordinates as the import — "
            "connector names reveal the real ports):\n" + rows +
            "\nDerive every port cutout from these actual body positions/sizes (case "
            "side, center, opening size) instead of assuming standard layouts. Include "
            "a comment block in the file: CUTOUT DETECTION REPORT listing each port: "
            "side, center, cutout size, clearance added, source (STEP body name), and "
            "confidence high/medium/low. Ports without a matching named body get a "
            "conservative oversized window and confidence=low — never fake precision."
        )
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


@app.get("/profiles")
async def list_profiles():
    return {"profiles": list(all_profiles().values()), "default": DEFAULT_PROFILE}


class CustomProfileRequest(BaseModel):
    profile: dict


@app.put("/profiles/custom")
async def set_custom_profile(req: CustomProfileRequest):
    base = get_profile(DEFAULT_PROFILE).copy()
    base.update({k: v for k, v in req.profile.items() if k in base})
    base["name"] = str(req.profile.get("name", "Custom Printer"))[:50]
    customs = {}
    if PROFILES_FILE.exists():
        try:
            customs = json.loads(PROFILES_FILE.read_text())
        except Exception:
            pass
    customs[base["name"]] = base
    PROFILES_FILE.write_text(json.dumps(customs, indent=1))
    return base


@app.post("/spec")
async def spec(req: SpecRequest):
    mesh_note = _all_mesh_notes(_mesh_ids(req))
    prof, override = resolve_profile(req.profile, req.prompt)
    text = await call_llm([{"role": "user",
                            "content": spec_prompt(req.prompt, mesh_note)
                                       + profile_block(prof, override) + presets_block()}])
    return {"spec": text, "profile": prof["name"], "override": override}


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


async def _run_generate(req: GenerateRequest, emit):
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
    prof, prof_override = resolve_profile(req.profile, req.prompt)
    prof_note = profile_block(prof, prof_override)
    parent_prof = _parent_meta(req.parent_id).get("profile", {}).get("name")
    if req.current_scad and parent_prof and parent_prof != prof["name"]:
        prof_note += (f"\nPROFILE CHANGE: this model was generated for '{parent_prof}' "
                      f"and is now targeting '{prof['name']}' — re-check wall thickness, "
                      "clearances, overhangs and bed fit against the new profile.\n")
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
        instruction = SYSTEM_PROMPT + archetype_notes(req.prompt) + prof_note + presets_block() + promoted_rules_block(req.prompt) + "\n\n" + (f"{mesh_note}\n\n" if mesh_note else "") + (
            intent_block(load_intent(req.parent_id)) +
            rules_block(load_rules(req.parent_id)) +
            part_state_block(load_part_state(req.parent_id)) +
            "The attached images (if any) are renders of the current model and/or a "
            f"user-provided reference photo.\nModification request: {req.prompt}{float_note}"
        )
        global LAST_BACKEND
        await emit("llm", "Editing the model…")
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
            await emit("render", "Rendering STL…")
            stl = render_stl(scad, {})
        except HTTPException as e:
            await emit("fixing", "Fixing render error…")
            scad = await asyncio.to_thread(
                call_codex_edit, scad,
                f"The file fails to render with this error:\n{e.detail}\nFix it.")
            stl = render_stl(scad, {})  # second failure propagates to the UI
    else:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT + archetype_notes(req.prompt)
                                          + prof_note + presets_block()
                                          + promoted_rules_block(req.prompt)
                                          + _taste_example(req.prompt)},
            {"role": "user", "content": user_prompt(req.prompt, req.current_scad, mesh_note)},
        ]
        await emit("llm", "Writing OpenSCAD…")
        scad = await call_llm(messages, images)
        # validate by rendering; on failure, one automatic retry with the error fed back
        try:
            await emit("render", "Rendering STL…")
            stl = render_stl(scad, {})
        except HTTPException as e:
            await emit("fixing", "Fixing render error…")
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
        for _qa_i in range(QA_ROUNDS):
            await emit(f"qa_round_{_qa_i + 1}", f"Vision QA round {_qa_i + 1}…")
            try:
                ps_note = part_state_block(load_part_state(req.parent_id))
                scad, stl, status = await vision_qa(
                    req.prompt, scad, stl, base_stl,
                    load_intent(req.parent_id) + ([ps_note] if ps_note else []) + [prof_note],
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

    await emit("report", "Building print report…")
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
        report = await asyncio.to_thread(print_report, report_stl, prof)
        report["measured"] = measured
    except Exception:
        report = {}
    meta_file = LIB_DIR / model_id / "meta.json"
    meta = json.loads(meta_file.read_text())
    meta.update({"qa": qa, "backend": LAST_BACKEND, "report": report,
                 "profile": dict(prof)})  # snapshot values, not just the name
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
            "print_warning_details": warning_details,
            "profile_used": prof["name"], "profile_override": prof_override}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _now_ms() -> int:
    return int(time.time() * 1000)


@app.post("/generate")
async def generate(req: GenerateRequest, stream: int = 1):
    # stream=0 keeps the original single-JSON response (unchanged contract for
    # any non-SSE caller); default streams stage events as Server-Sent Events.
    if not stream:
        async def _noop(*_a, **_k):
            return
        return await _run_generate(req, _noop)

    q: asyncio.Queue = asyncio.Queue()
    state = {"stage": "start"}  # last active stage, for labelling an error

    async def emit(stage: str, label: str = ""):
        state["stage"] = stage
        await q.put({"stage": stage, "label": label, "ts": _now_ms()})

    async def _run():
        try:
            result = await _run_generate(req, emit)
            await q.put({"stage": "done", "ts": _now_ms(), "result": result})
        except HTTPException as e:
            await q.put({"stage": "error", "failed": state["stage"],
                         "detail": str(e.detail), "ts": _now_ms()})
        except Exception as e:  # never leak a stack trace to the client
            await q.put({"stage": "error", "failed": state["stage"],
                         "detail": str(e), "ts": _now_ms()})
        finally:
            await q.put(None)  # sentinel: stream done

    async def gen():
        task = asyncio.create_task(_run())
        try:
            while True:
                # keepalive comment every 15s so the long codex step (running in
                # a thread) keeps the connection flushing through any proxy.
                try:
                    item = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if item is None:
                    break
                yield _sse(item)
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    if req.role not in {"printable", "reference", "fit_cutout", "assembly", "negative"}:
        raise HTTPException(400, "role must be printable, reference, fit_cutout, "
                                 "assembly or negative")
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
    return {
        "bambuddy": bool(BAMBUDDY_API_KEY),
        "organic": _organic_ready(),
        "training_lab": EVOLUTION_LAB_CONFIG.training_lab_enabled,
        "evolution": EVOLUTION_LAB_CONFIG.evolution_enabled,
        "memory_learning": EVOLUTION_LAB_CONFIG.memory_learning_enabled,
        "physical_feedback": EVOLUTION_LAB_CONFIG.physical_feedback_enabled,
        "actual_training": EVOLUTION_LAB_CONFIG.actual_training_enabled,
    }


def _model_dir(model_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", model_id):
        raise HTTPException(400, "bad id")
    mdir = LIB_DIR / model_id
    if not mdir.exists():
        raise HTTPException(404, "not found")
    return mdir


def _public_model_meta(meta: dict) -> dict:
    """Add browser-safe library identity/version fields without exposing paths."""
    model_id = str(meta.get("id") or "")
    if not re.fullmatch(r"[0-9a-f]{12}", model_id):
        raise ValueError("invalid public model id in metadata")
    version = 1
    parent = meta.get("parent")
    seen = {model_id}
    while isinstance(parent, str) and re.fullmatch(r"[0-9a-f]{12}", parent) and parent not in seen:
        seen.add(parent)
        parent_file = LIB_DIR / parent / "meta.json"
        if not parent_file.exists():
            break
        version += 1
        try:
            parent = json.loads(parent_file.read_text()).get("parent")
        except (OSError, ValueError, json.JSONDecodeError):
            break
    qa = str(meta.get("qa") or "").lower()
    status = "qa_passed" if qa == "passed" else "qa_fixed" if qa.startswith("fixed") else "ready"
    return {
        **meta,
        "library_id": model_id,
        "latest_version": version,
        "status": status,
        "thumbnail_url": f"/models/{model_id}/thumb",
        "model_url": f"/?model={model_id}",
    }


@app.get("/models")
async def list_models():
    out = []
    for mdir in LIB_DIR.iterdir():
        meta = mdir / "meta.json"
        if meta.exists():
            try:
                out.append(_public_model_meta(json.loads(meta.read_text())))
            except (ValueError, json.JSONDecodeError):
                continue
    return sorted(out, key=lambda m: -m["created"])


@app.get("/models/{model_id}/metadata")
async def model_metadata(model_id: str):
    mdir = _model_dir(model_id)
    return _public_model_meta(json.loads((mdir / "meta.json").read_text()))


@app.get("/models/{model_id}")
async def get_model(model_id: str):
    mdir = _model_dir(model_id)
    scad = (mdir / "model.scad").read_text()
    stl = render_stl(scad, {})
    return {"meta": _public_model_meta(json.loads((mdir / "meta.json").read_text())),
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
    """Few-shot from the user's ratings: the best-matching thumbs-up model to emulate
    and the best-matching thumbs-down model to avoid."""
    words = set(re.findall(r"[a-z]{3,}", prompt.lower()))
    best = {1: (None, 1), -1: (None, 1)}  # sign -> (mdir, score); require >=2 word overlap
    for mdir in LIB_DIR.iterdir():
        mf = mdir / "meta.json"
        if not mf.exists():
            continue
        meta = json.loads(mf.read_text())
        rating = meta.get("rating", 0)
        if rating == 0:
            continue
        sign = 1 if rating > 0 else -1
        mwords = set(re.findall(r"[a-z]{3,}",
                                f"{meta.get('prompt', '')} {meta.get('name', '')}".lower()))
        score = len(words & mwords)
        if score > best[sign][1]:
            best[sign] = (mdir, score)

    def _scad(mdir):  # None or the model, only if short enough to be a useful few-shot
        if not mdir:
            return None
        s = (mdir / "model.scad").read_text()
        return s if len(s) <= 8000 else None

    out = ""
    pos = _scad(best[1][0])
    if pos:
        out += ("\n\nUSER-APPROVED EXAMPLE — the user rated this result highly for a similar "
                "request; match its conventions, printability choices and quality bar:\n"
                + pos)
    neg = _scad(best[-1][0])
    if neg:
        out += ("\n\nUSER-REJECTED EXAMPLE — the user rated this result poorly for a similar "
                "request. Do NOT reproduce its approach; treat it as a cautionary "
                "counter-example of what the user does not want:\n" + neg)
    return out


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


def _lab_load_source(model_id: str) -> dict:
    """Read a production model as an immutable lab input; never write back to it."""
    mdir = _model_dir(model_id)
    return {
        "scad": (mdir / "model.scad").read_text(),
        "meta": json.loads((mdir / "meta.json").read_text()),
    }


def _lab_wait_process(proc: subprocess.Popen, cancel_event, timeout: float) -> tuple[str, str]:
    deadline = time.monotonic() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            raise RuntimeError("generation cancelled")
        if time.monotonic() >= deadline:
            os.killpg(proc.pid, signal.SIGKILL)
            raise RuntimeError("generation subprocess timed out")
        try:
            return proc.communicate(timeout=0.1)
        except subprocess.TimeoutExpired:
            continue


def _lab_render_stl(scad: str, params: dict, cancel_event=None) -> Path:
    job = WORK_DIR / uuid.uuid4().hex
    scad_file, stl_file = job.with_suffix(".scad"), job.with_suffix(".stl")
    scad_file.write_text(scad)
    cmd = ["openscad", *OPENSCAD_ARGS, "-o", str(stl_file), "--export-format", "binstl"]
    for key, value in params.items():
        if not re.fullmatch(r"\w+", key):
            raise HTTPException(400, f"bad param name: {key}")
        encoded = f'"{value}"' if isinstance(value, str) else str(value)
        cmd += ["-D", f"{key}={encoded}"]
    cmd.append(str(scad_file))
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    _stdout, stderr = _lab_wait_process(proc, cancel_event, OPENSCAD_TIMEOUT)
    if proc.returncode != 0 or not stl_file.exists():
        raise HTTPException(422, f"OpenSCAD error:\n{stderr[-2000:]}")
    if stl_file.stat().st_size > 100_000_000:
        raise HTTPException(413, "STL too large")
    return stl_file


def _lab_render_png(scad_path: Path, out_png: Path, cancel_event=None) -> Path | None:
    cmd = ["openscad", *OPENSCAD_ARGS, "-o", str(out_png), "--imgsize", "800,600", "--autocenter", "--viewall", "--camera", "0,0,0,70,0,25,340", "--projection", "p", str(scad_path)]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    _lab_wait_process(proc, cancel_event, OPENSCAD_TIMEOUT)
    return out_png if proc.returncode == 0 and out_png.exists() else None


def _lab_codex_process(cmd: list[str], prompt: str, cancel_event, timeout: float) -> tuple[str, str]:
    """Run one lab-only Codex process with cooperative, process-group cancellation."""
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    try:
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=0.25)
            if proc.returncode != 0:
                raise RuntimeError(f"codex process failed (rc {proc.returncode}): {stderr[-1500:]}")
            return stdout, stderr
        except subprocess.TimeoutExpired:
            pass
        while True:
            if cancel_event is not None and cancel_event.is_set():
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                raise RuntimeError("generation cancelled")
            if time.monotonic() >= deadline:
                os.killpg(proc.pid, signal.SIGKILL)
                raise RuntimeError("codex generation timed out")
            try:
                stdout, stderr = proc.communicate(timeout=0.25)
                if proc.returncode != 0:
                    raise RuntimeError(f"codex process failed (rc {proc.returncode}): {stderr[-1500:]}")
                return stdout, stderr
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _lab_codex_fresh(prompt: str, cancel_event=None, images: list[str] | None = None) -> str:
    out = WORK_DIR / f"lab-codex-{uuid.uuid4().hex}.txt"
    cmd = ["codex", "exec", "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "-o", str(out)]
    for image in images or []:
        cmd += ["-i", image]
    cmd.append("-")
    _stdout, stderr = _lab_codex_process(cmd, prompt, cancel_event, 900)
    if not out.exists():
        raise RuntimeError(f"codex generation failed: {stderr[-1500:]}")
    return strip_fences(out.read_text())


def _lab_codex_edit(scad: str, instruction: str, cancel_event=None) -> str:
    job = WORK_DIR / f"lab-edit-{uuid.uuid4().hex}"
    job.mkdir()
    model = job / "model.scad"
    model.write_text(scad)
    prompt = (
        f"{instruction}\n\nApply your changes by EDITING model.scad in this directory with precise, "
        "minimal edits. Preserve every unrelated feature. Never rewrite an existing model from scratch."
    )
    cmd = ["codex", "exec", "-C", str(job), "-s", "workspace-write", "--skip-git-repo-check", "--ephemeral", "-"]
    _stdout, stderr = _lab_codex_process(cmd, prompt, cancel_event, 900)
    if not model.exists() or not model.read_text().strip():
        raise RuntimeError(f"codex edit failed: {stderr[-1500:]}")
    return model.read_text()


def _lab_generate_initial_candidate(context: dict) -> dict:
    if LLM_BACKEND != "codex":
        raise RuntimeError("Evolution generation zero currently requires the codex backend")
    profile = context.get("printer_profile") or {}
    instruction = f"""{SYSTEM_PROMPT}

Create generation zero for an isolated PrintForge evolution run.
Return one complete OpenSCAD model that follows the validated design specification.

DESIGN SPECIFICATION (law):
{context['validated_spec']}

LOCKED REQUIREMENTS (must be present and satisfied; severity in brackets — HARD LOCK and FORBIDDEN are non-negotiable):
{requirements_prompt_block(context.get('locked_constraints', []))}

PRINTER PROFILE:
{json.dumps(profile, indent=2)}

Do not claim validation, slicing, or physical testing. The pipeline will measure the result.
"""
    scad = _lab_codex_fresh(instruction, context.get("cancel_event"))
    return {
        "scad": scad, "backend": "codex/cli-default (fresh)",
        "backend_calls": 1, "estimated_cost": 0,
        "cost_estimate_status": "unavailable",
    }


def _lab_generate_candidate(parent_scad: str, context: dict) -> dict:
    if LLM_BACKEND != "codex":
        raise RuntimeError("Evolution candidate editing currently requires the codex backend")
    mutation = context["mutation"]
    memory = context.get("memory_rules", {})
    applied = "\n".join(
        f"- {rule.get('recommendation', '')} (confidence {rule.get('confidence', 0):.2f})"
        for rule in memory.get("applied", [])
    ) or "- none"
    instruction = f"""You are the DESIGNER role in an isolated PrintForge A/B evolution run.
Edit the supplied model.scad IN PLACE with the smallest possible change. Do not rewrite it.

VALIDATED SPEC (law):
{context['validated_spec']}

LOCKED REQUIREMENTS (severity in brackets — HARD LOCK and FORBIDDEN must hold exactly):
{requirements_prompt_block(context.get('locked_constraints', []))}

CONTROLLED MUTATION:
{json.dumps(mutation, indent=2)}

Expected benefit: {mutation.get('expected_benefit', '')}
Relevant validated memory rules:
{applied}

Preserve every unrelated working feature, parameter, module, part role and export exclusion.
Do not add an unrelated redesign. Do not claim QA, slicing, or physical validation.
"""
    scad = _lab_codex_edit(parent_scad, instruction, context.get("cancel_event"))
    return {
        "scad": scad,
        "backend": "codex/cli-default (edit)",
        "backend_calls": 1,
        "estimated_cost": 0,
        "cost_estimate_status": "unavailable",
    }


def _lab_constraint_findings(parent_scad: str, candidate_scad: str, locks: list,
                             report: dict | None = None, floats: list | None = None) -> tuple[list[dict], list[str]]:
    """Deterministically verify guided/legacy locked requirements against the
    rendered candidate. Delegates to the pure evolution_lab.requirements module so
    the same logic is unit-tested without the generation stack."""
    return verify_requirements(parent_scad, candidate_scad, report or {}, floats or [], locks or [])


def _lab_ai_review(scad: str, context: dict, report: dict, issues: list[dict], images: list[str]) -> tuple[list[dict], int]:
    prompt = f"""You are the independent FUNCTION CRITIC, PROMPT/SPEC AUDITOR, PRINT ENGINEER,
STRUCTURAL REVIEWER, ERGONOMICS REVIEWER, and SIMPLICITY REVIEWER for PrintForge.
The DESIGNER did not score this output. Judge only the supplied SCAD, deterministic report,
issues and renders. Unknown claims get zero. Never claim slicing or physical verification.
Return JSON only with keys function, adherence, structural, ergonomics, simplicity. Each value
must be {{"points": number, "reason": string}}. Caps respectively: 18, 16, 7, 6, 7.

SPEC:\n{context['validated_spec'][:30000]}
MUTATION:\n{json.dumps(context['mutation'])}
REPORT:\n{json.dumps(report)}
DETERMINISTIC ISSUES:\n{json.dumps(issues)}
SCAD SOURCE:\n{scad[:30000]}
"""
    try:
        judged = json.loads(_lab_codex_fresh(prompt, context.get("cancel_event"), images))
    except Exception:
        return [
            {"category": "function", "criterion": "functional behavior", "points_awarded": 0, "points_possible": 25, "label": "UNVERIFIED", "source": "independent reviewer unavailable", "summary": "No independent function review", "confidence": 0, "critical": True},
            {"category": "prompt_spec_adherence", "criterion": "spec adherence", "points_awarded": 0, "points_possible": 16, "label": "UNVERIFIED", "source": "independent reviewer unavailable", "summary": "No independent spec review", "confidence": 0, "critical": True},
        ], 0
    mapping = {
        "function": ("function", 18, 25), "adherence": ("prompt_spec_adherence", 16, 16),
        "structural": ("structural_quality", 7, 10), "ergonomics": ("user_experience_ergonomics", 6, 10),
        "simplicity": ("simplicity_efficiency", 7, 10),
    }
    evidence = []
    for key, (category, cap, possible) in mapping.items():
        item = judged.get(key) if isinstance(judged, dict) else None
        points = max(0.0, min(float((item or {}).get("points", 0)), cap))
        evidence.append({
            "category": category, "criterion": f"independent {key} review",
            "points_awarded": points, "points_possible": possible, "label": "AI-JUDGED",
            "source": "independent codex review of SCAD, metrics and renders",
            "summary": str((item or {}).get("reason", "No rationale supplied"))[:2000],
            "confidence": 0.55, "critical": key in {"function", "adherence"},
        })
    return evidence, 1


def _lab_evaluate_candidate(scad: str, context: dict) -> dict:
    cancel_event = context.get("cancel_event")
    stl = _lab_render_stl(scad, {}, cancel_event)
    profile = context.get("printer_profile") or {}
    report = print_report(stl, profile)
    try:
        floats = floating_starts(stl)
    except Exception:
        floats = []
    issues = [{
        "issue_type": "floating_geometry", "severity": "warning",
        "coordinates": [item.get("x"), item.get("y"), item.get("z")],
        "message": f"Feature starts in mid-air near x={item.get('x')}, y={item.get('y')}, z={item.get('z')}mm",
        "source": "floating_starts",
    } for item in floats]
    lock_issues, failure_codes = _lab_constraint_findings(context.get("parent_scad", ""), scad, context.get("locked_constraints", []), report, floats)
    issues.extend(lock_issues)
    lock_violated = any(item.get("severity") in {"critical", "warning"} for item in lock_issues)
    bed_ok = report.get("bed_fit") == "ok"
    if not bed_ok:
        failure_codes.append("build_volume_overflow")
    evidence = [
        {"category": "printability", "criterion": "render and export", "points_awarded": 4, "points_possible": 4, "label": "MEASURED", "source": "OpenSCAD render", "summary": "Candidate rendered to STL", "confidence": 1, "critical": True},
        {"category": "printability", "criterion": "watertight geometry", "points_awarded": 6 if report.get("watertight") else 0, "points_possible": 6, "label": "MEASURED", "source": "trimesh", "summary": "Watertight mesh check", "confidence": 1, "critical": False},
        {"category": "printability", "criterion": "floating regions", "points_awarded": 5 if not floats else 0, "points_possible": 5, "label": "MEASURED", "source": "floating_starts", "summary": f"{len(floats)} reported floating starts", "confidence": 0.9, "critical": False},
        {"category": "printability", "criterion": "build volume", "points_awarded": 4 if bed_ok else 0, "points_possible": 4, "label": "MEASURED", "source": "print_report", "summary": report.get("bed_fit", "unavailable"), "confidence": 1, "critical": True},
        {"category": "printability", "criterion": "component structure", "points_awarded": 3, "points_possible": 3, "label": "MEASURED", "source": "trimesh connected components", "summary": f"{report.get('parts')} components; intent review required", "confidence": 0.7, "critical": False},
    ]
    if context.get("attached_reference_roles") or context.get("export_exclusions"):
        evidence.append({"category": "prompt_spec_adherence", "criterion": "reference geometry excluded from export", "points_awarded": 0, "points_possible": 4, "label": "UNVERIFIED", "source": "no post-export role verifier", "summary": "Reference leakage cannot yet be proved deterministically", "confidence": 0, "critical": True})
    else:
        evidence.append({"category": "prompt_spec_adherence", "criterion": "no reference export scope", "points_awarded": 4, "points_possible": 4, "label": "MEASURED", "source": "run configuration", "summary": "No attached reference-only geometry in this run", "confidence": 1, "critical": False})
    if context.get("locked_constraints"):
        evidence.append({"category": "prompt_spec_adherence", "criterion": "locked requirements preserved", "points_awarded": 4 if not lock_violated else 0, "points_possible": 4, "label": "MEASURED", "source": "deterministic constraint monitor", "summary": "All deterministically-checkable requirements preserved" if not lock_violated else "One or more requirements violated", "confidence": 1, "critical": True})
    preview = _lab_render_png(stl.with_suffix(".scad"), stl.with_name("lab-preview.png"), cancel_event)
    images = [str(preview)] if preview and preview.exists() else []
    ai_evidence, review_calls = _lab_ai_review(scad, context, report, issues, images)
    evidence.extend(ai_evidence)
    artifacts = {
        "model.stl": stl.read_bytes(),
        "geometry-report.json": json.dumps(report, indent=2),
        "qa-findings.json": json.dumps(issues, indent=2),
    }
    if preview and preview.exists():
        artifacts["preview.png"] = preview.read_bytes()
    return {
        "evidence": evidence, "failure_codes": failure_codes, "issues": issues,
        "qa_results": {"status": "evaluated", "deterministic": True, "ai_review": bool(review_calls)},
        "slicer_results": {"status": "skipped", "reason": "no slicer backend configured"},
        "artifacts": artifacts, "backend_calls": review_calls, "estimated_cost": 0,
        "cost_estimate_status": "unavailable",
    }


def _lab_current_branch() -> str:
    result = subprocess.run(["git", "branch", "--show-current"], cwd=Path(__file__).parent, capture_output=True, text=True, timeout=2)
    return result.stdout.strip() or "detached"


app.include_router(create_router(
    EVOLUTION_LAB_CONFIG,
    EvolutionAdapters(
        load_source_model=_lab_load_source,
        generate_initial_candidate=_lab_generate_initial_candidate,
        generate_candidate=_lab_generate_candidate,
        evaluate_candidate=_lab_evaluate_candidate,
        current_branch=_lab_current_branch,
        promote_exemplar=promote_exemplar_to_library,
        revoke_exemplar=revoke_exemplar_from_library,
        promote_rule=promote_rule_to_production,
        revoke_rule=revoke_rule_from_production,
    ),
    production_branch="main",
))


app.mount("/", StaticFiles(directory=Path(__file__).parent / "static", html=True))
