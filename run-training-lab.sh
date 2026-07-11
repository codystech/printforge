#!/usr/bin/env sh
# Isolated Training Lab service. LAN exposure is restricted by the host firewall;
# production remains separate on :8093.
set -eu
cd "$(dirname "$0")"
export PATH="$HOME/.local/npm/bin:$HOME/.local/bin:$PATH"
export LLM_BACKEND="${LLM_BACKEND:-codex}"
export PRINT_FORGE_TRAINING_LAB_ENABLED=true
export PRINT_FORGE_EVOLUTION_ENABLED=true
export PRINT_FORGE_MEMORY_LEARNING_ENABLED=true
export PRINT_FORGE_PHYSICAL_FEEDBACK_ENABLED=true
export PRINT_FORGE_ACTUAL_TRAINING_ENABLED=false
export PRINT_FORGE_TRAINING_ENABLED=false
export PRINT_FORGE_LAB_ONLY=true
export PRINT_FORGE_TRAINING_LAB_DATA_ROOT="${PRINT_FORGE_TRAINING_LAB_DATA_ROOT:-$PWD/training_lab_data}"

exec nix shell nixpkgs#openscad-unstable --command \
  uv run --with fastapi --with uvicorn --with httpx --with trimesh --with numpy --with scipy \
         --with python-multipart --with networkx --with lxml \
         --with shapely --with rtree --with manifold3d --with cascadio \
  uvicorn app:app --host 0.0.0.0 --port 8094
