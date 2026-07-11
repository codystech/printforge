"""Versioned benchmark definitions.  These are specifications, not fabricated results."""

BENCHMARK_NAMES = [
    ("step-enclosure", "enclosure around STEP reference", "reference role and exact fit"),
    ("port-cutout", "exact port-cutout alignment", "misalignment and guessed dimensions"),
    ("reference-export", "reference geometry excluded from export", "reference leakage"),
    ("wall-bracket", "wall-mounted bracket", "weak mounting bosses"),
    ("snap-container", "snap-fit container", "intentional interference false positive"),
    ("captured-slider", "captured slider", "binding and non-captive travel"),
    ("rotating-fidget", "rotating fidget mechanism", "fused rotational clearance"),
    ("heatset-plate", "heat-set insert mounting plate", "undersized bosses"),
    ("magnet-assembly", "magnet pocket assembly", "wrong pocket compensation"),
    ("cable-organizer", "cable organizer", "fragile clips"),
    ("boardgame-organizer", "board-game organizer", "wrong part count"),
    ("articulated-print", "articulated print", "floating links"),
    ("multicolor-sign", "multicolor embossed sign", "buried or unreadable text"),
    ("replacement-part", "mechanical replacement part", "dimension drift"),
    ("large-split", "large model requiring splitting", "build-volume overflow"),
    ("mixed-roles", "mixed printable/reference assembly", "role confusion"),
    ("multi-profile", "model with multiple printer profiles", "scope bleed"),
    ("broken-lock", "model with intentionally broken locks", "false lock pass"),
    ("floating-region", "expected floating-region detection", "false negative QA"),
    ("legacy-model", "legacy model without evolution metadata", "missing-field crash"),
]


def benchmark_catalog() -> list[dict]:
    return [{
        "benchmark_id": benchmark_id,
        "category": title,
        "prompt": f"Benchmark: {title}",
        "validated_spec": f"Versioned benchmark specification for {title}.",
        "expected_features": [title],
        "required_dimensions": [],
        "printer_profile": "Generic FDM - 220x220x250 PLA",
        "material_profile": {"material": "PLA", "nozzle": 0.4, "layer_height": 0.2},
        "hard_locks": [],
        "pass_fail_conditions": ["no critical failure", "evidence is persisted"],
        "known_traps": [trap],
        "expected_export_parts": [],
        "minimum_acceptable_score": 75,
        "critical_failure_conditions": ["broken_hard_lock", "reference_export_leakage", "generation_failed"],
        "result": None,
    } for benchmark_id, title, trap in BENCHMARK_NAMES]
