import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance.cli import main as cli_main, render_ledger  # noqa: E402
from consent_governance.contracts import JobState  # noqa: E402
from consent_governance.service import GovernanceService  # noqa: E402
from support import PURPOSE, TEXT, build_basic_scenario, make_service  # noqa: E402


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock, self.dir = make_service()
        self.ids = build_basic_scenario(self.svc)

    def test_ledger_shows_why_available_and_flows_before_withdraw(self):
        book = self.svc.student_ledger("S-001")
        agree = [r for r in book["consent_ledger"] if r["kind"] == "agree"][0]
        self.assertTrue(agree["effective_now"])
        self.assertEqual(agree["why_available"]["field_categories"],
                         ["learning_behavior", "academic_result"])
        self.assertEqual(agree["why_available"]["retention_days"], 365)
        self.assertEqual(agree["why_available"]["recipients"],
                         ["广西师大", "东盟文理学院"])
        self.assertEqual(len(book["flows"]["datasets"]), 1)
        self.assertEqual(len(book["flows"]["exports"]), 1)
        self.assertEqual(book["flows"]["exports"][0]["recipient"], "东盟文理学院")
        self.assertEqual(len(book["flows"]["conclusions"]), 1)

    def test_ledger_withdrawal_propagation_and_receipts(self):
        self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        self.svc.run_pending_dispositions()
        book = self.svc.student_ledger("S-001")
        self.assertEqual(len(book["withdrawals"]), 1)
        w = book["withdrawals"][0]
        self.assertEqual(w["propagation"], JobState.DONE.value)
        self.assertEqual(w["job"]["progress"], "5/5")
        self.assertIn(self.ids["ds"], w["affected"]["datasets"])
        receipt_node_ids = {r["node_id"] for r in w["destruction_receipts"]}
        self.assertEqual(receipt_node_ids, {self.ids["ds"], self.ids["ex"]})
        self.assertEqual(book["unfinished_dispositions"], [])

    def test_ledger_shows_unfinished_progress_after_restart(self):
        result = self.svc.withdraw("S-001", PURPOSE, TEXT, 1)
        jid = result["job_id"]
        # 任务已开启但尚未执行：全新进程加载后账本显示待续进度
        svc2 = GovernanceService(self.dir, clock=self.clock)
        book = svc2.student_ledger("S-001")
        self.assertEqual(len(book["unfinished_dispositions"]), 1)
        self.assertEqual(book["unfinished_dispositions"][0]["progress"], "0/5")
        text = render_ledger(book)
        self.assertIn("未完成处置", text)
        self.assertIn(jid, text)
        self.assertIn("影响分析", text)


class CliEndToEndTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="cg-cli-")

    def run_cli(self, *argv: str) -> str:
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_main(["--data", self.dir, *argv])
        return buf.getvalue()

    def test_full_scenario_via_cli(self):
        self.run_cli("text-register", "T1", "跨校授权书")
        self.run_cli("text-publish", "T1", "中文过程分析", "behavior,academic", "180", "广西师大,东盟学院")
        out = self.run_cli("consent", "agree", "S-100", "中文过程分析", "T1", "1")
        self.assertIn("EV-0001", out)
        self.run_cli("record", "S-100", "behavior", '{"student_id":"S-100","n":3}')
        self.run_cli("dataset", "行为集", "R-0001", "中文过程分析",
                     '{"behavior":"聚合"}', '--payload', '{"rows":[{"student_id":"S-100"}]}')
        self.run_cli("export", "外发文件", "D-0001", "东盟学院")
        self.run_cli("conclusion", "阶段结论", "D-0001", "行为数据可用")

        # 访问申请职责分离
        self.run_cli("access-request", "AR-1", "alice", "D-0001", "复现")
        with self.assertRaises(Exception):
            self.run_cli("access-decide", "AR-1", "approve", "alice", "steward")
        self.run_cli("access-decide", "AR-1", "approve", "bob", "steward")
        out = self.run_cli("access-decide", "AR-1", "approve", "carol", "dpo")
        self.assertIn("granted", out)

        # 撤回 → 处置
        out = self.run_cli("withdraw", "S-100", "中文过程分析", "T1", "1")
        self.assertIn("J-0001", out)
        out = self.run_cli("job-run")
        self.assertIn("done", out)

        # 账本可读视图
        text = self.run_cli("ledger", "S-100")
        self.assertIn("学生授权账本", text)
        self.assertIn("为何", text)
        self.assertIn("撤回传播", text)
        self.assertIn("销毁回执", text)
        self.assertIn("东盟学院", text)

        # JSON 视图
        import json
        book = json.loads(self.run_cli("ledger", "S-100", "--json"))
        self.assertEqual(book["student_id"], "S-100")
        self.assertEqual(book["withdrawals"][0]["job"]["progress"], "5/5")

        # 状态在全新进程中仍可读（重启持久化）
        svc = GovernanceService(self.dir)
        self.assertEqual(svc.disposition.get_job("J-0001")["state"], "done")


if __name__ == "__main__":
    unittest.main()
