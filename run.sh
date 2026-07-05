#!/usr/bin/env sh
# Run PrintForge on the host — required for the codex backend (codex CLI + auth
# live on the host, not in the container). openscad comes from nix, python from uv.
cd "$(dirname "$0")"
[ -f .env ] && . ./.env && export BAMBUDDY_API_KEY
export LLM_BACKEND="${LLM_BACKEND:-codex}"
exec nix shell nixpkgs#openscad-unstable --command \
  uv run --with fastapi --with uvicorn --with httpx --with trimesh --with numpy --with scipy \
         --with python-multipart --with networkx --with lxml \
  uvicorn app:app --host 0.0.0.0 --port 8093
