"""学生授权账本：解释"记录为何曾可用、流向何处、撤回如何传播、处置进度如何"。

账本只读取当前重放状态，可在任意时刻（含进程重启后）重建，输出结构化数据；
CLI 负责把它渲染成 DPO 可读的文本。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from . import contracts as C
from .service import CAUSE_DISPUTE, GovernanceService
from .storage import EV_ACCESS_RESUMED

CAUSE_LABELS = {
    C.CAUSE_WITHDRAWAL: "学生撤回",
    C.CAUSE_RETENTION: "保留期届满",
    C.CAUSE_DENIAL: "拒绝决定",
    CAUSE_DISPUTE: "争议封存",
}


def _dt(value: datetime) -> str:
    return value.isoformat()


def build_ledger(service: GovernanceService, student_id: str, *, at: Optional[datetime] = None) -> dict:
    at = at or service.store.clock()

    # 1) 授权决定时间线（同意/拒绝/撤回/重新同意）
    decisions = []
    for c in service.consents.get(student_id, ()):
        decisions.append({
            "at": _dt(c.recorded_at),
            "kind": c.decision,
            "event_id": c.event_id,
            "policy_id": c.policy_id,
            "policy_revision": c.policy_revision,
            "purposes": sorted(c.purposes),
            "field_categories": sorted(c.field_categories),
            "recipients": sorted(c.recipients),
            "retention_days": c.retention_days,
            "retention_deadline": _dt(c.retention_deadline()),
            "expired": c.retention_deadline() <= at,
            "re_consent": False,  # 渲染时根据时间线位置补充
        })
    for i, d in enumerate(decisions):
        d["re_consent"] = i > 0 and d["kind"] == C.DECISION_GRANTED

    withdrawals = []
    for w in service.withdrawals.get(student_id, ()):
        withdrawals.append({
            "at": _dt(w.recorded_at),
            "event_id": w.event_id,
            "scope": w.scope,
            "policy_id": w.policy_id,
            "purpose": w.purpose,
            "request_key": w.request_key,
        })
    disputes = [
        {
            "dispute_id": d.dispute_id,
            "scope": d.scope,
            "policy_id": d.policy_id,
            "purpose": d.purpose,
            "opened_at": _dt(d.opened_at),
            "closed_at": _dt(d.closed_at) if d.closed_at else None,
            "active": d.active,
        }
        for d in service.disputes if d.student_id == student_id
    ]

    # 2) 记录为何曾可用：数据集快照中该生条目及其授权依据
    dataset_flows = []
    for ds in service.datasets.values():
        entries = [e for e in ds.snapshot if e.student_id == student_id]
        if not entries:
            continue
        justifications = []
        for e in entries:
            consent = service._consent_by_event_id(e.decision_id)
            justifications.append({
                "record_id": e.record_id,
                "category": e.category,
                "fields_after_minimization": list(e.fields),
                "decision_id": e.decision_id,
                "decided_at": _dt(consent.recorded_at),
                "policy_id": consent.policy_id,
                "policy_revision": consent.policy_revision,
                "purpose_covered": ds.purpose in consent.purposes,
            })
        dataset_flows.append({
            "dataset_id": ds.dataset_id,
            "policy_id": ds.policy_id,
            "purpose": ds.purpose,
            "field_categories": sorted(ds.field_categories),
            "recipients": sorted(ds.recipients),
            "generated_at": _dt(ds.generated_at),
            "minimization": ds.minimization,
            "records": justifications,
            "destroyed": ds.destroyed,
            "certificate_id": ds.certificate_id,
            "rebuilt_as": ds.rebuilt_as,
            "suspended_now": student_id in ds.suspended_students,
            "suspension_causes": sorted(ds.suspension_causes.get(student_id, set())),
        })

    # 3) 流向哪些产物（含下游多级派生）
    artifact_flows = []
    for art in service.artifacts.values():
        if student_id not in art.student_ids:
            continue
        lineage = service.lineage(art.artifact_id)
        artifact_flows.append({
            "artifact_id": art.artifact_id,
            "kind": art.kind,
            "name": art.name,
            "created_at": _dt(art.created_at),
            "root_datasets": sorted(service._root_datasets(art.artifact_id)),
            "parent_artifact_id": art.parent_artifact_id,
            "descendants": lineage["descendants"],
            "destroyed": art.destroyed,
            "certificate_id": art.certificate_id,
            "rebuilt_as": art.rebuilt_as,
            "suspended_now": student_id in art.suspended_students,
            "suspension_causes": sorted(art.suspension_causes.get(student_id, set())),
            "immutable_conclusion": art.immutable,
            "annotations": list(art.annotations),
        })

    # 4) 授权失效如何传播：直接从事件日志按 trigger 精确还原
    propagation = []
    raw_events = list(service.store.load())

    # 该生因撤回或拒绝失效后的恢复事件（重新同意触发）
    resume_events = [
        (e.get("seq", 0), e["target_kind"], e["target_id"], e["at"])
        for e in raw_events
        if e.get("type") == EV_ACCESS_RESUMED
        and e.get("cause") in (C.CAUSE_WITHDRAWAL, C.CAUSE_DENIAL)
        and student_id in e.get("student_ids", [])
    ]

    withdrawal_records = service.withdrawals.get(student_id, ())
    # 每个失效触发事件（撤回/拒绝）所暂停的目标及其暂停序号
    suspends_by_trigger: dict[str, list[tuple]] = {}
    trigger_events = [w.event_id for w in withdrawal_records] + [
        d["event_id"] for d in decisions if d["kind"] == C.DECISION_DENIED
    ]
    for trigger_id in trigger_events:
        rows = []
        for e in raw_events:
            if (e.get("type") == "access_suspended"
                    and e.get("trigger") == trigger_id
                    and student_id in e.get("student_ids", [])):
                rows.append((e.get("seq", 0), e["target_kind"], e["target_id"]))
        suspends_by_trigger[trigger_id] = rows

    def resumes_for_trigger(trigger_id: str) -> list[dict]:
        """该触发事件暂停的每个目标，在下次再被暂停之前的第一次恢复。"""
        out = []
        for (suspend_seq, kind, tid) in suspends_by_trigger[trigger_id]:
            later_suspend_seqs = [
                s for other in trigger_events
                for (s, k, i) in suspends_by_trigger[other]
                if (k, i) == (kind, tid) and s > suspend_seq
            ]
            next_suspend = min(later_suspend_seqs) if later_suspend_seqs else None
            for (rseq, rkind, rtid, at) in resume_events:
                if (rkind, rtid) != (kind, tid) or rseq <= suspend_seq:
                    continue
                if next_suspend is not None and rseq > next_suspend:
                    continue
                out.append({"target_kind": kind, "target_id": tid, "at": at})
                break
        return out

    for w in withdrawal_records:
        result = service.propagation_result(w.event_id)
        propagation.append({
            "kind": "withdrawal",
            "event_id": w.event_id,
            "withdrawal_event_id": w.event_id,
            "at": _dt(w.recorded_at),
            "scope": w.scope,
            "policy_id": w.policy_id,
            "purpose": w.purpose,
            "suspended_datasets": result["suspended_datasets"],
            "suspended_artifacts": result["suspended_artifacts"],
            "opened_tasks": result["opened_tasks"],
            "annotated_conclusions": result["annotated_conclusions"],
            "resumed_after": resumes_for_trigger(w.event_id),
        })

    # 拒绝决定（推翻先前同意）同样导致失效，按其事件 trigger 还原传播
    for d in decisions:
        if d["kind"] != C.DECISION_DENIED:
            continue
        result = service.propagation_result(d["event_id"])
        if not (result["suspended_datasets"] or result["suspended_artifacts"]
                or result["annotated_conclusions"]):
            continue
        propagation.append({
            "kind": "denial",
            "event_id": d["event_id"],
            "withdrawal_event_id": d["event_id"],
            "at": d["at"],
            "scope": C.SCOPE_POLICY,
            "policy_id": d["policy_id"],
            "purpose": None,
            "suspended_datasets": result["suspended_datasets"],
            "suspended_artifacts": result["suspended_artifacts"],
            "opened_tasks": result["opened_tasks"],
            "annotated_conclusions": result["annotated_conclusions"],
            "resumed_after": resumes_for_trigger(d["event_id"]),
        })

    # 5) 处置任务与销毁证明（重启后仍可读到进度）
    dispositions = []
    for t in service.tasks.values():
        if student_id not in t.student_ids:
            continue
        dispositions.append({
            "task_id": t.task_id,
            "target_kind": t.target_kind,
            "target_id": t.target_id,
            "student_ids": sorted(t.student_ids),
            "action": t.action,
            "cause": t.cause,
            "status": t.status,
            "progress_percent": t.progress_percent,
            "detail": t.detail,
            "blocked_reason": t.blocked_reason,
            "rationale": t.rationale,
            "created_at": _dt(t.created_at),
            "updated_at": _dt(t.updated_at),
            "result_dataset_id": t.result_dataset_id,
            "result_artifact_id": t.result_artifact_id,
            "certificate_id": t.certificate_id,
        })

    certificates = [
        {
            "certificate_id": c.certificate_id,
            "target_kind": c.target_kind,
            "target_id": c.target_id,
            "method": c.method,
            "certified_at": _dt(c.certified_at),
            "task_id": c.task_id,
            "request_key": c.request_key,
        }
        for c in service.certificates.values() if student_id in c.student_ids
    ]

    # 当前覆盖总览（账本抬头：现在还能用吗，为什么）
    current = {}
    all_purposes = sorted({p for c in service.consents.get(student_id, ()) for p in c.purposes})
    for purpose in all_purposes:
        current[purpose] = {"covered": False, "reasons": []}
        cov = service.effective_consent(student_id, purpose, at=at)
        current[purpose] = {"covered": cov.covered, "decision_id": cov.decision_id, "reasons": cov.reasons}

    propagation.sort(key=lambda b: b["at"])
    return {
        "student_id": student_id,
        "generated_at": _dt(at),
        "current_coverage": current,
        "decisions": decisions,
        "withdrawals": withdrawals,
        "disputes": disputes,
        "dataset_flows": dataset_flows,
        "artifact_flows": artifact_flows,
        "withdrawal_propagation": propagation,
        "dispositions": dispositions,
        "certificates": certificates,
    }

