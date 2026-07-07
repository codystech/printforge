---
name: printforge-organic-quality-campaign
description: Load when improving PrintForge organic/image-to-mesh quality, debugging organic/generate.py output, evaluating Sparc3D/TRELLIS/TRELLIS.2/Hi3DGen/TripoSR/alternative organic backends, changing Hunyuan3D-2mini steps/model/subfolder, handling organic holes/components/floating regions/VRAM/download hangs, or working on hybrid organic+parametric modeling with import(base_mesh_path), cross-sections, embossing, engraving, or organic output registration.
---

# PrintForge Organic Quality Campaign

Use this as an executable, decision-gated research campaign. Do not ship a knob or backend change from vibes. Ship only after a fixed test set beats the current default on measured printability and owner-reviewed quality.

Definitions: organic means image-to-mesh generation from a photo. Backend means the model runner behind `organic/generate.py`. Watertight means the mesh encloses a volume with no open boundary edges. Connected components means separate mesh bodies after `trimesh.split()`. Floating starts are cross-section islands with no material below them, as detected by `parts.py:floating_starts()`. VRAM means GPU memory reported by `nvidia-smi`.

## Current State

As of 2026-07-06:

| Fact | Ground truth |
| --- | --- |
| Live organic default | Hunyuan3D-2mini, shape-only, local GPU path |
| Hardware assumption | RTX 3090 with 24GB VRAM, not guaranteed outside the base NixOS generation |
| Observed baseline | About 90 seconds per 30-step image on the 3090, not executed during skill authorship; re-verify |
| Runner contract | `organic/.venv/bin/python organic/generate.py --image X --out Y --target-mm Z [--steps N --model M --subfolder S]` |
| Default runner args | `--steps 30 --model tencent/Hunyuan3D-2mini --subfolder hunyuan3d-dit-v2-mini` |
| Postprocess | Rotate Hunyuan Y-up to PrintForge Z-up, keep largest component, fill holes, fix normals, cap at 400k faces, scale max dimension to `target_mm`, center XY, floor to Z=0, shave/cap a 0.4mm base slice |
| Endpoint path | `POST /organic` decodes a data URL, clamps `target_mm` to 10..250, serializes with `_organic_lock`, calls `_free_gpu()`, runs `organic/generate.py`, then `_register_mesh()` registers the STL as an upload |
| Hybrid path | Organic output is auto-registered as `uploads/<id>.stl`, gets metadata/cross-sections, and returns wrapper OpenSCAD: `base_mesh_path = "..."; import(base_mesh_path, convexity=10);` |

Do not call the live `/organic` endpoint during research. Direct runner jobs still use the shared GPU; schedule them, run one at a time, and route any production behavior change through `printforge-change-control`.

## When NOT to Use This Skill

| Need | Use this sibling skill instead |
| --- | --- |
| Deploying, restarting, editing live behavior, or deciding whether a change can ship | `printforge-change-control` |
| General incident triage or live outage debugging | `printforge-debugging-playbook` |
| Mesh math, 3MF, STEP, slicing, `floating_starts()`, or watertight interpretation only | `printforge-mesh-geometry-reference` |
| OpenSCAD import, rule 14 base-mesh fusion, emboss recipes, or parametric CAD semantics only | `printforge-openscad-reference` |
| Environment rebuild, organic setup, CUDA/libGL/opencv/pymeshlab traps | `printforge-build-and-env` |
| Evidence standards, golden models, and test discipline outside organic campaign work | `printforge-validation-and-qa` |
| Research framing without running the campaign | `printforge-research-frontier` or `printforge-research-methodology` |

## Non-Negotiable Safety

Checklist before any GPU run:

- Confirm the collaborator is not depending on the GPU-backed service right now.
- Do not start `run.sh`, `uvicorn`, Docker, systemd, Codex, or any organic job unless the campaign owner explicitly scheduled GPU time.
- Do not send `POST`, `PUT`, `PATCH`, or `DELETE` to `http://localhost:8093`.
- Do not write under `library/`, `uploads/`, `.env`, `presets.txt`, `profiles.json`, or `static/`.
- Store campaign inputs/results under the skill directory or another owner-approved stable path, never under user data directories.
- Keep Hunyuan3D-2mini as the production default until a candidate beats it on the scorecard and passes `printforge-change-control`.

Safe live readiness check:

```sh
cd /home/cody/projects/printforge
curl -fsS http://127.0.0.1:8093/config
```

Expected as of 2026-07-06 on the live host: `{"bambuddy":true,"organic":true}`. If the GET fails in a sandbox, mark the readiness claim `not executed here -- re-verify`.

