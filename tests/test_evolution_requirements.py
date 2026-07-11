"""Unit tests for the guided locked-requirement schema and deterministic checks.

Pure logic only — no generation stack, OpenSCAD, or model provider is imported.
"""

from __future__ import annotations

import unittest

from evolution_lab.requirements import (
    normalize_requirement,
    normalize_requirements,
    requirements_prompt_block,
    summarize_requirement,
    verify_requirements,
)


REPORT_OK = {"bbox_mm": [80.0, 55.0, 12.0], "watertight": True, "parts": 3, "bed_fit": "ok"}


def codes(reqs, *, parent="", candidate="cube();", report=None, floats=None):
    _, failures = verify_requirements(parent, candidate, report or REPORT_OK, floats or [], reqs)
    return failures


def findings(reqs, *, parent="", candidate="cube();", report=None, floats=None):
    found, _ = verify_requirements(parent, candidate, report or REPORT_OK, floats or [], reqs)
    return found


class NormalizationTests(unittest.TestCase):
    def test_plain_string_becomes_custom_note(self) -> None:
        item = normalize_requirement("preserve slider geometry")
        self.assertEqual(item["type"], "custom_note")
        self.assertEqual(item["severity"], "hard_lock")
        self.assertEqual(item["value"]["text"], "preserve slider geometry")

    def test_legacy_lock_shape_is_preserved(self) -> None:
        item = normalize_requirement({"type": "module", "name": "body_67_2d"})
        self.assertEqual(item["type"], "module")
        self.assertEqual(item["name"], "body_67_2d")
        self.assertEqual(item["severity"], "hard_lock")

    def test_guided_shape_defaults_severity_and_label(self) -> None:
        item = normalize_requirement({"category": "text", "type": "required_text", "value": {"text": "SIX SEVEN"}})
        self.assertEqual(item["severity"], "hard_lock")
        self.assertEqual(item["label"], "required_text")

    def test_junk_is_dropped(self) -> None:
        self.assertEqual(normalize_requirements([None, 5, "", {"type": "required_text"}]).__len__(), 1)


class DeterministicRejectionTests(unittest.TestCase):
    def test_within_max_size_passes_and_over_size_hard_rejects(self) -> None:
        ok = {"category": "dimensions", "type": "maximum_overall_size", "severity": "hard_lock",
              "value": {"x": 82, "y": 56, "z": 13}}
        self.assertEqual(codes([ok]), [])
        over = dict(ok, value={"x": 70, "y": 56, "z": 13})
        self.assertIn("build_volume_overflow", codes([over]))

    def test_required_text_present_vs_missing(self) -> None:
        req = {"category": "text", "type": "required_text", "severity": "hard_lock", "value": {"text": "SIX SEVEN"}}
        self.assertEqual(codes([req], candidate='text("SIX SEVEN");'), [])
        self.assertIn("required_feature_missing", codes([req], candidate='text("nope");'))

    def test_no_floating_geometry_hard_rejects_when_floats_present(self) -> None:
        req = {"category": "printability", "type": "no_floating_geometry", "severity": "hard_lock", "value": {}}
        self.assertEqual(codes([req], floats=[]), [])
        self.assertIn("broken_hard_lock", codes([req], floats=[{"z": 5, "x": 1, "y": 1}]))

    def test_exact_part_count_mismatch_hard_rejects(self) -> None:
        req = {"category": "parts", "type": "exact_part_count", "severity": "hard_lock", "value": {"n": 4}}
        self.assertIn("broken_hard_lock", codes([req], report=dict(REPORT_OK, parts=3)))
        self.assertEqual(codes([req], report=dict(REPORT_OK, parts=4)), [])

    def test_single_piece_required(self) -> None:
        req = {"category": "parts", "type": "single_piece_required", "severity": "hard_lock", "value": {}}
        self.assertIn("broken_hard_lock", codes([req], report=dict(REPORT_OK, parts=3)))
        self.assertEqual(codes([req], report=dict(REPORT_OK, parts=1)), [])

    def test_must_fit_printer_uses_bed_fit(self) -> None:
        req = {"category": "dimensions", "type": "must_fit_selected_printer", "severity": "hard_lock", "value": {}}
        self.assertEqual(codes([req], report=dict(REPORT_OK, bed_fit="ok")), [])
        self.assertIn("build_volume_overflow", codes([req], report=dict(REPORT_OK, bed_fit="EXCEEDS bed")))


