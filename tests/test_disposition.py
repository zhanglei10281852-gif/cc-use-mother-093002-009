import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance.contracts import JobState, NodeKind, NodeState, StepState  # noqa: E402
from consent_governance.service import GovernanceService  # noqa: E402
from consent_governance.texts import GovernanceError  # noqa: E402
from support import PURPOSE, TEXT, build_basic_scenario, make_service  # noqa: E402


class DispositionTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock, self.dir = make_service()
        self.ids = build_basic_scenario(self.svc)

    def test_withdraw_propagates_full_pipeline(self):
        result = self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        job = self.svc.disposition.get_job(result["job_id"])
        self.assertEqual(job["trigger_event_id"], result["event"]["event_id"])
        self.svc.run_pending_dispositions()
        job = self.svc.disposition.get_job(job["job_id"])
        self.assertEqual(job["state"], JobState.DONE.value)
        self.assertTrue(all(s["state"] == StepState.DONE.value for s in job["steps"]))
        nodes = self.svc.store.state["nodes"]
        self.assertEqual(nodes[self.ids["ds"]]["state"], NodeState.DESTROYED.value)
        self.assertEqual(nodes[self.ids["ex"]]["state"], NodeState.DESTROYED.value)
        self.assertEqual(nodes[self.ids["cc"]]["state"], NodeState.HISTORICAL_LOCKED.value)
        # 销毁回执
        receipt_nodes = {r["node_id"] for r in self.svc.store.state["receipts"]}
        self.assertEqual(receipt_nodes, {self.ids["ds"], self.ids["ex"]})

    def test_repeated_withdraw_opens_single_job(self):
        a = self.svc.withdraw("S-001", PURPOSE, TEXT, 1, request_id="w1")
        b = self.svc.withdraw("S-001", PURPOSE, TEXT, 1, request_id="w2")
        self.assertEqual(a["job_id"], b["job_id"])
        open_jobs = self.svc.disposition.pending_jobs()
        self.assertEqual(len(open_jobs), 1)

    def test_destroy_receipt_issued_once_on_rerun(self):
        result = self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        self.svc.run_pending_dispositions()
        # 再次执行同一任务不得重复销毁/重复出回执
        self.svc.disposition.run_job(result["job_id"])
        receipts = [r for r in self.svc.store.state["receipts"] if r["student_id"] == "S-001"]
        self.assertEqual(len(receipts), 2)  # 数据集 + 导出 各一次

    def test_hold_blocks_destroy_but_keeps_audit(self):
        result = self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        jid = result["job_id"]
        # 处置执行前实施争议保全
        self.svc.holds.impose("CASE-9", [self.ids["ds"], self.ids["ex"]], "学生投诉授权范围争议")
        # 新进程加载并执行（模拟重启后续跑）
        svc2 = GovernanceService(self.dir, clock=self.clock)
        svc2.disposition.run_job(jid)
        job = svc2.disposition.get_job(jid)
        self.assertEqual(job["state"], JobState.BLOCKED.value)
        nodes = svc2.store.state["nodes"]
        self.assertEqual(nodes[self.ids["ds"]]["state"], NodeState.QUARANTINED.value)
        self.assertIn("payload", nodes[self.ids["ds"]])  # 正文未销毁
        # 审计完整保留：任务步骤、影响清单都在
        self.assertTrue(job["steps"][0]["state"] == StepState.DONE.value)
        self.assertEqual(len(job["affected"][NodeKind.DATASET.value]), 1)
        # 无销毁回执
        self.assertEqual(svc2.store.state["receipts"], [])
        # 争议解除后续跑
        svc2.holds.release("CASE-9")
        resumed = svc2.resume_after_holds()
        self.assertEqual(resumed, [jid])
        self.assertEqual(svc2.disposition.get_job(jid)["state"], JobState.DONE.value)
        self.assertEqual(svc2.store.state["nodes"][self.ids["ds"]]["state"], NodeState.DESTROYED.value)

    def test_step_progress_persists_across_restart(self):
        result = self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        jid = result["job_id"]
        self.svc.disposition.run_job(jid)
        self.svc.disposition.get_job(jid)
        # 新进程加载：任务已完成，状态持久
        svc2 = GovernanceService(self.dir, clock=self.clock)
        self.assertEqual(svc2.disposition.get_job(jid)["state"], JobState.DONE.value)

    def test_retention_expiry_scan_opens_jobs(self):
        self.clock.advance(days=400)
        opened = self.svc.scan_expired()
        self.assertEqual(len(opened), 2)  # 两名学生都到期
        # 重复扫描不开新任务
        again = self.svc.scan_expired()
        self.assertEqual(again, [])


class SeparationOfDutiesTests(unittest.TestCase):
    def setUp(self):
        self.svc, _, _ = make_service()
        self.ids = build_basic_scenario(self.svc)

    def test_applicant_cannot_approve(self):
        self.svc.access.request_access("AR-1", "alice", self.ids["ds"], "论文复现")
        with self.assertRaises(GovernanceError):
            self.svc.access.approve("AR-1", "alice", "steward")

    def test_two_distinct_roles_in_order(self):
        self.svc.access.request_access("AR-1", "alice", self.ids["ds"], "论文复现")
        with self.assertRaises(GovernanceError):  # 顺序错误：dpo 不能先批
            self.svc.access.approve("AR-1", "bob", "dpo")
        self.svc.access.approve("AR-1", "bob", "steward")
        with self.assertRaises(GovernanceError):  # 同一人不能跨级
            self.svc.access.approve("AR-1", "bob", "dpo")
        req = self.svc.access.approve("AR-1", "carol", "dpo")
        self.assertEqual(req["state"], "granted")

    def test_access_to_paused_asset_denied_even_with_approvals(self):
        self.svc.access.request_access("AR-2", "alice", self.ids["ds"], "复用")
        self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        self.svc.run_pending_dispositions()
        self.svc.access.approve("AR-2", "bob", "steward")
        with self.assertRaises(GovernanceError):
            self.svc.access.approve("AR-2", "carol", "dpo")
        self.assertEqual(self.svc.store.state["access_requests"]["AR-2"]["state"], "denied")

    def test_deny_ends_request(self):
        self.svc.access.request_access("AR-3", "alice", self.ids["ds"], "复用")
        self.svc.access.deny("AR-3", "bob", "steward")
        with self.assertRaises(GovernanceError):
            self.svc.access.approve("AR-3", "carol", "dpo")


if __name__ == "__main__":
    unittest.main()
