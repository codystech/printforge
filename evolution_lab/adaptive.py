"""Adaptive mutation selection for the evolution engine.

Pure and unit-testable — no I/O, no production writes. The engine records each generation's
mutation outcomes (which mutation was tried, whether it won, and its score delta); this module
turns that accumulated history into per-strategy weights so future generations bias toward
mutations that have actually worked, while an exploration rate preserves diversity. This is the
"adaptive evolution" core: exploit what worked, explore to avoid local optima. Because the history
is read across runs, later lab runs inherit what earlier lab runs learned.
"""
from __future__ import annotations

import hashlib
import math
import random
import re

# The strategies the engine can try. Each is a controlled, single-axis edit-in-place; the DESIGNER
# adapter turns these into a minimal SCAD change. Kept small and interpretable on purpose.
MUTATION_CATALOG: list[dict] = [
    {"mutation_type": "fit_clearance", "parameter": "clearance", "mutated_value": "+0.05mm",
     "expected_benefit": "reduce binding", "reason": "controlled fit exploration"},
    {"mutation_type": "retention_geometry", "parameter": "retention", "mutated_value": "alternate profile",
     "expected_benefit": "improve retention", "reason": "controlled functional exploration"},
    {"mutation_type": "wall_thickness", "parameter": "wall", "mutated_value": "+0.4mm",
     "expected_benefit": "improve structural strength", "reason": "structural reinforcement"},
    {"mutation_type": "base_adhesion", "parameter": "base", "mutated_value": "wider, flatter footprint",
     "expected_benefit": "improve bed adhesion and anti-tip stability", "reason": "printability improvement"},
    {"mutation_type": "support_reduction", "parameter": "overhang", "mutated_value": "steepen undersides past 45deg",
     "expected_benefit": "remove the need for supports", "reason": "support-free printability"},
    {"mutation_type": "fillet_stress", "parameter": "fillet", "mutated_value": "add fillets at load-bearing joints",
     "expected_benefit": "reduce stress concentration", "reason": "structural durability"},
    {"mutation_type": "seat_floating", "parameter": "seating", "mutated_value": "embed mid-air features 2-3mm into base",
     "expected_benefit": "eliminate floating geometry", "reason": "printability repair"},
]

_BY_TYPE = {m["mutation_type"]: m for m in MUTATION_CATALOG}


