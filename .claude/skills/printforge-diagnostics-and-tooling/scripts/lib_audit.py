#!/usr/bin/env python3
"""Audit PrintForge library metadata without modifying it.

Run from the repo root:
  uv run --with trimesh --with numpy --with scipy --with shapely --with rtree \
         --with networkx python \
         .claude/skills/printforge-diagnostics-and-tooling/scripts/lib_audit.py
"""
import argparse
import json
import sys
import time
from pathlib import Path


BASE_FIELDS = ("id", "name", "prompt", "created")
ENRICHED_FIELDS = ("qa", "backend", "report")


def short(value: object, width: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= width else text[:width - 1] + "…"


def date_from_epoch(value: object) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(value)))
    except Exception:
        return "bad-date"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--library", type=Path, default=Path("library"),
                    help="library directory, relative to repo root by default")
    ap.add_argument("--limit", type=int, default=0,
                    help="limit rows printed; 0 means all")
    args = ap.parse_args()

    if not args.library.exists():
        print(f"no such library directory: {args.library}", file=sys.stderr)
        return 2

    rows = []
    errors = []
    for mdir in args.library.iterdir():
        meta_file = mdir / "meta.json"
        if not meta_file.exists():
            continue
        try:
            meta = json.loads(meta_file.read_text())
        except Exception as exc:
            errors.append(f"{mdir.name}: invalid JSON: {exc}")
            continue
        report = meta.get("report") or {}
        missing = [k for k in (*BASE_FIELDS, *ENRICHED_FIELDS) if k not in meta]
        rows.append({
            "sort": float(meta.get("created") or 0),
            "id": meta.get("id", mdir.name),
            "name": meta.get("name", ""),
            "date": date_from_epoch(meta.get("created")),
            "qa": meta.get("qa", "missing"),
            "backend": meta.get("backend", "missing"),
            "rating": meta.get("rating", ""),
            "parts": report.get("parts", "missing"),
            "missing": ",".join(missing),
        })

    rows.sort(key=lambda r: -r["sort"])
    if args.limit > 0:
        rows = rows[:args.limit]

    print("id           created     qa          backend              rating parts name                                     missing")
    print("------------ ----------- ----------- -------------------- ------ ----- ---------------------------------------- ----------------")
    for r in rows:
        print(f"{short(r['id'], 12):12} {r['date']:11} {short(r['qa'], 11):11} "
              f"{short(r['backend'], 20):20} {short(r['rating'], 6):6} "
              f"{short(r['parts'], 5):5} {short(r['name'], 40):40} "
              f"{short(r['missing'], 80)}")
    if errors:
        print("\nmetadata errors:")
        for e in errors:
            print(f"  {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
