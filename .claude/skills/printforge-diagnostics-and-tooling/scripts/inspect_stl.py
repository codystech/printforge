#!/usr/bin/env python3
"""Measure an STL: bbox, watertightness, volume/weight, connected parts, floating starts.

Run from the repo root so `import parts` resolves:
  cd /home/cody/projects/printforge
  uv run --with trimesh --with numpy --with scipy --with shapely --with rtree \
         --with networkx python \
         .claude/skills/printforge-diagnostics-and-tooling/scripts/inspect_stl.py \
         uploads/<id>.stl

Read-only. Prints a human report; never writes files.
"""
import argparse
import sys
from pathlib import Path

import trimesh

# Make the repo root importable no matter the cwd (script lives 4 dirs deep under it),
# so we measure with the server's OWN detector instead of a reimplementation.
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))
from parts import floating_starts

PLA_DENSITY = 1.24  # g/cm^3, matches print_report()'s default in app.py


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("stl", type=Path, help="path to an .stl file")
    ap.add_argument("--density", type=float, default=PLA_DENSITY,
                    help="filament density g/cm^3 (default 1.24 = PLA)")
    args = ap.parse_args()

    if not args.stl.exists():
        print(f"no such file: {args.stl}", file=sys.stderr)
        return 2

    m = trimesh.load_mesh(str(args.stl))
    ext = m.extents
    watertight = bool(m.is_watertight)
    is_vol = bool(m.is_volume)
    parts = len(m.split(only_watertight=False))

    print(f"file:            {args.stl}")
    print(f"triangles:       {len(m.faces):,}")
    print(f"vertices:        {len(m.vertices):,}")
    print(f"bbox mm (XxYxZ): {ext[0]:.1f} x {ext[1]:.1f} x {ext[2]:.1f}")
    print(f"origin..max:     [{m.bounds[0][0]:.1f},{m.bounds[0][1]:.1f},{m.bounds[0][2]:.1f}]"
          f" .. [{m.bounds[1][0]:.1f},{m.bounds[1][1]:.1f},{m.bounds[1][2]:.1f}]")
    print(f"watertight:      {watertight}"
          + ("" if watertight else "   <-- volume/weight below are UNRELIABLE"))
    print(f"is_volume:       {is_vol}")
    print(f"connected parts: {parts}"
          + ("   <-- >1 means disconnected islands (HARD BLOCK unless intended)"
             if parts > 1 else ""))

    if is_vol:
        vol_cm3 = abs(m.volume) / 1000.0
        print(f"volume:          {vol_cm3:.2f} cm^3")
        print(f"est weight:      {vol_cm3 * args.density:.1f} g "
              f"(solid, density {args.density}; infill/walls make the real print lighter)")
    else:
        print("volume:          n/a (not a closed volume — weight cannot be estimated)")

    floats = floating_starts(str(args.stl))
    print(f"\nfloating_starts findings (>4mm^2 mid-air starts): {len(floats)}")
    if floats:
        print("  each is a feature that appears with nothing beneath it when sliced "
              "bottom-up; slicers reject these unless supported.")
        for f in floats:
            print(f"  z={f['z']:>6} at ({f['x']:>6},{f['y']:>6})  ~{f['area']} mm^2")
    else:
        print("  none above the 4mm^2 report threshold "
              "(sub-4mm^2 arcs/columns are spot-support territory, not reported).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
