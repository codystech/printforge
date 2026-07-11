"""Guided locked-requirement schema: normalization, deterministic verification,
and human-readable rendering for the Training Lab.

This module is intentionally dependency-free (stdlib only) so it can be unit
tested without importing PrintForge's heavy generation stack.  ``app.py`` wires
:func:`verify_requirements` into the isolated lab evaluation adapter and
:func:`requirements_prompt_block` into the generation prompts.

Three requirement shapes are accepted for backward compatibility:

* plain strings (legacy/demo runs)           -> a non-enforceable note
* ``{"type": "module"|"parameter"|"literal", "name", "value"}`` (v1 locks)
* ``{"category", "type", "severity", "value", "label"}`` (guided builder v2)

Only what can be proved deterministically from the rendered candidate (its
bounding box, watertightness, connected-part count, floating regions, and the
SCAD source text) triggers a hard failure.  Anything the pipeline cannot measure
is reported as *unverified* and enforced softly through the generation prompt and
the independent AI review — it never auto-rejects a candidate, because a false
rejection is worse than a missed one here.
"""

from __future__ import annotations

import re
from typing import Any

# Severity levels, most-binding first. ``hard_lock`` and ``forbidden`` violations
# reject a candidate; the rest only inform scoring.
SEVERITIES = ("hard_lock", "required", "preferred", "avoid", "forbidden")
REJECTING_SEVERITIES = {"hard_lock", "forbidden"}
DEFAULT_SEVERITY = "hard_lock"

# Legacy v1 lock types the deterministic diff already understood.
LEGACY_TYPES = {"module", "parameter", "literal"}

_SIZE_TOLERANCE_MM = 0.5  # rendering + measurement slack for dimension checks