## Metrics First

Never judge success by eye. Every run over every test image must produce this scorecard:

| Metric | Success direction | Collector |
| --- | --- | --- |
| `watertight` | `true` required after postprocess | `trimesh.is_watertight` |
| `components` | Usually `1`; if greater than 1, explain why postprocess did not keep only one | `mesh.split(only_watertight=False)` |
| `triangles` | Below or equal to 400000 after current postprocess | `len(mesh.faces)` |
| `floating_starts` | `0` required for promotion | `parts.py:floating_starts()` |
| `bbox_mm` and `target_error_mm` | Max dimension should be close to `target_mm`; investigate error over 1mm | `mesh.extents` |
| `inference_seconds` | Lower is better unless quality win is measurable | shell wall clock |
| `peak_vram_mb` | Must fit below 24GB with headroom | `nvidia-smi` sampling |
| `operator_quality_notes` | Human notes allowed only after metrics | concise defects: missing back, mushy detail, wrong silhouette, base damage |

Create one fixed 6-image test set and never tune on ad hoc images:

```sh
cd /home/cody/projects/printforge
export CAMPAIGN=/home/cody/projects/printforge/.claude/skills/printforge-organic-quality-campaign
mkdir -p "$CAMPAIGN/testset" "$CAMPAIGN/runs"
find "$CAMPAIGN/testset" -maxdepth 1 -type f \( -iname '*.png' -o -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.webp' \) | sort
```

Convention: use six front-facing object photos with uncluttered backgrounds, stable filenames like `01_toy.png` through `06_tool.png`, and a target size recorded beside the results table. Keep the set unchanged for the whole campaign.

Collect mesh metrics for one STL:

```sh
cd /home/cody/projects/printforge
export STL=/path/to/output.stl
export TARGET_MM=80
UV_CACHE_DIR=/tmp/uv-cache-pf uv run --with trimesh --with numpy --with scipy --with shapely --with rtree --with networkx python - <<'PY' "$STL" "$TARGET_MM"
import json
import sys
import trimesh
from parts import floating_starts

path = sys.argv[1]
target = float(sys.argv[2])
mesh = trimesh.load_mesh(path)
components = mesh.split(only_watertight=False)
if len(components) == 0:
    components = [mesh]
maxdim = float(max(mesh.extents))
print(json.dumps({
    "watertight": bool(mesh.is_watertight),
    "components": len(components),
    "triangles": int(len(mesh.faces)),
    "floating_starts": len(floating_starts(path)),
    "bbox_mm": [round(float(v), 2) for v in mesh.extents],
    "target_mm": target,
    "target_error_mm": round(abs(maxdim - target), 2),
}, sort_keys=True))
PY
```

The metric command above was executed during authorship on a synthetic box with `UV_CACHE_DIR=/tmp/uv-cache-pf`; it returned watertight true, one component, 12 triangles, and zero floating starts. If `uv` fails on `/home/cody/.cache/uv`, set `UV_CACHE_DIR` exactly as shown.

If Ollama is holding the GPU, replicate the app's `_free_gpu()` only inside an approved GPU window. WARNING: unloading Ollama also frees the local qwen brain that serves as the codex rate-limit fallback (and other lab automation) — schedule this when nothing depends on local qwen. This command may unload local Ollama models and was not executed during skill authorship:

```sh
cd /home/cody/projects/printforge
organic/.venv/bin/python - <<'PY'
import httpx

with httpx.Client(timeout=15) as client:
    ps = client.get("http://127.0.0.1:11434/api/ps").json()
    for model in ps.get("models", []):
        client.post(
            "http://127.0.0.1:11434/api/generate",
            json={"model": model["name"], "keep_alive": 0},
        )
PY
```

Collect inference time and peak VRAM for one scheduled GPU run. This command invokes the GPU runner and was not executed during skill authorship:

