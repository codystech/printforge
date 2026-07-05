#!/usr/bin/env sh
# One-time setup for organic (image->mesh) generation: Hunyuan3D-2 shape pipeline.
# Weights (~4-6GB) download from HuggingFace on first generate into ~/.cache/huggingface.
set -e
cd "$(dirname "$0")"
uv venv .venv --python 3.11
. .venv/bin/activate
uv pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121
uv pip install trimesh numpy pillow scipy networkx rtree fast-simplification rembg onnxruntime
# hy3dgen: try PyPI, fall back to the Tencent repo
uv pip install hy3dgen || uv pip install "git+https://github.com/Tencent/Hunyuan3D-2.git"
# NixOS: cv2's GUI build wants libxcb; headless works everywhere
uv pip uninstall opencv-python 2>/dev/null || true
uv pip install opencv-python-headless
# pymeshlab needs a pile of system libs we don't have; we postprocess with trimesh instead
SG=.venv/lib/python3.11/site-packages/hy3dgen/shapegen/__init__.py
grep -q "except ImportError" "$SG" || sed -i 's/^from .postprocessors import .*/try:\n    from .postprocessors import FaceReducer, FloaterRemover, DegenerateFaceRemover, MeshSimplifier\nexcept ImportError:\n    pass/' "$SG"
echo "organic venv ready"
