"""Self-check for the Training Lab -> production promotion bridges (app.py).

Runs against a temp library so it never touches real data. Exercises both bridges end to end:
  B) promote a winner -> library exemplar that _taste_example() actually selects
  A) promote a rule   -> promoted_rules_block() actually emits it into the prompt
plus revoke and the PRINT_FORGE_PROMOTED_RULES kill-switch.

Run: uv run --with fastapi --with ... python tests/test_promotion.py   (see run-training-lab.sh deps)
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="promo-test-"))
    # Redirect the module globals the promotion helpers use at call time.
    app.LIB_DIR = tmp
    app.WORK_DIR = tmp
    app.PROMOTED_RULES_FILE = tmp / "promoted_rules.json"

    # --- Bridge B: winner -> library exemplar picked up by the taste-loop ---
    scad = "// tag\ncube([10,10,10]);\n"
    mid = app.promote_exemplar_to_library(scad, "a desk phone stand", "a desk phone stand", 66, "cand_1")
    duplicate = app.promote_exemplar_to_library(scad, "duplicate request", "duplicate request", 99, "cand_1")
    meta = app.json.loads((tmp / mid / "meta.json").read_text())
    assert duplicate == mid, "promotion retries must return the existing exemplar ID"
    assert len([path for path in tmp.iterdir() if (path / "meta.json").exists()]) == 1, \
        "promotion retries must not create duplicate exemplars"
    assert meta["rating"] == 1, "promoted exemplar must be a thumbs-up"
    assert meta["source"] == "evolution-lab" and meta["source_candidate_id"] == "cand_1"

    fewshot = app._taste_example("phone stand for my desk")
    assert "USER-APPROVED EXAMPLE" in fewshot and "// tag" in fewshot, \
        "taste-loop must select the promoted exemplar for an overlapping prompt"

    assert app.revoke_exemplar_from_library("cand_1") == 1
    assert not (tmp / mid).exists(), "revoke must remove the library model"
    assert "// tag" not in app._taste_example("phone stand for my desk"), "revoked exemplar must vanish"

    # --- Bridge A: validated rule -> promoted_rules_block into the prompt ---
    rule = {"id": "r1", "recommendation": "Embed angled rests 2-3mm into the base",
            "trigger_conditions": "a feature is angled off the base", "scope": {}}
    app.promote_rule_to_production(rule)
    block = app.promoted_rules_block("make a stand")
    assert "Embed angled rests" in block and "VALIDATED DESIGN RULES" in block, \
        "promoted rule must appear in the generation prompt block"

    os.environ["PRINT_FORGE_PROMOTED_RULES"] = "0"
    assert app.promoted_rules_block("make a stand") == "", "kill-switch must disable the block"
    os.environ.pop("PRINT_FORGE_PROMOTED_RULES")

    assert app.revoke_rule_from_production("r1") == 1
    assert app.promoted_rules_block("make a stand") == "", "revoked rule must vanish from the prompt"

    print("OK: promotion bridges (exemplar + rule), taste-loop pickup, revoke, kill-switch")


if __name__ == "__main__":
    main()