def _scope_text(value: object) -> str:
    """Normalize human-entered metadata without retaining large prompt text."""

    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _scope_number(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    return format(number, ".12g") if math.isfinite(number) else ""


def adaptive_history_scope(run: dict) -> dict:
    """Build the stable, explicit context key used for cross-run learning.

    Existing-model runs are scoped to their source Library model. Spec-created
    runs use a digest of normalized validated-spec text, keeping the persisted
    outcome small while allowing a later run with the same design law to reuse
    it. Printer, nozzle, layer height, and material must also match exactly.
    """

    run = run if isinstance(run, dict) else {}
    profile = run.get("printer_profile") if isinstance(run.get("printer_profile"), dict) else {}
    material = run.get("material_profile") if isinstance(run.get("material_profile"), dict) else {}
    source_model_id = _scope_text(run.get("source_model_id"))
    if source_model_id:
        design = {"kind": "source_model", "value": source_model_id}
    else:
        normalized_spec = _scope_text(run.get("validated_spec"))
        design = {
            "kind": "validated_spec",
            "value": hashlib.sha256(normalized_spec.encode("utf-8")).hexdigest(),
        }
    return {
        "version": 1,
        "design": design,
        "printer_profile": _scope_text(profile.get("name")),
        "printer": _scope_text(profile.get("printer")),
        "nozzle_mm": _scope_number(profile.get("nozzle")),
        "layer_height_mm": _scope_number(material.get("layer_height") or profile.get("layer")),
        "material": _scope_text(material.get("material") or profile.get("material")),
    }


def mutation_weights(history: list[dict], prior: float = 1.0) -> dict[str, float]:
    """Laplace-smoothed success weight per mutation_type, nudged by mean score delta.

    history items: {"mutation_type": str, "success": bool, "score_delta": float}. Unseen strategies
    fall back to the neutral prior (0.5-ish) so they still get explored.
    """
    try:
        prior = float(prior)
    except (TypeError, ValueError):
        prior = 1.0
    if not math.isfinite(prior) or prior <= 0:
        prior = 1.0

    agg: dict[str, dict] = {}
    for h in history:
        if not isinstance(h, dict):
            continue
        k = h.get("mutation_type")
        if not isinstance(k, str) or k not in _BY_TYPE:
            continue
        try:
            delta = float(h.get("score_delta") or 0.0)
        except (TypeError, ValueError):
            delta = 0.0
        if not math.isfinite(delta):
            delta = 0.0
        # Engine scores are bounded to 0..100. Clamp persisted input to the
        # corresponding delta range so a corrupt/manual history record cannot
        # dominate every future selection.
        delta = max(-100.0, min(100.0, delta))
        status = str(h.get("candidate_status") or h.get("status") or "").casefold()
        # Legacy outcomes predate the explicit flag and remain usable. Once
        # present, however, only a real JSON boolean true counts as eligible;
        # malformed strings/lists fail closed.
        eligible = h.get("eligible") is True if "eligible" in h else True
        if h.get("hard_rejected") is True or status in {"failed", "rejected", "cancelled"}:
            eligible = False
        # An invalid attempt may have a deceptively high numeric score. Keep
        # its negative evidence, but never let a positive delta reward it.
        if not eligible:
            delta = min(0.0, delta)
        s = agg.setdefault(k, {"win": 0, "n": 0, "delta": 0.0})
        s["n"] += 1
        if eligible and h.get("success") is True:
            s["win"] += 1
        s["delta"] += delta
    weights: dict[str, float] = {}
    for m in MUTATION_CATALOG:
        k = m["mutation_type"]
        s = agg.get(k, {"win": 0, "n": 0, "delta": 0.0})
        rate = (s["win"] + prior) / (s["n"] + 2 * prior)          # smoothed win rate in (0,1)
        mean_delta = (s["delta"] / s["n"]) if s["n"] else 0.0     # average points gained
        weights[k] = max(0.01, rate + 0.02 * mean_delta)
    return weights


def select_mutations(history: list[dict], n: int = 2, exploration_rate: float = 0.15,
                     rng: random.Random | None = None) -> list[dict]:
    """Pick ``n`` distinct mutations. Each pick either explores (uniform) with probability
    ``exploration_rate`` or exploits (sampled proportional to success weight). Deterministic when
    passed a seeded ``rng``. Always returns at most len(MUTATION_CATALOG) items."""
    try:
        requested = max(0, int(n))
    except (TypeError, ValueError):
        requested = 0
    try:
        exploration_rate = float(exploration_rate)
    except (TypeError, ValueError):
        exploration_rate = 0.15
    if not math.isfinite(exploration_rate):
        exploration_rate = 0.15
    exploration_rate = min(1.0, max(0.0, exploration_rate))

    rng = rng or random
    weights = mutation_weights(history)
    pool = list(MUTATION_CATALOG)
    chosen: list[dict] = []
    while pool and len(chosen) < requested:
        if rng.random() < exploration_rate:
            pick = rng.choice(pool)
        else:
            total = sum(weights[m["mutation_type"]] for m in pool)
            r = rng.random() * total
            acc = 0.0
            pick = pool[-1]
            for m in pool:
                acc += weights[m["mutation_type"]]
                if r <= acc:
                    pick = m
                    break
        chosen.append(dict(pick))
        pool.remove(pick)
    return chosen


def outcome_record(mutation: dict, *, success: bool, score_delta: float, run_id: str,
                   candidate_id: str, generation: int, eligible: bool = True,
                   hard_rejected: bool = False, candidate_status: str = "",
                   selection_status: str = "",
                   adaptive_scope: dict | None = None) -> dict:
    """Shape one evolution-memory row from a scored candidate. The engine persists these across
    runs; mutation_weights() consumes them next time."""
    return {
        "mutation_type": (mutation or {}).get("mutation_type", "unknown"),
        "parameter": (mutation or {}).get("parameter"),
        "success": bool(success) and bool(eligible) and not bool(hard_rejected),
        "eligible": bool(eligible) and not bool(hard_rejected),
        "hard_rejected": bool(hard_rejected),
        "candidate_status": str(candidate_status or ""),
        "selection_status": str(selection_status or ""),
        "score_delta": float(score_delta),
        "run_id": run_id,
        "candidate_id": candidate_id,
        "generation": int(generation),
        "adaptive_scope": dict(adaptive_scope or {}),
    }


if __name__ == "__main__":  # ponytail: self-check the adaptive core without a framework
    rng = random.Random(0)
    # With no history every strategy is near-neutral; selection still returns n distinct picks.
    picks = select_mutations([], n=2, rng=rng)
    assert len(picks) == 2 and picks[0]["mutation_type"] != picks[1]["mutation_type"]

    # A strategy that consistently won gets a higher weight than one that consistently lost.
    hist = ([outcome_record(_BY_TYPE["wall_thickness"], success=True, score_delta=8, run_id="r",
                             candidate_id="c", generation=1) for _ in range(6)]
            + [outcome_record(_BY_TYPE["fit_clearance"], success=False, score_delta=-4, run_id="r",
                              candidate_id="c", generation=1) for _ in range(6)])
    w = mutation_weights(hist)
    assert w["wall_thickness"] > w["fit_clearance"], w

    # A hard-rejected score increase is audit evidence, never positive credit.
    rejected = outcome_record(_BY_TYPE["fit_clearance"], success=True, eligible=False,
                              hard_rejected=True, score_delta=49, run_id="r",
                              candidate_id="rejected", generation=1)
    valid = outcome_record(_BY_TYPE["wall_thickness"], success=True, eligible=True,
                           score_delta=20, run_id="r", candidate_id="winner", generation=1)
    rejection_weights = mutation_weights([rejected, valid])
    assert not rejected["success"]
    assert rejection_weights["wall_thickness"] > rejection_weights["fit_clearance"], rejection_weights

    # Exploit-only selection should favor the winner across many draws.
    counts = {"wall_thickness": 0, "fit_clearance": 0}
    for i in range(400):
        top = select_mutations(hist, n=1, exploration_rate=0.0, rng=random.Random(i))[0]["mutation_type"]
        if top in counts:
            counts[top] += 1
    assert counts["wall_thickness"] > counts["fit_clearance"], counts
    print("OK: adaptive selection biases toward winners, explores, and falls back cleanly", counts)