```sh
cd /home/cody/projects/printforge
export IMAGE=/path/to/testset/01_toy.png
export OUT=/tmp/pf-organic-01.stl
export TARGET_MM=80
export STEPS=30
export MODEL=tencent/Hunyuan3D-2mini
export SUBFOLDER=hunyuan3d-dit-v2-mini
export RUN_DIR=/tmp/pf-organic-run-$(date +%Y%m%d-%H%M%S)
mkdir -p "$RUN_DIR"

nvidia-smi --query-gpu=timestamp,index,memory.used --format=csv,noheader,nounits -lms 200 >"$RUN_DIR/vram.csv" 2>"$RUN_DIR/vram.err" &
VRAM_PID=$!
START=$(date +%s)
organic/.venv/bin/python organic/generate.py \
  --image "$IMAGE" --out "$OUT" --target-mm "$TARGET_MM" \
  --steps "$STEPS" --model "$MODEL" --subfolder "$SUBFOLDER" \
  >"$RUN_DIR/generate.out" 2>"$RUN_DIR/generate.err"
STATUS=$?
END=$(date +%s)
kill "$VRAM_PID" 2>/dev/null || true
wait "$VRAM_PID" 2>/dev/null || true
printf 'status=%s inference_seconds=%s\n' "$STATUS" "$((END - START))"
awk -F, 'BEGIN{m=0} {gsub(/ /,"",$3); if (($3+0)>m) m=$3+0} END{print "peak_vram_mb=" m}' "$RUN_DIR/vram.csv"
```

`nvidia-smi` exists on the host, but in this sandbox it could not communicate with the NVIDIA driver; mark peak VRAM as `not executed here -- re-verify` until collected on the live GPU.

Results table template:

| phase | backend | model | subfolder | steps | target_mm | image | watertight | components | triangles | floating_starts | bbox_mm | target_error_mm | inference_seconds | peak_vram_mb | notes | decision |
| --- | --- | --- | --- | ---: | ---: | --- | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- | --- |
| P0 | hunyuan | tencent/Hunyuan3D-2mini | hunyuan3d-dit-v2-mini | 30 | 80 | 01_toy.png |  |  |  |  |  |  |  |  |  |  |

## P0: Baseline the Current Pipeline

Goal: make the present pipeline measurable before changing anything.

Run all six images with default args:

```sh
cd /home/cody/projects/printforge
export CAMPAIGN=/home/cody/projects/printforge/.claude/skills/printforge-organic-quality-campaign
export TARGET_MM=80
export STEPS=30
export MODEL=tencent/Hunyuan3D-2mini
export SUBFOLDER=hunyuan3d-dit-v2-mini
mkdir -p "$CAMPAIGN/runs/P0-baseline"
# For each image, run the timed GPU command from Metrics First, then run the mesh metric command.
```

Gate:

| Observation | Branch |
| --- | --- |
| All six are watertight, `floating_starts=0`, components are explainable, and target scale is close | Proceed to P1 |
| Any output is not watertight | Do postprocess debugging first; do not swap models |
| Triangle count exceeds 400000 | Debug decimation or runner output; do not promote |
| Scale error exceeds 1mm | Debug `postprocess()` scale/floor/base-slice path |
| Time is far from about 90s/image at 30 steps on the 3090 | Check GPU contention, Ollama residency, NixOS generation, and `ORGANIC_LIBS` before judging model quality |

## P1: Cheap Knobs Before New Backends

Try one variable at a time against the P0 scorecard.

| Knob | Command delta | Expected result | Gate |
| --- | --- | --- | --- |
| Steps 30 to 50 | `--steps 50` | Time grows roughly linearly; geometry may improve | Keep only if scorecard or owner notes show a measurable quality delta; otherwise abandon |
| Full Hunyuan3D-2 shape model | `--model tencent/Hunyuan3D-2 --subfolder hunyuan3d-dit-v2-0` | Candidate quality lift; official Hunyuan docs say shape generation uses about 6GB VRAM, but confirm on this host before relying | Do not download until license, VRAM, and disk/cache path are confirmed |
| Target size | `--target-mm 50`, `80`, `120` | Smaller targets hide detail; larger targets expose holes/floating regions | Promote no default size change without fixed-test-set evidence |

Before trying full Hunyuan, confirm the current cache and download path:

```sh
cd /home/cody/projects/printforge
find /home/cody/.cache/hy3dgen -maxdepth 3 -type d | sed -n '1,40p'
grep -n 'cache\\|HuggingFace\\|hy3dgen' organic/setup.sh run.sh
```

As of 2026-07-06, `organic/setup.sh` still comments that weights download into `~/.cache/huggingface`, but the installed mini weights are visible under `/home/cody/.cache/hy3dgen/tencent/Hunyuan3D-2mini/hunyuan3d-dit-v2-mini`.

## P2: Candidate Backend Evaluation

Keep the existing runner as the default until a candidate beats it on the fixed scorecard. Every candidate must satisfy these obligations before any integration branch:

