import csv
import json
import tempfile
import unittest
from pathlib import Path

from app import AnnotationStore, MergeStore, SingleFileMergeStore, UserFacingError, WorkspaceManager


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

    def create_merge_files(self, reviewer_b_tile_order="tile-1,tile-2"):
        (self.root / "reviewer_A_calibration.csv").write_text(
            "tile_id,label_set,notes\ntile-1,thin_cloud,A thinks thin\ntile-2,clear,A is clear\n",
            encoding="utf-8",
        )
        reviewer_b_rows = {
            "tile-1": "tile-1,haze_cirrus,B sees haze\n",
            "tile-2": "tile-2,clear,B is clear\n",
        }
        reviewer_b_content = "tile_id,label_set,notes\n" + "".join(
            reviewer_b_rows[tile_id] for tile_id in reviewer_b_tile_order.split(",")
        )
        (self.root / "reviewer_B_calibration.csv").write_text(reviewer_b_content, encoding="utf-8")
        (self.root / "calibration_consensus.csv").write_text(
            "tile_id,agreed_label_set,rule_or_counterexample,extra\ntile-1,,,kept\ntile-2,clear,existing,also-kept\n",
            encoding="utf-8",
        )

    def test_merge_state_shows_both_reviewers_and_writes_only_consensus(self):
        self.create_merge_files()
        store = MergeStore(
            self.root,
            "reviewer_A_calibration.csv",
            "reviewer_B_calibration.csv",
            "calibration_consensus.csv",
            "label_set.csv",
        )
        state = store.state()
        self.assertEqual("merge", state["mode"])
        self.assertEqual("thin_cloud", state["tiles"][0]["reviewer_a"]["label_set"])
        self.assertEqual("haze_cirrus", state["tiles"][0]["reviewer_b"]["label_set"])
        result = store.save("tile-1", ["thin_cloud"], "Panel decision", "manual")
        self.assertEqual("thin_cloud", result["label_set"])
        with (self.root / "calibration_consensus.csv").open(encoding="utf-8", newline="") as handle:
            consensus = list(csv.DictReader(handle))
        self.assertEqual("thin_cloud", consensus[0]["agreed_label_set"])
        self.assertEqual("Panel decision", consensus[0]["rule_or_counterexample"])
        self.assertEqual("kept", consensus[0]["extra"])
        self.assertIn("haze_cirrus", (self.root / "reviewer_B_calibration.csv").read_text(encoding="utf-8"))

    def test_merge_rejects_reviewer_tile_order_mismatch(self):
        self.create_merge_files(reviewer_b_tile_order="tile-2,tile-1")
        with self.assertRaisesRegex(UserFacingError, "记录顺序不一致"):
            MergeStore(
                self.root,
                "reviewer_A_calibration.csv",
                "reviewer_B_calibration.csv",
                "calibration_consensus.csv",
                "label_set.csv",
            )

    def test_auto_merge_agreements_writes_only_blank_consensus_rows(self):
        self.create_merge_files()
        (self.root / "calibration_consensus.csv").write_text(
            "tile_id,agreed_label_set,rule_or_counterexample\ntile-1,,,\ntile-2,,,\n",
            encoding="utf-8",
        )
        store = MergeStore(
            self.root,
            "reviewer_A_calibration.csv",
            "reviewer_B_calibration.csv",
            "calibration_consensus.csv",
            "label_set.csv",
        )
        result = store.auto_merge_agreements()
        self.assertEqual({"matched": 1, "saved": 1, "already_final": 0, "manual": 1}, result)
        with (self.root / "calibration_consensus.csv").open(encoding="utf-8", newline="") as handle:
            consensus = list(csv.DictReader(handle))
        self.assertEqual("", consensus[0]["agreed_label_set"])
        self.assertEqual("clear", consensus[1]["agreed_label_set"])
        self.assertIn("haze_cirrus", (self.root / "reviewer_B_calibration.csv").read_text(encoding="utf-8"))

        rerun = store.auto_merge_agreements()
        self.assertEqual({"matched": 1, "saved": 0, "already_final": 1, "manual": 1}, rerun)

    def test_single_file_merge_uses_tile_id_for_non_sequential_png_names(self):
        (self.root / "panels" / "tile-7.png").write_bytes(b"not-a-real-png")
        (self.root / "single_merge.csv").write_text(
            "tile_id,reviewer_A_label_set,reviewer_B_label_set,final_label_set,rationale,extra\n"
            "tile-1,clear,clear,,,first\n"
            "tile-7,thin_cloud,uncertain,,,jumped\n",
            encoding="utf-8",
        )
        store = SingleFileMergeStore(self.root, "single_merge.csv", "label_set.csv")
        state = store.state()
        self.assertEqual("merge", state["mode"])
        self.assertEqual("single_file", state["merge_layout"])
        self.assertTrue(state["tiles"][0]["has_image"])
        self.assertTrue(state["tiles"][1]["has_image"])
        self.assertEqual("clear", state["tiles"][0]["reviewer_agreement_label_set"])
        self.assertEqual("", state["tiles"][1]["reviewer_agreement_label_set"])

        result = store.auto_merge_agreements()
        self.assertEqual({"matched": 1, "saved": 1, "already_final": 0, "manual": 1}, result)
        store.save("tile-7", ["thin_cloud"], "Panel decision", "manual")
        with (self.root / "single_merge.csv").open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual("clear", rows[0]["final_label_set"])
        self.assertEqual("thin_cloud", rows[1]["final_label_set"])
        self.assertEqual("Panel decision", rows[1]["rationale"])
        self.assertEqual("uncertain", rows[1]["reviewer_B_label_set"])
        self.assertEqual("jumped", rows[1]["extra"])

    def test_workspace_manager_catalog_preview_and_activation(self):
        self.create_merge_files()
        (self.root / "single_merge.csv").write_text(
            "tile_id,reviewer_A_label_set,reviewer_B_label_set,final_label_set,rationale\n"
            "tile-1,clear,clear,,\n",
            encoding="utf-8",
        )
        manager = WorkspaceManager(self.root, "label_set.csv")
        catalog = manager.navigation()
        kinds = {entry["name"]: entry["kind"] for entry in catalog["csv_files"]}
        self.assertEqual("reviewer", kinds["reviewer.csv"])
        self.assertEqual("label_config", kinds["label_set.csv"])
        self.assertEqual("single_file_merge", kinds["single_merge.csv"])
        self.assertEqual(1, catalog["panel_count"])
        self.assertEqual("clear", manager.label_preview("label_set.csv")["labels"][0]["label"])

        review_state = manager.activate({"mode": "review", "csv": "reviewer.csv", "labels": "label_set.csv"})
        self.assertEqual("annotation", review_state["mode"])
        consensus_state = manager.activate(
            {
                "mode": "consensus",
                "reviewer_a": "reviewer_A_calibration.csv",
                "reviewer_b": "reviewer_B_calibration.csv",
                "consensus": "calibration_consensus.csv",
                "labels": "label_set.csv",
            }
        )
        self.assertEqual("merge", consensus_state["mode"])
        single_state = manager.activate(
            {"mode": "single_file_consensus", "csv": "single_merge.csv", "labels": "label_set.csv"}
        )
        self.assertEqual("single_file", single_state["merge_layout"])


if __name__ == "__main__":
    unittest.main()
