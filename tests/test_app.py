import csv
import json
import tempfile
import unittest
from pathlib import Path

from app import AnnotationStore, UserFacingError


LABELS = """label,description,multi_selectable,exclusive,allowed_with
clear,Clear,true,false,thin_cloud
thin_cloud,Thin,true,false,clear
uncertain,Uncertain,false,true,
"""


class AnnotationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        (self.root / "panels").mkdir()
        (self.root / "panels" / "tile-1.png").write_bytes(b"not-a-real-png")
        (self.root / "label_set.csv").write_text(LABELS, encoding="utf-8")
        (self.root / "reviewer.csv").write_text(
            "tile_id,label_set,notes,extra\ntile-1,,initial,kept\ntile-2,clear,done,also-kept\n",
            encoding="utf-8",
        )
        self.store = AnnotationStore(self.root, "reviewer.csv", "label_set.csv")

    def tearDown(self):
        self.tempdir.cleanup()

    def read_rows(self):
        with (self.root / "reviewer.csv").open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    def test_save_is_normalized_preserves_extra_columns_and_audits(self):
        result = self.store.save("tile-1", ["thin_cloud", "clear"], "updated", "test")
        self.assertEqual("clear|thin_cloud", result["label_set"])
        rows = self.read_rows()
        self.assertEqual("clear|thin_cloud", rows[0]["label_set"])
        self.assertEqual("updated", rows[0]["notes"])
        self.assertEqual("kept", rows[0]["extra"])
        self.assertTrue(list((self.root / ".annotation_history").glob("reviewer.*.csv")))
        audit = (self.root / ".annotation_history" / "audit.jsonl").read_text(encoding="utf-8")
        self.assertEqual("tile-1", json.loads(audit)["tile_id"])

    def test_disallowed_pair_is_rejected_without_writing(self):
        with self.assertRaises(UserFacingError):
            self.store.save("tile-1", ["clear", "uncertain"], "x", "test")
        self.assertEqual("", self.read_rows()[0]["label_set"])

    def test_state_marks_missing_image(self):
        state = self.store.state()
        self.assertTrue(state["tiles"][0]["has_image"])
        self.assertFalse(state["tiles"][1]["has_image"])


if __name__ == "__main__":
    unittest.main()