def _module_block(scad: str, name: str) -> str | None:
    """Extract ``module <name>(...) { ... }`` by brace counting (mirrors app.py)."""
    match = re.search(rf"module\s+{re.escape(name)}\s*\(", scad)
    if not match:
        return None
    start = scad.find("{", match.start())
    if start < 0:
        return None
    depth = 0
    for index in range(start, len(scad)):
        char = scad[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return scad[match.start():index + 1]
    return None


def normalize_requirement(raw: Any) -> dict | None:
    """Coerce any accepted shape into the canonical guided dict, or ``None``."""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        return {
            "category": "custom", "type": "custom_note", "severity": DEFAULT_SEVERITY,
            "label": text, "value": {"text": text}, "_legacy": "string",
        }
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    severity = raw.get("severity")
    if severity not in SEVERITIES:
        severity = DEFAULT_SEVERITY
    if kind in LEGACY_TYPES:
        # Preserve v1 locks verbatim; they carry name/value the diff needs.
        item = dict(raw)
        item["severity"] = severity
        item.setdefault("category", "legacy")
        item.setdefault("label", raw.get("name") or kind)
        return item
    return {
        "id": raw.get("id"),
        "category": raw.get("category", "custom"),
        "type": kind or "custom_note",
        "severity": severity,
        "label": raw.get("label") or kind or "requirement",
        "value": raw.get("value", {}),
        **({"name": raw["name"]} if "name" in raw else {}),
    }


def normalize_requirements(raw_list: Any) -> list[dict]:
    if not isinstance(raw_list, (list, tuple)):
        return []
    out = []
    for raw in raw_list:
        item = normalize_requirement(raw)
        if item is not None:
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Value extraction helpers (tolerant of the several shapes the UI may send)
# ---------------------------------------------------------------------------

def _num(value: Any) -> float | None:
    try:
        if isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _value_number(req: dict) -> float | None:
    value = req.get("value")
    if isinstance(value, dict):
        for key in ("n", "value", "mm", "amount"):
            if key in value:
                return _num(value[key])
        return None
    return _num(value)


def _value_dims(req: dict) -> tuple[float, float, float] | None:
    value = req.get("value")
    if isinstance(value, dict):
        dims = [_num(value.get(axis)) for axis in ("x", "y", "z")]
        if all(d is not None for d in dims):
            return dims[0], dims[1], dims[2]  # type: ignore[return-value]
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        dims = [_num(value[i]) for i in range(3)]
        if all(d is not None for d in dims):
            return dims[0], dims[1], dims[2]  # type: ignore[return-value]
    return None


def _value_texts(req: dict) -> list[str]:
    value = req.get("value")
    out: list[str] = []
    if isinstance(value, dict):
        if value.get("text"):
            out.append(str(value["text"]))
        for item in value.get("items", []) or []:
            if str(item).strip():
                out.append(str(item))
    elif isinstance(value, str) and value.strip():
        out.append(value)
    if not out and req.get("name"):
        out.append(str(req["name"]))
    return out


def _value_items(req: dict) -> list[str]:
    value = req.get("value")
    if isinstance(value, dict):
        return [str(i) for i in (value.get("items") or []) if str(i).strip()]
    if isinstance(value, (list, tuple)):
        return [str(i) for i in value if str(i).strip()]
    if isinstance(value, str) and value.strip():
        return [value]
    return []


def _value_choice(req: dict) -> str:
    value = req.get("value")
    if isinstance(value, dict):
        return str(value.get("choice") or value.get("text") or "").strip()
    if isinstance(value, str):
        return value.strip()
    return ""


# ---------------------------------------------------------------------------
# Deterministic verification
# ---------------------------------------------------------------------------

def _finding(code: str, severity: str, message: str) -> dict:
    return {"issue_type": code, "severity": severity, "message": message,
            "source": "constraint monitor"}


def _check(req: dict, parent_scad: str, candidate_scad: str,
           report: dict, floats: list) -> tuple[bool | None, str]:
    """Return (satisfied, detail).  ``None`` = not deterministically checkable."""
    kind = req.get("type")
    label = req.get("label") or kind
    bbox = report.get("bbox_mm") if isinstance(report, dict) else None

    # --- dimensions -------------------------------------------------------
    if kind == "maximum_overall_size":
        dims = _value_dims(req)
        if not dims or not bbox:
            return None, "no target size or measured bbox"
        over = [f"{axis}:{got:.1f}>{lim:.1f}" for axis, got, lim, in
                zip("XYZ", bbox, dims) if got > lim + _SIZE_TOLERANCE_MM]
        return (not over), (f"exceeds max on {', '.join(over)}" if over
                            else f"within {dims[0]}×{dims[1]}×{dims[2]}mm")
    if kind == "exact_overall_size":
        dims = _value_dims(req)
        if not dims or not bbox:
            return None, "no target size or measured bbox"
        off = [f"{axis}:{got:.1f}≠{want:.1f}" for axis, got, want in
               zip("XYZ", bbox, dims) if abs(got - want) > _SIZE_TOLERANCE_MM]
        return (not off), (f"off target on {', '.join(off)}" if off
                           else "matches exact size")
    if kind in ("must_fit_selected_printer", "must_fit_build_volume"):
        if not isinstance(report, dict) or "bed_fit" not in report:
            return None, "no bed-fit report"
        ok = report.get("bed_fit") == "ok"
        return ok, (report.get("bed_fit") or "unknown")

    # --- text -------------------------------------------------------------
    if kind in ("required_text", "preserve_exact_spelling"):
        texts = _value_texts(req)
        if not texts:
            return None, "no text specified"
        missing = [t for t in texts if t not in candidate_scad]
        return (not missing), (f"missing {missing!r}" if missing else "present in source")

    # --- printability -----------------------------------------------------
    if kind == "no_floating_geometry":
        return (not floats), (f"{len(floats)} floating region(s)" if floats else "none detected")
    if kind == "watertight_parts_required":
        if not isinstance(report, dict) or "watertight" not in report:
            return None, "no watertight report"
        return bool(report.get("watertight")), ("watertight" if report.get("watertight") else "not watertight")

    # --- parts / assembly -------------------------------------------------
    if kind in ("maximum_part_count", "exact_part_count", "single_piece_required"):
        parts = report.get("parts") if isinstance(report, dict) else None
        if parts is None:
            return None, "no part count"
        if kind == "single_piece_required":
            return (parts == 1), f"{parts} connected part(s)"
        want = 1 if kind == "single_piece_required" else _value_number(req)
        if want is None:
            return None, "no target count"
        want = int(want)
        ok = parts <= want if kind == "maximum_part_count" else parts == want
        return ok, f"{parts} part(s) vs target {want}"

    # --- legacy v1 locks --------------------------------------------------
    if kind == "module" and req.get("name"):
        before, after = _module_block(parent_scad, req["name"]), _module_block(candidate_scad, req["name"])
        ok = after is not None and (
            not parent_scad.strip()
            or (before is not None and re.sub(r"\s+", "", before) == re.sub(r"\s+", "", after))
        )
        return ok, ("module preserved" if ok else "module changed/missing")
    if kind == "parameter" and req.get("name"):
        pattern = re.compile(rf"^\s*{re.escape(req['name'])}\s*=\s*([^;]+);", re.MULTILINE)
        before, after = pattern.search(parent_scad), pattern.search(candidate_scad)
        expected = req.get("value")
        ok = bool(after and (
            (not parent_scad.strip() and (expected is None or after.group(1).strip().strip('"') == str(expected).strip().strip('"')))
            or (before and before.group(1).strip() == after.group(1).strip())
        ))
        return ok, ("parameter preserved" if ok else "parameter changed/missing")
    if kind == "literal":
        literal = req.get("value")
        if literal is None:
            return None, "no literal value"
        literal = str(literal)
        ok = literal in candidate_scad and (not parent_scad.strip() or literal in parent_scad)
        return ok, ("literal present" if ok else "literal missing")

    # Everything else is not deterministically measurable by this pipeline.
    return None, "enforced via generation prompt + AI review"


def _failure_code(kind: str) -> str:
    if kind in ("maximum_overall_size", "exact_overall_size",
                "must_fit_selected_printer", "must_fit_build_volume"):
        return "build_volume_overflow"
    if kind in ("required_text", "preserve_exact_spelling"):
        return "required_feature_missing"
    return "broken_hard_lock"


def verify_requirements(parent_scad: str, candidate_scad: str, report: dict,
                        floats: list, requirements: Any) -> tuple[list[dict], list[str]]:
    """Deterministically check what we can; return (findings, hard_failure_codes).

    ``findings`` includes info-level notes for unverifiable requirements so the
    Constraint Monitor can show them honestly.  ``hard_failure_codes`` contains a
    code only when a rejecting-severity requirement is *proven* violated.
    """
    findings: list[dict] = []
    failures: list[str] = []
    report = report if isinstance(report, dict) else {}
    floats = floats or []
    for req in normalize_requirements(requirements):
        satisfied, detail = _check(req, parent_scad or "", candidate_scad or "", report, floats)
        label = req.get("label") or req.get("type")
        severity = req.get("severity", DEFAULT_SEVERITY)
        if satisfied is False:
            rejecting = severity in REJECTING_SEVERITIES
            code = _failure_code(req.get("type", ""))
            findings.append(_finding(
                code if rejecting else "requirement_not_met",
                "critical" if rejecting else "warning",
                f"{label}: {detail}" + ("" if rejecting else " (soft — scoring only)"),
            ))
            if rejecting:
                failures.append(code)
        elif satisfied is None:
            findings.append(_finding(
                "constraint_unverified", "info",
                f"{label}: {detail}",
            ))
        # satisfied is True -> no finding (keep the red-team list quiet)
    return findings, failures


# ---------------------------------------------------------------------------
# Human-readable rendering (chips, prompt block)
# ---------------------------------------------------------------------------

def summarize_value(req: dict) -> str:
    kind = req.get("type")
    dims = _value_dims(req)
    if dims:
        return f"{dims[0]:g} × {dims[1]:g} × {dims[2]:g} mm"
    number = _value_number(req)
    if number is not None:
        unit = ""
        if isinstance(req.get("value"), dict):
            unit = str(req["value"].get("unit") or "")
        if not unit:
            unit = "" if kind in ("maximum_part_count", "exact_part_count") else "mm"
        return f"{number:g}{(' ' + unit) if unit else ''}".strip()
    items = _value_items(req)
    if items:
        return " + ".join(items)
    texts = _value_texts(req)
    if texts:
        return ", ".join(texts)
    choice = _value_choice(req)
    if choice:
        return choice
    return ""


def summarize_requirement(req: dict) -> str:
    req = normalize_requirement(req) or {}
    label = req.get("label") or req.get("type") or "requirement"
    value = summarize_value(req)
    return f"{label}: {value}" if value else str(label)


def requirements_prompt_block(requirements: Any) -> str:
    """Readable, severity-grouped block for the generation prompt (A/B identical)."""
    reqs = normalize_requirements(requirements)
    if not reqs:
        return "(none specified)"
    lines = []
    for req in reqs:
        severity = req.get("severity", DEFAULT_SEVERITY).replace("_", " ").upper()
        lines.append(f"- [{severity}] {summarize_requirement(req)}")
    return "\n".join(lines)
