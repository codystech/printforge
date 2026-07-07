#!/usr/bin/env python3
"""Render the PrintForge oblique inspection view set for an OpenSCAD file.

Run from the repo root:
  uv run --with trimesh --with numpy --with scipy --with shapely --with rtree \
         --with networkx python \
         .claude/skills/printforge-diagnostics-and-tooling/scripts/render_views.py \
         library/<id>/model.scad /tmp/printforge-views

Writes PNG files only to the requested output directory.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_OPENSCAD_ARGS = "--enable=textmetrics --enable=manifold"

VIEWS = [
    # tag, camera, projection, imgsize
    ("iso", None, None, "1000,750"),
    ("oblique_025", "0,0,0,70,0,25,340", "p", "1000,750"),
    ("oblique_205", "0,0,0,70,0,205,340", "p", "1000,750"),
    ("top_ortho", "0,0,0,0,0,0,340", "o", "1000,750"),
]


def render(scad: Path, out_png: Path, camera: str | None,
           projection: str | None, imgsize: str) -> tuple[bool, str]:
    openscad_args = os.environ.get("OPENSCAD_ARGS", DEFAULT_OPENSCAD_ARGS).split()
    env = os.environ.copy()
    env.setdefault("XDG_CACHE_HOME", "/tmp/printforge-xdg-cache")
    cmd = [
        "nix", "shell", "nixpkgs#openscad-unstable", "--command",
        "openscad", *openscad_args,
        "-o", str(out_png),
        "--imgsize", imgsize,
        "--autocenter", "--viewall",
    ]
    if camera:
        cmd += ["--camera", camera]
    if projection:
        cmd += ["--projection", projection]
    cmd.append(str(scad))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env)
    ok = proc.returncode == 0 and out_png.exists()
    return ok, (proc.stderr or proc.stdout or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("scad", type=Path, help="path to model.scad")
    ap.add_argument("outdir", type=Path, help="directory for rendered PNGs")
    args = ap.parse_args()

    if not args.scad.exists():
        print(f"no such file: {args.scad}", file=sys.stderr)
        return 2
    args.outdir.mkdir(parents=True, exist_ok=True)

    print(f"source: {args.scad}")
    print(f"outdir: {args.outdir}")
    print("relief rule: use oblique perspective views for raised/engraved features; "
          "top/orthographic is only for footprint/layout checks.")

    failures = 0
    for tag, camera, projection, imgsize in VIEWS:
        out = args.outdir / f"{args.scad.stem}_{tag}.png"
        ok, msg = render(args.scad, out, camera, projection, imgsize)
        if ok:
            print(f"OK   {tag:<12} {out}")
        else:
            failures += 1
            tail = msg[-500:] if msg else "no OpenSCAD output"
            print(f"FAIL {tag:<12} {tail}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
