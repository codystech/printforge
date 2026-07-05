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
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from parts import split_parts, write_3mf
from prompts import SYSTEM_PROMPT, qa_prompt, user_prompt

# "codex" shells out to the codex CLI (gpt-5.5, host only) and falls back to HTTP on failure
LLM_BACKEND = os.environ.get("LLM_BACKEND", "http")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:4000/v1")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-brain-coder")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "dummy")
QA_CHECK = os.environ.get("QA_CHECK", "1") == "1"  # vision self-check, codex backend only
BAMBUDDY_URL = os.environ.get("BAMBUDDY_URL", "http://192.168.1.50:8000")
BAMBUDDY_API_KEY = os.environ.get("BAMBUDDY_API_KEY", "")
OPENSCAD_TIMEOUT = 60  # seconds; complex models can be slow
# needs openscad 2024+ (nixpkgs#openscad-unstable); set empty for old 2021.01 builds
OPENSCAD_ARGS = os.environ.get("OPENSCAD_ARGS", "--enable=textmetrics --enable=manifold").split()
WORK_DIR = Path(tempfile.gettempdir()) / "printforge"
WORK_DIR.mkdir(exist_ok=True)
LIB_DIR = Path(__file__).parent / "library"
LIB_DIR.mkdir(exist_ok=True)

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
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(f"codex exec failed: {proc.stderr[-500:]}")
    return strip_fences(out.read_text())


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
               imgsize: str = "800,600") -> Path | None:
    cmd = ["openscad", *OPENSCAD_ARGS, "-o", str(out_png), "--imgsize", imgsize,
           "--autocenter", "--viewall"]
    if camera:
        cmd += ["--camera", camera, "--projection", "o"]
    cmd.append(str(scad_path))
    try:
        subprocess.run(cmd, capture_output=True, timeout=OPENSCAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    return out_png if out_png.exists() else None


async def vision_qa(prompt: str, scad: str, stl: Path) -> tuple[str, Path, str]:
    """One round of render-and-review; returns (scad, stl, qa_status)."""
    scad_path = WORK_DIR / f"{stl.stem}.scad"
    iso = render_png(scad_path, WORK_DIR / f"{stl.stem}_iso.png")
    top = render_png(scad_path, WORK_DIR / f"{stl.stem}_top.png", camera="0,0,0,0,0,0,340")
    images = [str(p) for p in (iso, top) if p]
    if not images:
        return scad, stl, "skipped"
    reply = await asyncio.to_thread(
        call_codex, [{"role": "user", "content": qa_prompt(prompt, scad)}], images
    )
    if reply.strip() == "OK":
        return scad, stl, "passed"
    fixed = strip_fences(reply)
    try:
        fixed_stl = render_stl(fixed, {})
    except HTTPException:
        return scad, stl, "passed"  # ponytail: fix didn't render, ship the original
    return fixed, fixed_stl, "fixed"


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
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt(req.prompt, req.current_scad)},
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
        try:
            scad, stl, qa = await vision_qa(req.prompt, scad, stl)
        except Exception as e:
            print(f"vision QA failed: {e}")

    model_id = save_to_library(req.prompt, scad, stl)
    return {"scad": scad, "params": parse_params(scad), "stl_id": stl.stem,
            "qa": qa, "model_id": model_id}


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
