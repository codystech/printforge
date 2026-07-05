#!/usr/bin/env sh
# One-time setup for organic (image->mesh) generation: Hunyuan3D-2 shape pipeline.
# Weights (~4-6GB) download from HuggingFace on first generate into ~/.cache/huggingface.
set -e
cd "$(dirname "$0")"
uv venv .venv --python 3.11
. .venv/bin/activate
uv pip install torch --index-url https://download.pytorch.org/whl/cu121
uv pip install trimesh numpy pillow scipy networkx rtree fast-simplification rembg onnxruntime
# hy3dgen: try PyPI, fall back to the Tencent repo
uv pip install hy3dgen || uv pip install "git+https://github.com/Tencent/Hunyuan3D-2.git"
echo "organic venv ready"
