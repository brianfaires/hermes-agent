"""Keep the cumulative human checklist in sync with landed ledger subfeatures."""

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
            subfeature["id"]
            for row in ledger["rows"]
            for subfeature in row["subfeatures"]
            if subfeature["disposition"] == "restoration_implemented_with_tests"
        ]
        checklist = (DOCS / "V021_SAFE_SLICE_CHECKLIST.md").read_text()
        # IDs belong to data rows, never to prose or an automated inventory.
        rows = [line for line in checklist.splitlines() if line.startswith("| ")]
        covered = []
        for row in rows:
            ids = re.findall(r"`(FC-[0-9A-Za-z]+-[0-9A-Za-z_-]+)`", row)
            if not ids:
                continue
            cells = row.split("|")[1:-1]
            self.assertEqual(len(cells), 4, row)
            self.assertTrue(cells[1].strip(), "Missing action/expected result")
            self.assertTrue(cells[2].strip(), "Missing safety negative")
            self.assertRegex(cells[3].strip(), r"^(pending|pass|fail) / .+$")
            if not cells[3].strip().startswith("pending"):
                self.assertNotEqual(cells[3].split(" / ", 1)[1].strip(), "—")
            covered.extend(ids)
        self.assertEqual(len(landed), len(set(landed)), "Duplicate landed ledger IDs")
        duplicates = [key for key, count in collections.Counter(covered).items() if count > 1]
        self.assertEqual(duplicates, [], "Duplicate human checklist coverage")
        self.assertEqual(
            set(covered), set(landed),
            f"Missing checks: {sorted(set(landed) - set(covered))}; "
            f"unlanded/unknown checks: {sorted(set(covered) - set(landed))}",
        )
        # Catch IDs accidentally left in obsolete prose or a second inventory.
        all_ids = re.findall(r"`(FC-[0-9A-Za-z]+-[0-9A-Za-z_-]+)`", checklist)
        self.assertEqual(collections.Counter(all_ids), collections.Counter(covered))


if __name__ == "__main__":
    unittest.main()
