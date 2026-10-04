import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance.contracts import NodeKind, NodeState  # noqa: E402
from consent_governance.texts import GovernanceError  # noqa: E402
from support import FIELDS, PURPOSE, PURPOSE_NEW, RECIPIENTS, TEXT, build_basic_scenario, make_service  # noqa: E402


class DatasetFreezeTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock, _ = make_service()
        self.ids = build_basic_scenario(self.svc)

    def test_dataset_freezes_source_snapshot_and_rules(self):
        ds = self.svc.store.state["nodes"][self.ids["ds"]]
        src_ids = {s["record_id"] for s in ds["source_snapshot"]["sources"]}
        self.assertEqual(src_ids, {self.ids["r1"], self.ids["r2"]})
        for s in ds["source_snapshot"]["sources"]:
            self.assertIn("basis_event", s)
            self.assertEqual(s["text_id"], TEXT)
            self.assertEqual(s["text_revision"], 1)
        self.assertIn("learning_behavior", ds["minimization_rules"])
        self.assertTrue(ds["source_snapshot_hash"])
        self.assertTrue(ds["minimization_rules_hash"])

    def test_build_without_consent_rejected(self):
        self.svc.consent.refuse("S-007", PURPOSE, TEXT, 1)
        rid = self.svc.lineage.register_record("S-007", "behavior", {"x": 1})
        with self.assertRaises(GovernanceError):
            self.svc.lineage.build_dataset("d", [rid], PURPOSE, {}, {"rows": []})

    def test_lineage_graph_and_impact(self):
        impact = self.svc.lineage.impact_for_student("S-001")["impact"]
        self.assertEqual(impact[NodeKind.DATASET.value], [self.ids["ds"]])
        self.assertEqual(impact[NodeKind.EXPORT.value], [self.ids["ex"]])
        self.assertEqual(impact[NodeKind.CONCLUSION.value], [self.ids["cc"]])

    def test_conclusion_is_worm_history_not_rewritten(self):
        node = self.svc.store.state["nodes"][self.ids["cc"]]
        original = node["finding"]
        h = node["finding_hash"]
        # 撤回 S-001 并完成处置
        self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        self.svc.run_pending_dispositions()
        node = self.svc.store.state["nodes"][self.ids["cc"]]
        self.assertEqual(node["state"], NodeState.HISTORICAL_LOCKED.value)
        self.assertEqual(node["finding"], original)  # 正文未改写
        self.assertEqual(node["finding_hash"], h)
        self.assertIn("history_note", node)

    def test_rebuild_excludes_withdrawn_student_via_supersede_chain(self):
        self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        self.svc.run_pending_dispositions()
        old = self.svc.store.state["nodes"][self.ids["ds"]]
        self.assertEqual(old["state"], NodeState.DESTROYED.value)
        self.assertNotIn("payload", old)
        # 新版本节点存在且只含 S-002
        new_id = old["revisions_chain"][-1]
        self.assertNotEqual(new_id, old["node_id"])
        new = self.svc.store.state["nodes"][new_id]
        self.assertEqual(new["students"], ["S-002"])
        self.assertEqual(new["state"], NodeState.ACTIVE.value)
        self.assertEqual(new["payload"]["rows"], [{"student_id": "S-002", "v": 88}])

    def test_version_mismatch_after_reagree_on_new_revision(self):
        """学生撤回后按新版本重新同意，旧数据集依据即版本错配，需要重建。"""
        r2 = self.svc.texts.publish(TEXT, [PURPOSE, PURPOSE_NEW], FIELDS, 365, RECIPIENTS)
        self.svc.consent.withdraw("S-001", PURPOSE, TEXT, 1)
        self.svc.consent.reagree("S-001", PURPOSE, TEXT, r2)
        report = self.svc.lineage.node_consent_is_current(self.svc.store.state["nodes"][self.ids["ds"]])
        self.assertEqual(report[self.ids["r1"]], "version_mismatch")
        self.assertEqual(report[self.ids["r2"]], "current")


if __name__ == "__main__":
    unittest.main()