- License: confirm code and weights license allows this self-hosted use. Hunyuan3D-2 uses a Tencent community license, not plain MIT; TRELLIS code is MIT; other claims below are unverified -- confirm before relying.
- VRAM: prove the model fits in 24GB with `nvidia-smi` peak sampling before downloading full weights into the production cache.
- Weights: prove files are actually downloadable on this network.
- Shape-only compatibility: output a `trimesh.Trimesh` or file that can pass the same `postprocess()` and metrics.
- Isolation: use a sibling venv under `organic/` and a CLI-selected branch in `generate.py`; do not edit the working Hunyuan path until promotion.

Candidate ranking as of 2026-07-06:

| Rank | Candidate | Why evaluate | Pre-checks |
| ---: | --- | --- | --- |
| 1 | Sparc3D | Paper claims sparse deformable marching-cubes style high-resolution geometry and arbitrary topology; promising for print shape fidelity | Code, weights, license, and VRAM are unverified -- confirm before relying |
| 2 | TRELLIS / TRELLIS.2 | Public TRELLIS repo has image-to-3D pretrained `microsoft/TRELLIS-image-large` on Hugging Face; TRELLIS.2 appears in 2026 papers but a stable public repo/weights path is unverified -- confirm before relying | Confirm whether evaluating TRELLIS-image-large or a real TRELLIS.2 release; then license, deps, VRAM, mesh export |
| 3 | Hi3DGen | Paper proposes image-to-normal then normal-to-geometry for detail recovery | Code, weights, license, and VRAM are unverified -- confirm before relying |
| 4 | TripoSR | Public repo reports MIT license and about 6GB VRAM for default single-image inference; fast baseline candidate | Confirm mesh quality is competitive; likely lower-detail than diffusion backends |

Hugging Face CDN trap:

```sh
getent ahosts us.aws.cdn.hf.co | sed -n '1,8p'
cd /home/cody/projects/printforge
curl -fsS --connect-timeout 10 'https://cloudflare-dns.com/dns-query?name=us.aws.cdn.hf.co&type=A' \
  -H 'accept: application/dns-json' \
  | organic/.venv/bin/python -c 'import json,sys; d=json.load(sys.stdin); print(next((a["data"] for a in d.get("Answer", []) if a.get("type")==1), "NO_A"))'
```

If `getent` returns `0.0.0.0` or `::`, the lab DNS/AdGuard path is blocking `us.aws.cdn.hf.co`. Work around it by asking the owner to whitelist the domain, or use the DoH-resolved IP with `curl --resolve` for the specific file URL:

```sh
export HF_CDN_IP=<ip-from-doh-command>
curl -L --fail --connect-timeout 20 \
  --resolve us.aws.cdn.hf.co:443:"$HF_CDN_IP" \
  -o /tmp/model-file \
  'https://us.aws.cdn.hf.co/path/from/huggingface/redirect'
```

Do not paste guessed Hugging Face CDN paths. First obtain the real redirected URL with `curl -I -L` against the model file.

Integration recipe for a new backend (NOTE: `organic/generate.py` is the LIVE
runner behind `POST /organic` — even a defaults-preserving edit to it is a
high-risk live-pipeline change; route the edit itself through
printforge-change-control BEFORE step 2, not only at promotion in step 6):

1. Create a sibling environment such as `organic/.venv-trellis` or `organic/.venv-sparc3d`.
2. Add a backend selector to `organic/generate.py`, for example `--backend hunyuan|trellis`, while preserving current Hunyuan defaults.
3. Route candidate code through a new branch after argument parsing. Return a `trimesh.Trimesh` or exported mesh, then call the same `postprocess(mesh, target_mm)`.
4. Keep old CLI flags working exactly: `--image`, `--out`, `--target-mm`, `--steps`, `--model`, `--subfolder`.
5. Run P0 metrics against the candidate. If it loses on watertightness, floating starts, target scale, or VRAM, stop.
6. Only after a candidate beats default on the scorecard, open a `printforge-change-control` promotion with before/after table, GPU-time cost, license notes, and README `Later` update.

## P3: Hybrid Organic + Parametric

Verified flow:

1. Organic generation returns an STL and `_register_mesh()` immediately stores it under `uploads/<id>.stl`.
2. `_register_mesh()` records `bounds_min`, `bounds_max`, `tris`, `bodies`, `watertight`, and five horizontal cross-sections at 10%, 30%, 50%, 70%, and 90% height.
3. The returned wrapper OpenSCAD imports the registered mesh through `base_mesh_path`.
4. A normal Generate/Refine request can then use `import(base_mesh_path, convexity=10);` and the mesh note tells the LLM to place features only where cross-sections show material.

Hybrid rules:

- Use `prompts.py` rule 14, not stale rule numbers.
- Never use `scale([1,1,big]) import(...)` to clip to a footprint. That is banned by rule 14b because it stretches the bottom slice and collapses features into slivers.
- For raised text on vertical sides, use the verified rule 14f recipe. Do not re-derive rotation algebra.
- Prefer engraving over raised additions on irregular organic surfaces when the user allows it.
- Expect open quality questions: embossing on high-poly curved surfaces, decimation versus detail preservation, and whether 400k faces is too high for repeated parametric refines.
- Remember `PARAM_RE` only parses non-negative numeric defaults because the value alternation is `[\d.]+|"[^"]*"`. Negative defaults silently fail to become sliders; avoid negative top-level defaults in hybrid helper variables unless `printforge-change-control` changes parser behavior.

Promotion gate for hybrid work:

| Observation | Branch |
| --- | --- |
| Imported organic mesh remains watertight and `floating_starts=0` after additions | Continue |
| Emboss/inlay appears in straight-on render only | Inspect oblique renders; straight-on views hide low relief |
| Added feature disappears | Check whether union buried it inside the organic solid; move it into open air or engrave |
| Decimation erases desired detail | Run a decimation threshold experiment on the fixed test set before changing the 400k cap |

## Fenced Wrong Paths

Do not spend campaign time here:

| Wrong path | Reason |
| --- | --- |
| `pymeshlab` postprocess | It was deliberately patched out because it does not build cleanly here; use `trimesh` postprocess |
| Texture/paint pipelines | PrintForge organic mode is shape-only by design; color/material work belongs to later Bambu metadata research |
| Re-deriving Y-up to Z-up | Solved in `organic/generate.py:10-11` with X-axis rotation |
| Running while Ollama holds the GPU | Use the app path or replicate `_free_gpu()` before scheduled direct runner tests |
| Assuming the 3090 is always available | GPU access is a base-host fact, not guaranteed in non-base NixOS generations or sandboxed shells |
| Calling live `/organic` for benchmarks | It mutates uploads and competes with the collaborator; direct runner plus explicit result paths is the campaign path |
| Full-file rewrites of `generate.py` | Add narrow backend branches; do not risk breaking the working Hunyuan default |

## Promotion Protocol

A backend or knob change ships only through `printforge-change-control` with:

- Before/after scorecard for all six fixed images.
- Owner sign-off on GPU time, model license, cache/disk impact, and user-facing quality.
- Proof that watertightness, `floating_starts`, target scale, and VRAM fit are not worse.
- README `Later` section update: remove, refine, or keep the evaluated backend item honestly.
- Default remains Hunyuan3D-2mini until the promoted path is measured better and operationally safe.

## Provenance and Maintenance

Run these one-line checks when the repo or host drifts:

```sh
cd /home/cody/projects/printforge && nl -ba organic/generate.py | sed -n '1,80p'
cd /home/cody/projects/printforge && nl -ba app.py | sed -n '42,90p;592,686p;1250,1314p'
cd /home/cody/projects/printforge && nl -ba parts.py | sed -n '1,110p'
cd /home/cody/projects/printforge && nl -ba prompts.py | sed -n '40,96p'
cd /home/cody/projects/printforge && nl -ba README.md | sed -n '24,36p;118,126p'
cd /home/cody/projects/printforge && rg -n 'cu121|opencv-python-headless|pymeshlab|hy3dgen|cache|HuggingFace' organic/setup.sh run.sh
cd /home/cody/projects/printforge && find /home/cody/.cache/hy3dgen -maxdepth 3 -type d | sed -n '1,40p'
cd /home/cody/projects/printforge && curl -fsS http://127.0.0.1:8093/config
command -v nvidia-smi && timeout 1s nvidia-smi --query-gpu=timestamp,index,memory.used --format=csv,noheader,nounits -lms 200 || true
getent ahosts us.aws.cdn.hf.co | sed -n '1,8p'
cd /home/cody/projects/printforge && organic/.venv/bin/python - <<'PY'
import inspect
from hy3dgen.shapegen import Hunyuan3DDiTFlowMatchingPipeline
print(inspect.signature(Hunyuan3DDiTFlowMatchingPipeline.from_pretrained))
PY
```

External candidate facts must be re-verified before reliance: Hunyuan model zoo/VRAM/license, TRELLIS/TRELLIS.2 repo and model availability, Sparc3D code/weights/license/VRAM, Hi3DGen code/weights/license/VRAM, and TripoSR license/VRAM/quality. Treat every such claim as candidate intelligence until the campaign owner records dated links and local commands in the results table.
