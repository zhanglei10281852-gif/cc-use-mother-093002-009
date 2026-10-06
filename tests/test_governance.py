"""授权治理端到端测试。"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance import contracts as C
from consent_governance.errors import (
    CoverageError,
    DatasetError,
    DispositionError,
    GovernanceError,
    ImmutableConclusionError,
    PolicyError,
    SegregationOfDutiesError,
)
from consent_governance.ledger import build_ledger
from consent_governance.service import CAUSE_DISPUTE, GovernanceService
from consent_governance.storage import EventStore

PURPOSES = ["course_improvement", "pronunciation_research"]
CATS = ["behavior", "assessment"]
RECIPIENTS = ["CN-UNIV-A", "ASEAN-UNIV-B"]


class Clock:
    """可控时钟，用于保留期与重启测试。"""

    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, days: int = 0, **kw) -> None:
        self.t += timedelta(days=days, **kw)


def make_service(path=None, clock=None):
    return GovernanceService(EventStore(path, clock=clock or (lambda: datetime.now(timezone.utc))))


def records(*students):
    out = []
    for i, sid in enumerate(students, start=1):
        out.append({
            "record_id": f"R-{sid}", "student_id": sid, "category": "behavior",
            "fields": ["video_face", "click_stream"],
        })
    return out


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "events.jsonl"
        self.clock = Clock(datetime(2026, 10, 1, tzinfo=timezone.utc))
        self.s = make_service(self.path, self.clock)
        self.policy = self.s.register_policy(
            "中文学习数据授权书", PURPOSES, CATS, 365, RECIPIENTS
        )
        for sid in ("S-1", "S-2", "S-3"):
            self.s.record_consent(sid, self.policy.policy_id)

    def tearDown(self):
        self.tmp.cleanup()

    def make_dataset(self, students=("S-1", "S-2", "S-3"), purpose="course_improvement"):
        return self.s.generate_dataset(
            purpose, records(*students), ["behavior"], RECIPIENTS,
            minimization_rules={"drop_fields": ["video_face"], "hash_fields": ["student_no"]},
        )


class PolicyTests(ServiceTestBase):
    def test_policy_is_versioned(self):
        rev2 = self.s.register_policy(
            "中文学习数据授权书", PURPOSES + ["ai_tutoring"], CATS, 365, RECIPIENTS,
            policy_id=self.policy.policy_id,
        )
        self.assertEqual(rev2.revision, 2)
        diff = self.s.policy_diff(self.policy.policy_id)
        self.assertEqual(diff["added_purposes"], ["ai_tutoring"])
        self.assertTrue(diff["new_purposes_require_reconsent"])

    def test_new_purpose_not_covered_by_old_consent(self):
        self.s.register_policy(
            "中文学习数据授权书", PURPOSES + ["ai_tutoring"], CATS, 365, RECIPIENTS,
            policy_id=self.policy.policy_id,
        )
        cov = self.s.effective_consent("S-1", "ai_tutoring")
        self.assertFalse(cov.covered)
        self.assertIn("no_consent", cov.reasons)

    def test_invalid_policy_rejected(self):
        with self.assertRaises(PolicyError):
            self.s.register_policy("x", [], CATS, 10, RECIPIENTS)
        with self.assertRaises(PolicyError):
            self.s.register_policy("x", PURPOSES, CATS, 0, RECIPIENTS)


class ConsentCoverageTests(ServiceTestBase):
    def test_granted_then_denied_blocks_use(self):
        self.s.record_consent("S-1", self.policy.policy_id, C.DECISION_DENIED)
        cov = self.s.effective_consent("S-1", "course_improvement")
        self.assertFalse(cov.covered)
        self.assertEqual(cov.reasons, ["denied"])

    def test_denial_after_grant_propagates_like_withdrawal(self):
        ds = self.make_dataset(students=("S-1", "S-2"))
        export = self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出", dataset_id=ds.dataset_id)
        self.s.record_consent("S-1", self.policy.policy_id, C.DECISION_DENIED)
        self.assertIn("S-1", self.s.datasets[ds.dataset_id].suspended_students)
        self.assertEqual(
            self.s.datasets[ds.dataset_id].suspension_causes["S-1"], {C.CAUSE_DENIAL}
        )
        self.assertIn("S-1", self.s.artifacts[export.artifact_id].suspended_students)
        self.assertTrue(self.s.pending_tasks())
        # 重新同意后恢复
        self.s.record_consent("S-1", self.policy.policy_id)
        self.assertNotIn("S-1", self.s.datasets[ds.dataset_id].suspended_students)

    def test_field_category_and_recipient_must_be_covered(self):
        cov = self.s.effective_consent(
            "S-1", "course_improvement", ["biometric"], ["CN-UNIV-A"]
        )
        self.assertFalse(cov.covered)
        self.assertTrue(any("biometric" in r for r in cov.reasons))

        cov = self.s.effective_consent(
            "S-1", "course_improvement", ["behavior"], ["UNKNOWN-UNIV"]
        )
        self.assertFalse(cov.covered)
        self.assertTrue(any("UNKNOWN-UNIV" in r for r in cov.reasons))

    def test_retention_expiry(self):
        self.clock.advance(days=400)
        cov = self.s.effective_consent("S-1", "course_improvement")
        self.assertFalse(cov.covered)
        self.assertIn("retention_expired", cov.reasons)

    def test_consent_was_valid_at_historical_time(self):
        self.clock.advance(days=400)
        cov = self.s.effective_consent(
            "S-1", "course_improvement", at=datetime(2026, 10, 2, tzinfo=timezone.utc)
        )
        self.assertTrue(cov.covered)


class DatasetTests(ServiceTestBase):
    def test_generation_freezes_snapshot_and_minimization(self):
        ds = self.make_dataset()
        self.assertEqual(len(ds.snapshot), 3)
        entry = next(e for e in ds.snapshot if e.student_id == "S-1")
        self.assertNotIn("video_face", entry.fields)  # drop_fields 已固化
        self.assertIn("click_stream", entry.fields)
        self.assertTrue(entry.decision_id.startswith("A-"))

    def test_generation_refuses_unauthorized_record(self):
        self.s.record_consent("S-2", self.policy.policy_id, C.DECISION_DENIED)
        with self.assertRaises(CoverageError) as ctx:
            self.make_dataset()
        self.assertIn("S-2", ctx.exception.student_id)

    def test_derived_artifact_lineage(self):
        ds = self.make_dataset()
        export = self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出包", dataset_id=ds.dataset_id)
        report = self.s.derive_artifact(
            C.ARTIFACT_REPORT, "分析报告", parent_artifact_id=export.artifact_id,
            dataset_id=ds.dataset_id,
        )
        conclusion = self.s.derive_artifact(
            C.ARTIFACT_CONCLUSION, "研究结论", parent_artifact_id=report.artifact_id
        )
        lin = self.s.lineage(ds.dataset_id)
        self.assertEqual(set(lin["descendants"]), {export.artifact_id, report.artifact_id, conclusion.artifact_id})
        self.assertEqual(self.s._root_datasets(conclusion.artifact_id), {ds.dataset_id})

    def test_cannot_derive_from_suspended_students(self):
        ds = self.make_dataset(students=("S-1", "S-2"))
        self.s.withdraw("S-1")
        with self.assertRaises(DatasetError):
            self.s.derive_artifact(C.ARTIFACT_EXPORT, "x", dataset_id=ds.dataset_id,
                                   student_ids=["S-1", "S-2"])
        # 仍获授权的 S-2 可单独派生
        art = self.s.derive_artifact(C.ARTIFACT_EXPORT, "x", dataset_id=ds.dataset_id)
        self.assertEqual(set(art.student_ids), {"S-2"})


class WithdrawalPropagationTests(ServiceTestBase):
    def test_withdrawal_suspends_dataset_and_downstream(self):
        ds = self.make_dataset()
        export = self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出", dataset_id=ds.dataset_id)
        report = self.s.derive_artifact(C.ARTIFACT_REPORT, "报告", parent_artifact_id=export.artifact_id)
        conclusion = self.s.derive_artifact(
            C.ARTIFACT_CONCLUSION, "结论", parent_artifact_id=report.artifact_id)

        result = self.s.withdraw("S-1", request_key="wdr-1")
        self.assertIn(ds.dataset_id, result["suspended_datasets"])
        self.assertEqual(set(result["suspended_artifacts"]), {export.artifact_id, report.artifact_id})
        self.assertEqual(result["annotated_conclusions"], [conclusion.artifact_id])

        self.assertIn("S-1", self.s.datasets[ds.dataset_id].suspended_students)
        # 结论不暂停、不销毁，只追加标注
        self.assertFalse(self.s.artifacts[conclusion.artifact_id].suspended)
        self.assertEqual(len(self.s.artifacts[conclusion.artifact_id].annotations), 1)

    def test_duplicate_withdrawal_request_processed_once(self):
        self.make_dataset()
        r1 = self.s.withdraw("S-1", request_key="wdr-1")
        r2 = self.s.withdraw("S-1", request_key="wdr-1")
        self.assertEqual(r1, r2)
        # 只产生一条撤回事件
        self.assertEqual(len(self.s.withdrawals["S-1"]), 1)

    def test_repeated_withdrawal_without_key_still_single_task(self):
        ds = self.make_dataset()
        self.s.withdraw("S-1")
        self.s.withdraw("S-1")
        tasks = [t for t in self.s.tasks.values()
                 if t.target_id == ds.dataset_id and "S-1" in t.student_ids]
        self.assertEqual(len(tasks), 1)

    def test_purpose_scoped_withdrawal_only_matches_purpose(self):
        ds1 = self.make_dataset(purpose="course_improvement")
        ds2 = self.make_dataset(purpose="pronunciation_research")
        result = self.s.withdraw("S-1", scope=C.SCOPE_PURPOSE, purpose="pronunciation_research")
        self.assertNotIn(ds1.dataset_id, result["suspended_datasets"])
        self.assertIn(ds2.dataset_id, result["suspended_datasets"])

    def test_conclusion_body_immutable_only_annotation_api(self):
        ds = self.make_dataset()
        conclusion = self.s.derive_artifact(C.ARTIFACT_CONCLUSION, "结论", dataset_id=ds.dataset_id)
        with self.assertRaises(ImmutableConclusionError):
            # 非结论产物不能走结论标注
            export = self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出", dataset_id=ds.dataset_id)
            self.s.annotate_conclusion(export.artifact_id, "x")
        self.s.annotate_conclusion(conclusion.artifact_id, "补充说明")
        self.assertEqual(self.s.artifacts[conclusion.artifact_id].annotations[-1]["note"], "补充说明")


class DispositionTests(ServiceTestBase):
    def test_rebuild_removes_withdrawn_student_and_certifies_old(self):
        ds = self.make_dataset()
        self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出", dataset_id=ds.dataset_id)
        self.s.withdraw("S-1")
        task = next(t for t in self.s.pending_tasks()
                    if t.target_kind == C.TARGET_DATASET and t.action == C.ACTION_REBUILD)
        self.s.progress_task(task.task_id, 50, "重建中")
        done = self.s.complete_task(task.task_id)
        self.assertEqual(done.status, C.TASK_COMPLETED)
        self.assertTrue(self.s.datasets[ds.dataset_id].destroyed)
        successor = self.s.datasets[done.result_dataset_id]
        self.assertEqual(successor.student_ids(), {"S-2", "S-3"})
        self.assertTrue(self.s.datasets[ds.dataset_id].certificate_id)

    def test_destroy_when_no_other_students(self):
        ds = self.make_dataset(students=("S-1",))
        self.s.withdraw("S-1")
        task = next(t for t in self.s.pending_tasks() if t.target_id == ds.dataset_id)
        self.assertEqual(task.action, C.ACTION_DESTROY)
        done = self.s.complete_task(task.task_id, method="crypto_shredding")
        self.assertTrue(self.s.datasets[ds.dataset_id].destroyed)
        self.assertEqual(self.s.certificates[done.certificate_id].method, "crypto_shredding")

    def test_no_double_certificate(self):
        ds = self.make_dataset(students=("S-1",))
        self.s.withdraw("S-1")
        task = next(t for t in self.s.pending_tasks() if t.target_id == ds.dataset_id)
        self.s.complete_task(task.task_id)
        with self.assertRaises(DispositionError):
            self.s.complete_task(task.task_id)

    def test_destruction_receipt_idempotent(self):
        ds = self.make_dataset(students=("S-1",))
        payload = dict(target_kind=C.TARGET_DATASET, target_id=ds.dataset_id,
                       student_ids=["S-1"], method="recipient_secure_deletion")
        c1 = self.s.register_destruction_receipt(**payload, request_key="rcpt-1")
        c2 = self.s.register_destruction_receipt(**payload, request_key="rcpt-1")
        self.assertEqual(c1.certificate_id, c2.certificate_id)
        self.assertEqual(
            [c for c in self.s.certificates.values() if c.request_key == "rcpt-1"].__len__(), 1
        )

    def test_reconsent_cancels_pending_task_and_resumes_access(self):
        ds = self.make_dataset(students=("S-1", "S-2"))
        self.s.withdraw("S-1")
        task = next(t for t in self.s.pending_tasks() if t.target_id == ds.dataset_id)
        self.s.progress_task(task.task_id, 30, "处置中")
        # 重新同意（文本未变，覆盖同一用途）
        self.s.record_consent("S-1", self.policy.policy_id)
        self.assertEqual(self.s.tasks[task.task_id].status, C.TASK_CANCELLED)
        self.assertNotIn("S-1", self.s.datasets[ds.dataset_id].suspended_students)
        self.assertEqual(self.s.pending_tasks(), [])

    def test_reconsent_after_destroy_does_not_revive(self):
        ds = self.make_dataset(students=("S-1",))
        self.s.withdraw("S-1")
        task = next(t for t in self.s.pending_tasks() if t.target_id == ds.dataset_id)
        self.s.complete_task(task.task_id)
        self.s.record_consent("S-1", self.policy.policy_id)
        self.assertTrue(self.s.datasets[ds.dataset_id].destroyed)

    def test_blocked_task_cannot_complete(self):
        ds = self.make_dataset(students=("S-1",))
        self.s.withdraw("S-1")
        task = next(t for t in self.s.pending_tasks() if t.target_id == ds.dataset_id)
        self.s.block_task(task.task_id, "等待接收机构回执")
        with self.assertRaises(DispositionError):
            self.s.complete_task(task.task_id)
        self.s.unblock_task(task.task_id)
        self.s.complete_task(task.task_id)


class RetentionTests(ServiceTestBase):
    def test_sweep_suspends_and_opens_tasks(self):
        ds = self.make_dataset()
        self.clock.advance(days=400)
        result = self.s.sweep_retention()
        expired = {e["student_id"] for e in result["expired"]}
        self.assertEqual(expired, {"S-1", "S-2", "S-3"})
        self.assertIn("S-1", self.s.datasets[ds.dataset_id].suspended_students)
        self.assertEqual(
            self.s.datasets[ds.dataset_id].suspension_causes["S-1"], {C.CAUSE_RETENTION}
        )
        tasks = [t for t in result["opened_tasks"]]
        self.assertTrue(tasks)

    def test_sweep_idempotent(self):
        self.make_dataset()
        self.clock.advance(days=400)
        r1 = self.s.sweep_retention()
        self.assertTrue(r1["opened_tasks"])  # 首次扫描开出处置任务
        r2 = self.s.sweep_retention()
        self.assertEqual(r2["opened_tasks"], [])  # 第二次不重复开任务
        self.assertEqual(r2["suspended_datasets"], [])
        self.assertEqual(r2["expired"], [])


class SegregationOfDutiesTests(ServiceTestBase):
    def test_requester_cannot_approve_own_request(self):
        ds = self.make_dataset()
        req = self.s.submit_request("analyst-li", C.TARGET_DATASET, ds.dataset_id, "course_improvement")
        with self.assertRaises(SegregationOfDutiesError):
            self.s.decide_request(req.request_id, "analyst-li", True)
        decided = self.s.decide_request(req.request_id, "dpo-wang", True, "复核通过")
        self.assertEqual(decided.status, "approved")

    def test_request_against_suspended_target_rejected(self):
        ds = self.make_dataset()
        self.s.withdraw("S-1")
        req = self.s.submit_request("analyst-li", C.TARGET_DATASET, ds.dataset_id, "course_improvement")
        with self.assertRaises(GovernanceError):
            self.s.decide_request(req.request_id, "dpo-wang", True)


class DisputeTests(ServiceTestBase):
    def test_dispute_keeps_audit_but_blocks_body_access(self):
        ds = self.make_dataset()
        export = self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出", dataset_id=ds.dataset_id)
        dispute = self.s.open_dispute("S-1")
        self.assertEqual(
            self.s.datasets[ds.dataset_id].suspension_causes["S-1"], {CAUSE_DISPUTE}
        )
        self.assertIn("S-1", self.s.artifacts[export.artifact_id].suspended_students)
        # 争议期间撤回仍登记、传播，但处置任务被争议阻塞，数据不销毁
        self.s.withdraw("S-1")
        self.assertFalse(self.s.datasets[ds.dataset_id].destroyed)
        causes = self.s.datasets[ds.dataset_id].suspension_causes["S-1"]
        self.assertEqual(causes, {CAUSE_DISPUTE, C.CAUSE_WITHDRAWAL})
        tasks = [t for t in self.s.tasks.values() if "S-1" in t.student_ids]
        self.assertTrue(tasks)
        self.assertTrue(all(t.status == C.TASK_BLOCKED for t in tasks))
        # 争议解除后：撤回起因仍在，处置任务放行
        self.s.close_dispute(dispute.dispute_id)
        self.assertEqual(
            self.s.datasets[ds.dataset_id].suspension_causes["S-1"], {C.CAUSE_WITHDRAWAL}
        )
        self.assertTrue(all(t.status != C.TASK_BLOCKED for t in tasks))

    def test_dispute_close_restores_access_when_consent_valid(self):
        ds = self.make_dataset()
        dispute = self.s.open_dispute("S-1")
        self.assertIn("S-1", self.s.datasets[ds.dataset_id].suspended_students)
        self.s.close_dispute(dispute.dispute_id)
        self.assertNotIn("S-1", self.s.datasets[ds.dataset_id].suspended_students)
        self.assertEqual(self.s.datasets[ds.dataset_id].suspension_causes.get("S-1"), None)


class RestartRecoveryTests(ServiceTestBase):
    def test_state_and_task_progress_survive_restart(self):
        ds = self.make_dataset()
        self.s.withdraw("S-1")
        task = next(t for t in self.s.pending_tasks() if t.target_id == ds.dataset_id)
        self.s.progress_task(task.task_id, 57, "等待下游确认")

        restarted = make_service(self.path, self.clock)
        self.assertEqual(len(restarted.datasets), 1)
        recovered = restarted.tasks[task.task_id]
        self.assertEqual(recovered.status, C.TASK_IN_PROGRESS)
        self.assertEqual(recovered.progress_percent, 57)
        self.assertEqual(recovered.detail, "等待下游确认")
        self.assertIn("S-1", restarted.datasets[ds.dataset_id].suspended_students)

    def test_complete_after_restart(self):
        self.make_dataset(students=("S-1",))
        self.s.withdraw("S-1")
        task_id = next(iter(self.s.tasks))
        restarted = make_service(self.path, self.clock)
        done = restarted.complete_task(task_id)
        self.assertEqual(done.status, C.TASK_COMPLETED)


class LedgerTests(ServiceTestBase):
    def test_ledger_explains_full_journey(self):
        ds = self.make_dataset()
        export = self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出包", dataset_id=ds.dataset_id)
        conclusion = self.s.derive_artifact(
            C.ARTIFACT_CONCLUSION, "结论", dataset_id=ds.dataset_id)
        self.s.withdraw("S-1", request_key="wdr-1")
        task = next(t for t in self.s.pending_tasks()
                    if t.target_kind == C.TARGET_DATASET and "S-1" in t.student_ids)
        self.s.progress_task(task.task_id, 20, "开始处置")

        ledger = build_ledger(self.s, "S-1")
        self.assertIn("course_improvement", ledger["current_coverage"])
        self.assertEqual(len(ledger["decisions"]), 1)
        self.assertEqual(len(ledger["withdrawals"]), 1)
        flow = next(f for f in ledger["dataset_flows"] if f["dataset_id"] == ds.dataset_id)
        self.assertTrue(flow["suspended_now"])
        self.assertEqual(flow["records"][0]["decision_id"], ledger["decisions"][0]["event_id"])
        art_ids = {f["artifact_id"] for f in ledger["artifact_flows"]}
        self.assertEqual(art_ids, {export.artifact_id, conclusion.artifact_id})
        prop = ledger["withdrawal_propagation"][0]
        self.assertIn(ds.dataset_id, prop["suspended_datasets"])
        self.assertIn(export.artifact_id, prop["suspended_artifacts"])
        self.assertIn(conclusion.artifact_id, prop["annotated_conclusions"])
        disp = next(t for t in ledger["dispositions"] if t["task_id"] == task.task_id)
        self.assertEqual(disp["progress_percent"], 20)

    def test_ledger_after_restart_shows_certificate(self):
        self.make_dataset(students=("S-1",))
        self.s.withdraw("S-1")
        task = next(iter(self.s.tasks.values()))
        self.s.complete_task(task.task_id)

        restarted = make_service(self.path, self.clock)
        ledger = build_ledger(restarted, "S-1")
        self.assertEqual(len(ledger["certificates"]), 1)
        self.assertTrue(ledger["dataset_flows"][0]["destroyed"])


class RebuildLineageTests(ServiceTestBase):
    def test_successor_snapshot_keeps_minimization_and_fresh_basis(self):
        ds = self.make_dataset(students=("S-1", "S-2"))
        self.s.withdraw("S-1")
        task = next(t for t in self.s.pending_tasks() if t.target_id == ds.dataset_id)
        done = self.s.complete_task(task.task_id)
        successor = self.s.datasets[done.result_dataset_id]
        self.assertEqual(successor.student_ids(), {"S-2"})
        entry = successor.snapshot[0]
        self.assertNotIn("video_face", entry.fields)
        # 后继快照重新固化当时的授权依据
        self.assertTrue(entry.decision_id)
        self.assertEqual(self.s.datasets[ds.dataset_id].rebuilt_as, successor.dataset_id)

    def test_propagation_follows_multilevel_lineage(self):
        ds = self.make_dataset(students=("S-1", "S-2"))
        export = self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出", dataset_id=ds.dataset_id)
        report = self.s.derive_artifact(
            C.ARTIFACT_REPORT, "报告", parent_artifact_id=export.artifact_id)
        result = self.s.withdraw("S-1")
        self.assertIn(report.artifact_id, result["suspended_artifacts"])

    def test_artifact_rebuild_excludes_withdrawn_student(self):
        ds = self.make_dataset(students=("S-1", "S-2", "S-3"))
        export = self.s.derive_artifact(
            C.ARTIFACT_EXPORT, "导出", dataset_id=ds.dataset_id,
            student_ids=["S-1", "S-2", "S-3"])
        self.s.withdraw("S-1")
        # 数据集重建 + 导出重建两个任务
        ds_task = next(t for t in self.s.pending_tasks()
                       if t.target_kind == C.TARGET_DATASET and t.action == C.ACTION_REBUILD)
        art_task = next(t for t in self.s.pending_tasks()
                        if t.target_kind == C.TARGET_ARTIFACT and t.action == C.ACTION_REBUILD)
        self.s.complete_task(ds_task.task_id)
        done = self.s.complete_task(art_task.task_id)
        self.assertTrue(self.s.artifacts[export.artifact_id].destroyed)
        successor = self.s.artifacts[done.result_artifact_id]
        self.assertEqual(set(successor.student_ids), {"S-2", "S-3"})
        # 后继产物挂在重建后的数据集上
        self.assertEqual(successor.dataset_id, self.s.datasets[ds.dataset_id].rebuilt_as)

    def test_single_student_artifact_is_destroyed(self):
        ds = self.make_dataset(students=("S-1",))
        export = self.s.derive_artifact(C.ARTIFACT_EXPORT, "导出", dataset_id=ds.dataset_id)
        self.s.withdraw("S-1")
        art_task = next(t for t in self.s.pending_tasks() if t.target_kind == C.TARGET_ARTIFACT)
        self.assertEqual(art_task.action, C.ACTION_DESTROY)

    def test_ledger_records_resume_after_reconsent(self):
        from consent_governance.ledger import build_ledger
        self.make_dataset(students=("S-1", "S-2"))
        self.s.withdraw("S-1")
        self.s.record_consent("S-1", self.policy.policy_id)
        ledger = build_ledger(self.s, "S-1")
        prop = ledger["withdrawal_propagation"][0]
        self.assertTrue(prop["resumed_after"])
        self.assertTrue(ledger["current_coverage"]["course_improvement"]["covered"])
        cancelled = [t for t in ledger["dispositions"] if t["status"] == C.TASK_CANCELLED]
        self.assertTrue(cancelled)


if __name__ == "__main__":
    unittest.main()