class SeverityTests(unittest.TestCase):
    def test_soft_severity_violation_warns_without_failure_code(self) -> None:
        req = {"category": "parts", "type": "exact_part_count", "severity": "preferred", "value": {"n": 4}}
        found, failures = verify_requirements("", "cube();", dict(REPORT_OK, parts=3), [], [req])
        self.assertEqual(failures, [])
        self.assertTrue(any(f["severity"] == "warning" for f in found))

    def test_forbidden_severity_rejects_on_proven_violation(self) -> None:
        # A forbidden max-size (contrived) still rejects when measurably violated.
        req = {"category": "dimensions", "type": "maximum_overall_size", "severity": "forbidden",
               "value": {"x": 70, "y": 56, "z": 13}}
        self.assertIn("build_volume_overflow", codes([req]))

    def test_unverifiable_requirement_never_hard_fails(self) -> None:
        req = {"category": "identity", "type": "required_style", "severity": "hard_lock", "value": {"text": "art deco"}}
        found, failures = verify_requirements("", "cube();", REPORT_OK, [], [req])
        self.assertEqual(failures, [])
        self.assertTrue(any(f["issue_type"] == "constraint_unverified" for f in found))


class LegacyEnforcementTests(unittest.TestCase):
    def test_legacy_module_lock_detects_change(self) -> None:
        parent = "module knob(){ cube(5); }\n"
        same = "module knob(){ cube(5); }\nsphere(1);\n"
        changed = "module knob(){ cube(9); }\n"
        req = {"type": "module", "name": "knob", "severity": "hard_lock"}
        self.assertEqual(codes([req], parent=parent, candidate=same), [])
        self.assertIn("broken_hard_lock", codes([req], parent=parent, candidate=changed))

    def test_legacy_literal_lock(self) -> None:
        req = {"type": "literal", "value": "SIX SEVEN", "severity": "hard_lock"}
        self.assertEqual(codes([req], parent='x="SIX SEVEN";', candidate='x="SIX SEVEN";'), [])
        self.assertIn("broken_hard_lock", codes([req], parent='x="SIX SEVEN";', candidate='x="";'))

    def test_string_lock_is_soft_note_only(self) -> None:
        found, failures = verify_requirements("", "cube();", REPORT_OK, [], ["preserve outline"])
        self.assertEqual(failures, [])
        self.assertTrue(any(f["issue_type"] == "constraint_unverified" for f in found))


class RenderingTests(unittest.TestCase):
    def test_summarize_dimensions_and_chips(self) -> None:
        self.assertEqual(
            summarize_requirement({"type": "maximum_overall_size", "label": "Maximum overall size",
                                   "value": {"x": 82, "y": 56, "z": 13}}),
            "Maximum overall size: 82 × 56 × 13 mm",
        )
        self.assertEqual(
            summarize_requirement({"type": "required_mechanisms", "label": "Required mechanisms",
                                   "value": {"items": ["Spinner", "Slider"]}}),
            "Required mechanisms: Spinner + Slider",
        )

    def test_prompt_block_lists_severity(self) -> None:
        block = requirements_prompt_block([
            {"type": "required_text", "label": "Required text", "severity": "hard_lock", "value": {"text": "SIX SEVEN"}},
            {"type": "support_preference", "label": "Support preference", "severity": "preferred", "value": {"choice": "Prefer no supports"}},
        ])
        self.assertIn("[HARD LOCK] Required text: SIX SEVEN", block)
        self.assertIn("[PREFERRED] Support preference: Prefer no supports", block)


if __name__ == "__main__":
    unittest.main()
