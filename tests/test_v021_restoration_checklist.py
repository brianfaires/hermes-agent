"""Keep numbered human checks in sync with landed ledger subfeatures."""

import collections
import json
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "fork"


class RestorationChecklistCoverageTests(unittest.TestCase):
    def test_every_landed_subfeature_has_exactly_one_human_check(self):
        ledger = json.loads((DOCS / "V021_RESTORATION_LEDGER.json").read_text())
        landed = [
            item["id"] for row in ledger["rows"] for item in row["subfeatures"]
            if item["disposition"] == "restoration_implemented_with_tests"
        ]
        checklist = (DOCS / "V021_SAFE_SLICE_CHECKLIST.md").read_text()
        sections = re.findall(
            r"^## (\d+)\. ([^\n]+)\n(.*?)(?=^## |\Z)",
            checklist, re.MULTILINE | re.DOTALL,
        )
        self.assertTrue(sections, "Missing numbered cases")
        self.assertEqual([int(n) for n, _, _ in sections], list(range(1, len(sections) + 1)))
        covered = []
        for number, title, body in sections:
            ids = re.findall(r"`(FC-[0-9A-Za-z]+-[0-9A-Za-z_-]+)`", body)
            self.assertTrue(ids or title.startswith("Retained:"), f"Missing coverage: {number}")
            steps = re.findall(r"^- \[([ xX])\] ([a-z])\. (.+)$", body, re.MULTILINE)
            self.assertGreaterEqual(len(steps), 2, f"Missing action/safety checks: {number}")
            self.assertEqual([letter for _, letter, _ in steps],
                             [chr(ord("a") + i) for i in range(len(steps))])
            for state, letter, step in steps:
                self.assertIn(" -> ", step, f"Missing action/expected result: {number}{letter}")
                action, expected = step.split(" -> ", 1)
                self.assertTrue(action.strip() and expected.strip())
                if state.lower() == "x":
                    self.assertRegex(step, r"Evidence: \S+", "Checked step needs evidence")
            covered.extend(ids)
        self.assertEqual(len(landed), len(set(landed)), "Duplicate ledger IDs")
        self.assertEqual(len(covered), len(set(covered)), "Duplicate human coverage")
        self.assertEqual(set(covered), set(landed), "Missing or unknown coverage")
        all_ids = re.findall(r"`(FC-[0-9A-Za-z]+-[0-9A-Za-z_-]+)`", checklist)
        self.assertEqual(collections.Counter(all_ids), collections.Counter(covered))
        self.assertEqual(sum(title.startswith("Retained:") for _, title, _ in sections), 5)


if __name__ == "__main__":
    unittest.main()
