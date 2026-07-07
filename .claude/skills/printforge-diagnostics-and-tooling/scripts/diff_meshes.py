#!/usr/bin/env python3
"""Compare two STL meshes using PrintForge's triangle-centroid diff heuristic.

Run from the repo root:
  uv run --with trimesh --with numpy --with scipy --with shapely --with rtree \
         --with networkx python \
         .claude/skills/printforge-diagnostics-and-tooling/scripts/diff_meshes.py \
         /tmp/base.stl /tmp/new.stl

Read-only. Prints clustered added/removed surface regions.
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import trimesh
from scipy import ndimage


def cluster_bboxes(pts: np.ndarray, cell: float = 8.0,
                   max_regions: int = 6) -> list[tuple[int, np.ndarray, np.ndarray]]:
    """Group points into spatial clusters via an 8mm voxel grid."""
    if len(pts) == 0:
        return []
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
    return sorted(out, key=lambda t: -t[0])[:max_regions]


def fmt_box(mn: np.ndarray, mx: np.ndarray) -> str:
    return (f"x {mn[0]:.1f}..{mx[0]:.1f}, "
            f"y {mn[1]:.1f}..{mx[1]:.1f}, "
            f"z {mn[2]:.1f}..{mx[2]:.1f}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base_stl", type=Path)
    ap.add_argument("new_stl", type=Path)
    ap.add_argument("--round-mm", type=float, default=0.1,
                    help="centroid rounding grid in mm (default 0.1)")
    args = ap.parse_args()

    for p in (args.base_stl, args.new_stl):
        if not p.exists():
            print(f"no such file: {p}", file=sys.stderr)
            return 2

    base = trimesh.load_mesh(args.base_stl)
    new = trimesh.load_mesh(args.new_stl)
    decimals = max(0, int(round(-np.log10(args.round_mm))))
    bc = np.round(base.triangles_center, decimals)
    nc = np.round(new.triangles_center, decimals)
    bset, nset = set(map(tuple, bc)), set(map(tuple, nc))
    added = new.triangles_center[[tuple(c) not in bset for c in nc]]
    removed = base.triangles_center[[tuple(c) not in nset for c in bc]]

    print(f"base: {args.base_stl}  triangles={len(base.faces):,}")
    print(f"new:  {args.new_stl}  triangles={len(new.faces):,}")
    print(f"centroid grid: {args.round_mm:g} mm")
    print(f"added centroids:   {len(added):,} ({len(added) / max(1, len(nc)):.1%} of new)")
    print(f"removed centroids: {len(removed):,} ({len(removed) / max(1, len(bc)):.1%} of base)")

    print("\nadded regions:")
    regions = cluster_bboxes(added)
    if not regions:
        print("  none above the 8-triangle cluster threshold")
    for i, (count, mn, mx) in enumerate(regions, 1):
        print(f"  {i}. {count:,} triangles; {fmt_box(mn, mx)}")

    print("\nremoved regions:")
    rregions = cluster_bboxes(removed)
    if not rregions:
        print("  none above the 8-triangle cluster threshold")
    for i, (count, mn, mx) in enumerate(rregions, 1):
        print(f"  {i}. {count:,} triangles; {fmt_box(mn, mx)}")

    print("\ninterpretation:")
    if len(added) == 0:
        print("  no added surface was detected; a requested new feature may be missing "
              "or buried inside an existing solid.")
    elif not regions:
        print("  added surface exists only as tiny scattered fragments; inspect for scraps.")
    else:
        print("  match each requested added element to an added-region bbox; no region "
              "usually means the element is buried or absent.")
    if len(removed) > 0.02 * max(1, len(bc)):
        print("  more than 2% of base surface disappeared; confirm the user asked for "
              "a cut/engrave/reshape before treating this as acceptable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
