"""学习数据授权治理核心服务。

所有状态变更都以仅追加事件落盘，再经重放还原；撤回、销毁、审批进度均可在重启后恢复。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

from . import contracts as C
from .errors import (
    ConsentError,
    CoverageError,
    DatasetError,
    DispositionError,
    DisputeHoldError,
    GovernanceError,
    ImmutableConclusionError,
    PolicyError,
    SegregationOfDutiesError,
)
from .models import (
    AccessRequest,
    ArtifactRecord,
    Certificate,
    ConsentRecord,
    DatasetRecord,
    DispositionTask,
    Dispute,
    PolicyRevision,
    WithdrawalRecord,
    parse_dt,
)
from .storage import (
    EV_ACCESS_REQUEST_DECIDED,
    EV_ACCESS_REQUEST_SUBMITTED,
    EV_ACCESS_RESUMED,
    EV_ACCESS_SUSPENDED,
    EV_ARTIFACT_DERIVED,
    EV_CONCLUSION_ANNOTATED,
    EV_CONSENT_RECORDED,
    EV_CONSENT_WITHDRAWN,
    EV_DATASET_GENERATED,
    EV_DESTRUCTION_CERTIFIED,
    EV_DISPUTE_CLOSED,
    EV_DISPUTE_OPENED,
    EV_POLICY_REGISTERED,
    EV_TASK_OPENED,
    EV_TASK_PROGRESSED,
    EventStore,
)

CAUSE_DISPUTE = "dispute"


@dataclass
class Coverage:
    """某次使用请求的授权覆盖判定结果。"""
    covered: bool
    decision_id: Optional[str] = None
    reasons: list[str] = field(default_factory=list)


class GovernanceService:
    def __init__(self, store: EventStore) -> None:
        self.store = store
        # 当前状态
        self.policies: dict[str, list[PolicyRevision]] = {}
        self.consents: dict[str, list[ConsentRecord]] = {}
        self.withdrawals: dict[str, list[WithdrawalRecord]] = {}
        self.datasets: dict[str, DatasetRecord] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.tasks: dict[str, DispositionTask] = {}
        self.certificates: dict[str, Certificate] = {}
        self.requests: dict[str, AccessRequest] = {}
        self.disputes: list[Dispute] = []
        # 幂等键
        self._task_dedupe: set[str] = set()
        self._request_keys: dict[str, dict] = {}
        self._counter = 0
        self._seq = 0
        for event in store.load():
            self._apply(event)
            self._seq = max(self._seq, int(event.get("seq", 0)) + 1)

    # ------------------------------------------------------------------ id
    def _id(self, prefix: str) -> str:
        self._counter += 1
        return self.store.next_id(prefix)

    def _emit(self, event_type: str, payload: dict) -> dict:
        """追加事件并写入单调序号；固定时钟下序号决定事件先后。"""
        event = self.store.append(event_type, {"seq": self._seq, **payload})
        self._seq += 1
        return event

    # ---------------------------------------------------------- 授权文本
    def register_policy(
        self,
        display_name: str,
        purposes: Iterable[str],
        field_categories: Iterable[str],
        retention_days: int,
        recipients: Iterable[str],
        note: str = "",
        policy_id: Optional[str] = None,
    ) -> PolicyRevision:
        """登记授权文本；同一 policy_id 再次登记即产生新版本。

        新版本扩大用途不会自动扩展学生在旧版本上给出的授权——覆盖判定始终以
        学生作出决定时固化的快照为准。
        """
        purposes = frozenset(purposes)
        field_categories = frozenset(field_categories)
        recipients = frozenset(recipients)
        if not display_name or not purposes or not field_categories or not recipients:
            raise PolicyError("授权文本的用途、字段类别与接收机构均不能为空")
        if retention_days < 1:
            raise PolicyError("保留期限必须至少为 1 天")
        pid = policy_id or self._id("P")
        revision = len(self.policies.get(pid, ())) + 1
        event = self._emit(EV_POLICY_REGISTERED, {
            "policy_id": pid,
            "display_name": display_name,
            "revision": revision,
            "purposes": sorted(purposes),
            "field_categories": sorted(field_categories),
            "retention_days": retention_days,
            "recipients": sorted(recipients),
            "note": note,
        })
        self._apply(event)
        return self.policies[pid][-1]

    def policy(self, policy_id: str, revision: Optional[int] = None) -> PolicyRevision:
        revisions = self.policies.get(policy_id)
        if not revisions:
            raise PolicyError(f"授权文本不存在: {policy_id}")
        if revision is None:
            return revisions[-1]
        for rev in revisions:
            if rev.revision == revision:
                return rev
        raise PolicyError(f"授权文本 {policy_id} 不存在版本 {revision}")

    def policy_diff(self, policy_id: str) -> dict:
        """对比最新两版授权文本，回答"新用途是否被旧授权覆盖"。"""
        revisions = self.policies.get(policy_id, [])
        if len(revisions) < 2:
            return {"policy_id": policy_id, "changed": False}
        old, new = revisions[-2], revisions[-1]
        return {
            "policy_id": policy_id,
            "changed": True,
            "from_revision": old.revision,
            "to_revision": new.revision,
            "added_purposes": sorted(new.purposes - old.purposes),
            "removed_purposes": sorted(old.purposes - new.purposes),
            "added_field_categories": sorted(new.field_categories - old.field_categories),
            "added_recipients": sorted(new.recipients - old.recipients),
            "retention_days_changed": old.retention_days != new.retention_days,
            "new_purposes_require_reconsent": True,
        }

    # ---------------------------------------------------------- 同意记录
    def record_consent(
        self,
        student_id: str,
        policy_id: str,
        decision: str = C.DECISION_GRANTED,
    ) -> ConsentRecord:
        """记录同意或拒绝；覆盖范围在决定时刻按该版文本固化。"""
        if decision not in (C.DECISION_GRANTED, C.DECISION_DENIED):
            raise ConsentError("决定只能是 granted 或 denied")
        rev = self.policy(policy_id)
        event_id = self._id("A")
        payload = {
            "event_id": event_id,
            "student_id": student_id,
            "policy_id": policy_id,
            "policy_revision": rev.revision,
            "decision": decision,
            "purposes": sorted(rev.purposes),
            "field_categories": sorted(rev.field_categories),
            "recipients": sorted(rev.recipients),
            "retention_days": rev.retention_days,
        }
        event = self._emit(EV_CONSENT_RECORDED, payload)
        self._apply(event)
        record = self.consents[student_id][-1]
        if decision == C.DECISION_GRANTED:
            self._restore_on_reconsent(student_id)
        else:
            # 先前同意、现作出拒绝：授权失效，按文本范围向数据集与下游传播
            had_grant = any(
                c.decision == C.DECISION_GRANTED and c.policy_id == policy_id
                for c in self.consents[student_id][:-1]
            )
            if had_grant:
                self._propagate(
                    student_id, C.SCOPE_POLICY, policy_id, None,
                    cause=C.CAUSE_DENIAL, trigger_event_id=record.event_id,
                )
        return record

    def _restore_on_reconsent(self, student_id: str) -> None:
        """重新同意后：恢复仍被授权覆盖的访问，取消尚未执行的处置任务。

        争议封存导致的暂停不受影响；已销毁的目标不复活（销毁不可撤销）。
        """
        for ds in self.datasets.values():
            if ds.destroyed or student_id not in ds.student_ids():
                continue
            causes = ds.suspension_causes.get(student_id, set())
            if (causes and CAUSE_DISPUTE not in causes
                    and self.effective_consent(
                        student_id, ds.purpose, ds.field_categories, ds.recipients
                    ).covered):
                for cause in list(causes):
                    self._resume(C.TARGET_DATASET, ds.dataset_id, student_id, cause)
                self._cancel_pending_tasks(C.TARGET_DATASET, ds.dataset_id, student_id)
        for art in self.artifacts.values():
            if art.destroyed or art.immutable or student_id not in art.student_ids:
                continue
            causes = art.suspension_causes.get(student_id, set())
            if not causes or CAUSE_DISPUTE in causes:
                continue
            # 根源数据集均已销毁（重建/销毁处置已执行）→ 产物不直接复活，
            # 交由其处置任务从后继数据集重建或销毁
            live_roots = [
                r for r in self._root_datasets(art.artifact_id)
                if not self.datasets[r].destroyed
            ]
            if not live_roots:
                continue
            # 现存根源数据集仍因授权问题暂停该生时，产物不恢复
            if any(
                self.datasets[r].suspension_causes.get(student_id, set()) - {CAUSE_DISPUTE}
                for r in live_roots
            ):
                continue
            for cause in list(causes):
                self._resume(C.TARGET_ARTIFACT, art.artifact_id, student_id, cause)
            self._cancel_pending_tasks(C.TARGET_ARTIFACT, art.artifact_id, student_id)

    def _cancel_pending_tasks(self, target_kind: str, target_id: str, student_id: str) -> None:
        for task in list(self.tasks.values()):
            if (task.target_kind == target_kind and task.target_id == target_id
                    and task.student_ids == frozenset({student_id})
                    and task.status in (C.TASK_OPEN, C.TASK_IN_PROGRESS, C.TASK_BLOCKED)):
                self._emit_task_progress(
                    task.task_id, C.TASK_CANCELLED, task.progress_percent,
                    "学生重新同意且授权覆盖该用途，处置取消",
                )
                self._task_dedupe.discard(task.dedupe_key)

    def withdraw(
        self,
        student_id: str,
        scope: str = C.SCOPE_ALL,
        policy_id: Optional[str] = None,
        purpose: Optional[str] = None,
        request_key: Optional[str] = None,
    ) -> dict:
        """记录撤回并向所有含该生记录的数据集与下游产物传播。

        同一 request_key 重复提交只处理一次（幂等）。
        """
        if scope not in C.SCOPE_KINDS:
            raise ConsentError("撤回范围不合法")
        if scope == C.SCOPE_POLICY and not policy_id:
            raise ConsentError("按授权文本撤回必须指定 policy_id")
        if scope == C.SCOPE_PURPOSE and not purpose:
            raise ConsentError("按用途撤回必须指定 purpose")
        if request_key:
            existing = self._request_keys.get(request_key)
            if existing and "withdrawal_event_id" in existing:
                # 同一撤回请求（含进程重启后重放所见）只登记一次；
                # 原始传播结果从事件日志按 trigger 精确还原。
                event_id = existing["withdrawal_event_id"]
                return {"withdrawal_event_id": event_id, **self.propagation_result(event_id)}

        event_id = self._id("W")
        event = self._emit(EV_CONSENT_WITHDRAWN, {
            "event_id": event_id,
            "student_id": student_id,
            "scope": scope,
            "policy_id": policy_id,
            "purpose": purpose,
            "request_key": request_key,
        })
        self._apply(event)
        propagation = self._propagate(
            student_id, scope, policy_id, purpose,
            cause=C.CAUSE_WITHDRAWAL, trigger_event_id=event_id,
        )
        result = {"withdrawal_event_id": event_id, **propagation}
        if request_key:
            self._request_keys[request_key] = result
        return result

    # ---------------------------------------------------------- 覆盖判定
    def effective_consent(
        self,
        student_id: str,
        purpose: str,
        field_categories: Iterable[str] = (),
        recipients: Iterable[str] = (),
        at: Optional[datetime] = None,
    ) -> Coverage:
        """判定某学生在指定时刻对某用途/字段/接收机构是否有有效授权。"""
        at = at or self.store.clock()
        wanted_cats = frozenset(field_categories)
        wanted_recipients = frozenset(recipients)
        # 覆盖该用途的最新决定优先：之后的拒绝会推翻先前同意
        decisions = [
            c for c in self.consents.get(student_id, ())
            if c.recorded_at <= at and purpose in c.purposes
        ]
        if not decisions:
            return Coverage(False, reasons=["no_consent"])
        latest = decisions[-1]
        if latest.decision == C.DECISION_DENIED:
            return Coverage(False, reasons=["denied"])
        consent = latest

        missing_cats = wanted_cats - consent.field_categories
        missing_recipients = wanted_recipients - consent.recipients
        expired = consent.retention_deadline() <= at
        withdrawn_by = self._withdrawal_for(consent, purpose, at)
        reasons: list[str] = []
        if missing_cats:
            reasons.append("field_categories_not_covered:" + ",".join(sorted(missing_cats)))
        if missing_recipients:
            reasons.append("recipients_not_covered:" + ",".join(sorted(missing_recipients)))
        if expired:
            reasons.append("retention_expired")
        if withdrawn_by is not None:
            reasons.append(f"withdrawn:{withdrawn_by.scope}:{withdrawn_by.event_id}")
        if not reasons:
            return Coverage(True, decision_id=consent.event_id)
        return Coverage(False, reasons=reasons)

    def _withdrawal_for(
        self, consent: ConsentRecord, purpose: str, at: datetime
    ) -> Optional[WithdrawalRecord]:
        for w in self.withdrawals.get(consent.student_id, ()):
            # 撤回必须晚于所针对的同意（用单调序号判定，时钟同刻也不会错序）
            if not (w.seq > consent.seq and w.recorded_at <= at):
                continue
            if w.scope == C.SCOPE_ALL:
                return w
            if w.scope == C.SCOPE_POLICY and w.policy_id == consent.policy_id:
                return w
            if w.scope == C.SCOPE_PURPOSE and w.purpose == purpose and purpose in consent.purposes:
                return w
        return None

    # ---------------------------------------------------------- 数据集
    def generate_dataset(
        self,
        purpose: str,
        source_records: Iterable[dict],
        field_categories: Iterable[str],
        recipients: Iterable[str],
        minimization_rules: Optional[dict] = None,
        policy_id: Optional[str] = None,
        dataset_id: Optional[str] = None,
        _preminimized: bool = False,
    ) -> DatasetRecord:
        """生成数据集：逐条校验授权、固化来源快照与最小化规则。"""
        records = list(source_records)
        if not records:
            raise DatasetError("空数据集不允许生成")
        cats = frozenset(field_categories)
        recips = frozenset(recipients)
        rules = minimization_rules or {}
        snapshot = []
        failures: dict[str, list[str]] = {}
        for rec in records:
            sid = rec["student_id"]
            coverage = self.effective_consent(sid, purpose, cats, recips)
            if not coverage.covered:
                failures[sid] = coverage.reasons
                continue
            snapshot.append(C.SnapshotEntry(
                record_id=rec["record_id"],
                student_id=sid,
                category=rec["category"],
                fields=tuple(
                    list(rec.get("fields", ())) if _preminimized
                    else self._apply_minimization(rec.get("fields", ()), rules)
                ),
                decision_id=coverage.decision_id,
            ))
        if failures:
            raise CoverageError(
                "存在无有效授权的来源记录，数据集不得生成",
                student_id=";".join(sorted(failures)),
                purpose=purpose,
            )
        pid = policy_id or self._infer_policy_id(snapshot)
        ds_id = dataset_id or self._id("D")
        event = self._emit(EV_DATASET_GENERATED, {
            "dataset_id": ds_id,
            "policy_id": pid,
            "purpose": purpose,
            "field_categories": sorted(cats),
            "recipients": sorted(recips),
            "snapshot": [
                {
                    "record_id": e.record_id,
                    "student_id": e.student_id,
                    "category": e.category,
                    "fields": list(e.fields),
                    "decision_id": e.decision_id,
                }
                for e in snapshot
            ],
            "minimization": self._freeze_rules(rules),
        })
        self._apply(event)
        return self.datasets[ds_id]

    @staticmethod
    def _apply_minimization(fields: Iterable[str], rules: dict) -> list[str]:
        fields = list(fields)
        drop = set(rules.get("drop_fields", ()))
        hashed = set(rules.get("hash_fields", ()))
        out = []
        for f in fields:
            if f in drop:
                continue
            out.append(f"sha256({f})" if f in hashed else f)
        return out

    @staticmethod
    def _freeze_rules(rules: dict) -> dict:
        return {
            "rule_version": rules.get("rule_version", "1"),
            "drop_categories": sorted(rules.get("drop_categories", ())),
            "drop_fields": sorted(rules.get("drop_fields", ())),
            "hash_fields": sorted(rules.get("hash_fields", ())),
            "k_anonymity": rules.get("k_anonymity"),
        }

    def _infer_policy_id(self, snapshot: list[C.SnapshotEntry]) -> str:
        policy_ids = {
            self._consent_by_event_id(e.decision_id).policy_id for e in snapshot
        }
        if len(policy_ids) == 1:
            return next(iter(policy_ids))
        raise DatasetError("跨多个授权文本生成数据集须显式指定 policy_id")

    def _consent_by_event_id(self, event_id: str) -> ConsentRecord:
        for records in self.consents.values():
            for c in records:
                if c.event_id == event_id:
                    return c
        raise ConsentError(f"找不到授权事件 {event_id}")

    def derive_artifact(
        self,
        kind: str,
        name: str,
        dataset_id: Optional[str] = None,
        parent_artifact_id: Optional[str] = None,
        student_ids: Optional[Iterable[str]] = None,
    ) -> ArtifactRecord:
        """从数据集或上游产物派生导出文件、分析报告或研究结论。"""
        if kind not in C.ARTIFACT_KINDS:
            raise DatasetError("产物类型不合法")
        if not (dataset_id or parent_artifact_id):
            raise DatasetError("派生产物必须指定数据集或上游产物")
        parent_dataset, parent_artifact = None, None
        if dataset_id:
            parent_dataset = self.datasets.get(dataset_id)
            if not parent_dataset or parent_dataset.destroyed:
                raise DatasetError("来源数据集不存在或已销毁")
        if parent_artifact_id:
            parent_artifact = self.artifacts.get(parent_artifact_id)
            if not parent_artifact or parent_artifact.destroyed:
                raise DatasetError("上游产物不存在或已销毁")
        available = self._available_students(parent_dataset, parent_artifact)
        chosen = frozenset(student_ids) if student_ids is not None else available
        if not chosen:
            raise DatasetError("产物必须包含至少一名学生的记录")
        if not chosen <= available:
            blocked = sorted(chosen - available)
            raise DatasetError(f"部分学生记录当前不可用（暂停或未授权）: {blocked}")
        artifact_id = self._id("F" if kind == C.ARTIFACT_EXPORT else "R")
        event = self._emit(EV_ARTIFACT_DERIVED, {
            "artifact_id": artifact_id,
            "kind": kind,
            "name": name,
            "dataset_id": dataset_id,
            "parent_artifact_id": parent_artifact_id,
            "student_ids": sorted(chosen),
        })
        self._apply(event)
        return self.artifacts[artifact_id]

    def _available_students(
        self, dataset: Optional[DatasetRecord], artifact: Optional[ArtifactRecord]
    ) -> frozenset[str]:
        if dataset is not None:
            if dataset.suspended:
                return frozenset(dataset.student_ids() - dataset.suspended_students)
            return frozenset(dataset.student_ids())
        if artifact is not None:
            if artifact.suspended:
                return frozenset(artifact.student_ids - artifact.suspended_students)
            return artifact.student_ids
        return frozenset()

    # ---------------------------------------------------------- 谱系
    def lineage(self, target_id: str) -> dict:
        """返回某数据集/产物向上与向下的派生谱系。"""
        return {"ancestors": self._ancestors(target_id), "descendants": self._descendants(target_id)}

    def _ancestors(self, target_id: str) -> list[str]:
        out: list[str] = []
        dataset = self.datasets.get(target_id)
        artifact = self.artifacts.get(target_id)
        if artifact is not None:
            if artifact.dataset_id:
                out.append(artifact.dataset_id)
            if artifact.parent_artifact_id:
                out.extend(self._ancestors(artifact.parent_artifact_id))
        return out

    def _descendants(self, target_id: str) -> list[str]:
        out: list[str] = []
        for art in self.artifacts.values():
            if art.dataset_id == target_id or art.parent_artifact_id == target_id:
                out.append(art.artifact_id)
                out.extend(self._descendants(art.artifact_id))
        return out

    def _root_datasets(self, artifact_id: str) -> set[str]:
        roots: set[str] = set()
        art = self.artifacts[artifact_id]
        if art.dataset_id:
            roots.add(art.dataset_id)
        if art.parent_artifact_id:
            roots |= self._root_datasets(art.parent_artifact_id)
        return roots

    def propagation_result(self, trigger_event_id: str) -> dict:
        """从事件日志按 trigger 精确还原某次撤回的原始传播结果。"""
        suspended_datasets: list[str] = []
        suspended_artifacts: list[str] = []
        opened_tasks: list[str] = []
        for e in self.store.load():
            if e.get("type") == "access_suspended" and e.get("trigger") == trigger_event_id:
                bucket = (suspended_datasets if e["target_kind"] == C.TARGET_DATASET
                          else suspended_artifacts)
                bucket.append(e["target_id"])
            elif e.get("type") == "disposition_task_opened" and e.get("trigger") == trigger_event_id:
                opened_tasks.append(e["task_id"])
        # 结论标注的 ref 形如 withdrawal:S-xxx:<撤回事件>
        annotated = sorted({
            e["artifact_id"]
            for e in self.store.load()
            if e.get("type") == "conclusion_annotated"
            and trigger_event_id in e.get("annotation", {}).get("ref", "")
        })
        return {
            "suspended_datasets": suspended_datasets,
            "suspended_artifacts": suspended_artifacts,
            "opened_tasks": opened_tasks,
            "annotated_conclusions": annotated,
        }

    # ---------------------------------------------------------- 撤回传播
    def _match_scope(
        self, scope: str, policy_id: Optional[str], purpose: Optional[str],
        ds_policy: str, ds_purpose: str,
    ) -> bool:
        if scope == C.SCOPE_ALL:
            return True
        if scope == C.SCOPE_POLICY:
            return ds_policy == policy_id
        return ds_purpose == purpose

    def _propagate(
        self,
        student_id: str,
        scope: str,
        policy_id: Optional[str],
        purpose: Optional[str],
        *,
        cause: str,
        trigger_event_id: str,
    ) -> dict:
        suspended_datasets: list[str] = []
        suspended_artifacts: list[str] = []
        opened_tasks: list[str] = []
        annotated: list[str] = []
        affected_dataset_ids: set[str] = set()

        for ds in list(self.datasets.values()):
            if ds.destroyed or student_id not in ds.student_ids():
                continue
            if not self._match_scope(scope, policy_id, purpose, ds.policy_id, ds.purpose):
                continue
            affected_dataset_ids.add(ds.dataset_id)
            if self._suspend(C.TARGET_DATASET, ds.dataset_id, student_id, cause,
                             trigger=trigger_event_id):
                suspended_datasets.append(ds.dataset_id)
            task_id = self._open_dataset_task(ds, student_id, cause, trigger_event_id)
            if task_id:
                opened_tasks.append(task_id)

        for art in list(self.artifacts.values()):
            if art.destroyed or student_id not in art.student_ids:
                continue
            if not (self._root_datasets(art.artifact_id) & affected_dataset_ids):
                continue
            if art.immutable:
                # 历史研究结论：正文不得改写，仅追加撤回传播标注
                if self._annotate_conclusion(
                    art, student_id, cause, trigger_event_id
                ):
                    annotated.append(art.artifact_id)
                continue
            if self._suspend(C.TARGET_ARTIFACT, art.artifact_id, student_id, cause,
                             trigger=trigger_event_id):
                suspended_artifacts.append(art.artifact_id)
            task_id = self._open_artifact_task(art, student_id, cause, trigger_event_id)
            if task_id:
                opened_tasks.append(task_id)

        return {
            "suspended_datasets": suspended_datasets,
            "suspended_artifacts": suspended_artifacts,
            "opened_tasks": opened_tasks,
            "annotated_conclusions": annotated,
        }

    def _suspend(self, target_kind: str, target_id: str, student_id: str, cause: str,
                 trigger: str = "") -> bool:
        """叠加一个暂停起因；撤回、保留期、争议可并存。"""
        target = self.datasets[target_id] if target_kind == C.TARGET_DATASET else self.artifacts[target_id]
        causes = target.suspension_causes.setdefault(student_id, set())
        if cause in causes:
            return False
        event = self._emit(EV_ACCESS_SUSPENDED, {
            "target_kind": target_kind,
            "target_id": target_id,
            "student_ids": [student_id],
            "cause": cause,
            "trigger": trigger,
        })
        self._apply(event)
        return True

    def _resume(self, target_kind: str, target_id: str, student_id: str, cause: str) -> bool:
        """仅解除指定起因；其他起因（如争议仍在）继续维持暂停。"""
        target = self.datasets[target_id] if target_kind == C.TARGET_DATASET else self.artifacts[target_id]
        if cause not in target.suspension_causes.get(student_id, set()):
            return False
        event = self._emit(EV_ACCESS_RESUMED, {
            "target_kind": target_kind,
            "target_id": target_id,
            "student_ids": [student_id],
            "cause": cause,
        })
        self._apply(event)
        return True

    def _open_dataset_task(self, ds: DatasetRecord, student_id: str, cause: str,
                           trigger: str = "") -> Optional[str]:
        remaining = ds.student_ids() - {student_id}
        # 重建后仍有其他当下获授权学生 → 重建；否则整集销毁
        rebuildable = any(
            self.effective_consent(s, ds.purpose, ds.field_categories, ds.recipients).covered
            for s in remaining
        )
        action = C.ACTION_REBUILD if rebuildable else C.ACTION_DESTROY
        return self._open_task(C.TARGET_DATASET, ds.dataset_id, [student_id], action, cause, trigger)

    def _open_artifact_task(self, art: ArtifactRecord, student_id: str, cause: str,
                            trigger: str = "") -> Optional[str]:
        # 其他仍可访问（未暂停且授权有效）的学生 → 重建而非整份销毁
        others = set(art.student_ids) - {student_id} - art.suspended_students
        action = C.ACTION_REBUILD if others else C.ACTION_DESTROY
        return self._open_task(C.TARGET_ARTIFACT, art.artifact_id, [student_id],
                               action, cause, trigger)

    def _active_dispute_for(self, student_id: str, target_kind: str, target_id: str) -> Optional[Dispute]:
        """该学生是否存在范围覆盖该目标的有效争议。"""
        if target_kind == C.TARGET_DATASET:
            ds = self.datasets.get(target_id)
            policy_id, purpose = (ds.policy_id, ds.purpose) if ds else (None, None)
        else:
            roots = self._root_datasets(target_id)
            policy_id = purpose = None
            if roots:
                root = self.datasets[next(iter(roots))]
                policy_id, purpose = root.policy_id, root.purpose
        for d in self.disputes:
            if not d.active or d.student_id != student_id:
                continue
            if d.scope == C.SCOPE_ALL:
                return d
            if d.scope == C.SCOPE_POLICY and d.policy_id == policy_id:
                return d
            if d.scope == C.SCOPE_PURPOSE and d.purpose == purpose:
                return d
        return None

    def _set_tasks_blocked_for_dispute(self, student_id: str, *, blocked: bool) -> None:
        for task in list(self.tasks.values()):
            if student_id not in task.student_ids:
                continue
            under_dispute = self._active_dispute_for(student_id, task.target_kind, task.target_id)
            if blocked:
                if under_dispute and task.status in (C.TASK_OPEN, C.TASK_IN_PROGRESS):
                    self._emit_task_progress(
                        task.task_id, C.TASK_BLOCKED, task.progress_percent, task.detail,
                        blocked_reason="争议封存中，待争议结论后处置",
                    )
            else:
                if not under_dispute and task.status == C.TASK_BLOCKED \
                        and task.blocked_reason.startswith("争议封存"):
                    status = C.TASK_IN_PROGRESS if task.progress_percent > 0 else C.TASK_OPEN
                    self._emit_task_progress(
                        task.task_id, status, task.progress_percent, task.detail,
                        blocked_reason="",
                    )

    def _open_task(
        self, target_kind: str, target_id: str, student_ids: list[str], action: str,
        cause: str, trigger: str = "",
    ) -> Optional[str]:
        # 同一目标、同一批学生、同一动作只处置一次；撤回与到期殊途同归
        dedupe_key = f"{target_kind}:{target_id}:{','.join(student_ids)}:{action}"
        if dedupe_key in self._task_dedupe:
            return None
        task_id = self._id("T")
        rationale = self._task_rationale(target_kind, action, cause)
        event = self._emit(EV_TASK_OPENED, {
            "task_id": task_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "student_ids": student_ids,
            "action": action,
            "cause": cause,
            "rationale": rationale,
            "dedupe_key": dedupe_key,
            "trigger": trigger,
        })
        self._apply(event)
        # 学生相关争议进行中：处置暂缓，仅保留审计与暂停状态
        if self._active_dispute_for(student_ids[0], target_kind, target_id):
            self._emit_task_progress(
                task_id, C.TASK_BLOCKED, 0, rationale,
                blocked_reason="争议封存中，待争议结论后处置",
            )
        return task_id

    @staticmethod
    def _task_rationale(target_kind: str, action: str, cause: str) -> str:
        cause_text = {
            C.CAUSE_WITHDRAWAL: "学生撤回授权",
            C.CAUSE_RETENTION: "保留期限届满",
            C.CAUSE_DENIAL: "学生作出拒绝决定",
        }.get(cause, cause)
        if action == C.ACTION_REBUILD:
            target_text = "最小化数据集" if target_kind == C.TARGET_DATASET else "剔除该生记录后的下游产物"
            action_text = f"重建{target_text}"
        else:
            action_text = "销毁并出具证明"
        return f"{cause_text}，{action_text}"

    # ---------------------------------------------------------- 处置任务
    def pending_tasks(self) -> list[DispositionTask]:
        return [
            t for t in self.tasks.values()
            if t.status not in (C.TASK_COMPLETED, C.TASK_CANCELLED)
        ]

    def progress_task(self, task_id: str, percent: int, detail: str = "") -> DispositionTask:
        """记录处置进度（1-99 进行中），崩溃重启后从事件日志恢复。"""
        task = self._get_task(task_id)
        if task.status == C.TASK_COMPLETED:
            raise DispositionError("任务已完成，不能再更新进度")
        if not 0 <= percent <= 99:
            raise DispositionError("进行中进度必须在 0-99 之间")
        status = C.TASK_IN_PROGRESS if percent > 0 else C.TASK_OPEN
        self._emit_task_progress(task_id, status, percent, detail)
        return self.tasks[task_id]

    def block_task(self, task_id: str, reason: str) -> DispositionTask:
        self._get_task(task_id)
        self._emit_task_progress(task_id, C.TASK_BLOCKED, self.tasks[task_id].progress_percent,
                                 self.tasks[task_id].detail, blocked_reason=reason)
        return self.tasks[task_id]

    def unblock_task(self, task_id: str) -> DispositionTask:
        task = self._get_task(task_id)
        if task.status != C.TASK_BLOCKED:
            raise DispositionError("仅阻塞中的任务可解除阻塞")
        status = C.TASK_IN_PROGRESS if task.progress_percent > 0 else C.TASK_OPEN
        self._emit_task_progress(task_id, status, task.progress_percent, task.detail, blocked_reason="")
        return self.tasks[task_id]

    def complete_task(self, task_id: str, method: str = "secure_deletion") -> DispositionTask:
        """执行处置：销毁出具证明；重建则生成后继数据集并销毁旧集。"""
        task = self._get_task(task_id)
        if task.status == C.TASK_COMPLETED:
            raise DispositionError("任务已完成")
        if task.status == C.TASK_BLOCKED:
            raise DispositionError("任务处于阻塞状态，须先解除阻塞")

        certificate_id = None
        result_dataset_id = None
        result_artifact_id = None
        if task.target_kind == C.TARGET_DATASET and task.action == C.ACTION_REBUILD:
            successor = self._rebuild_dataset(task.target_id, task.student_ids, task)
            result_dataset_id = successor.dataset_id
            certificate_id = successor.certificate_id
        elif task.target_kind == C.TARGET_ARTIFACT and task.action == C.ACTION_REBUILD:
            successor = self._rebuild_artifact(task.target_id, task.student_ids, task)
            result_artifact_id = successor.artifact_id
            certificate_id = successor.certificate_id
        else:
            cert = self._certify_destruction(
                task.target_kind, task.target_id, sorted(task.student_ids),
                task_id=task.task_id, method=method,
            )
            certificate_id = cert.certificate_id
        self._emit_task_progress(
            task_id, C.TASK_COMPLETED, 100,
            f"处置完成：{method}"
            + (f"，后继数据集 {result_dataset_id}" if result_dataset_id else "")
            + (f"，后继产物 {result_artifact_id}" if result_artifact_id else ""),
            result_dataset_id=result_dataset_id, result_artifact_id=result_artifact_id,
            certificate_id=certificate_id,
        )
        return self.tasks[task_id]

    def _rebuild_artifact(self, artifact_id: str, removed_students: frozenset[str],
                          task: DispositionTask):
        """从同一来源重新派生剔除失权学生后的后继产物。"""
        old = self.artifacts[artifact_id]
        kept = [s for s in old.student_ids if s not in removed_students]
        if not kept:
            cert = self._certify_destruction(
                C.TARGET_ARTIFACT, artifact_id, sorted(old.student_ids),
                task_id=task.task_id, method="secure_deletion_rebuild_empty",
            )
            class _R:
                pass
            r = _R(); r.artifact_id = None; r.certificate_id = cert.certificate_id
            return r
        # 来源可能已先行重建：沿 rebuilt_as 找到现存后继
        dataset_id = self._successor_dataset_id(old.dataset_id)
        parent_id = self._successor_artifact_id(old.parent_artifact_id)
        # 仅保留在现存来源上仍可访问的学生
        available = self._available_students(
            self.datasets.get(dataset_id) if dataset_id else None,
            self.artifacts.get(parent_id) if parent_id else None,
        )
        kept = [s for s in kept if s in available]
        if not kept:
            cert = self._certify_destruction(
                C.TARGET_ARTIFACT, artifact_id, sorted(old.student_ids),
                task_id=task.task_id, method="secure_deletion_rebuild_empty",
            )
            class _R:
                pass
            r = _R(); r.artifact_id = None; r.certificate_id = cert.certificate_id
            return r
        successor = self.derive_artifact(
            kind=old.kind,
            name=f"{old.name}（重建剔除 {','.join(sorted(removed_students))}）",
            dataset_id=dataset_id,
            parent_artifact_id=parent_id,
            student_ids=kept,
        )
        cert = self._certify_destruction(
            C.TARGET_ARTIFACT, artifact_id, sorted(removed_students),
            task_id=task.task_id, method=f"rebuild_superseded_by:{successor.artifact_id}",
        )
        class _R:
            pass
        r = _R(); r.artifact_id = successor.artifact_id; r.certificate_id = cert.certificate_id
        return r

    def _successor_dataset_id(self, dataset_id: Optional[str]) -> Optional[str]:
        current = dataset_id
        while current and self.datasets[current].destroyed and self.datasets[current].rebuilt_as:
            current = self.datasets[current].rebuilt_as
        return current

    def _successor_artifact_id(self, artifact_id: Optional[str]) -> Optional[str]:
        current = artifact_id
        while current and self.artifacts[current].destroyed and self.artifacts[current].rebuilt_as:
            current = self.artifacts[current].rebuilt_as
        return current

    def _rebuild_dataset(self, dataset_id: str, removed_students: frozenset[str],
                         task: DispositionTask):
        old = self.datasets[dataset_id]
        kept_entries = []
        for e in old.snapshot:
            if e.student_id in removed_students:
                continue
            coverage = self.effective_consent(
                e.student_id, old.purpose, old.field_categories, old.recipients
            )
            if not coverage.covered:
                # 重建时刻再次失权的学生一并剔除
                removed_students = removed_students | {e.student_id}
                self._suspend(C.TARGET_DATASET, dataset_id, e.student_id, task.cause,
                              trigger=task.trigger)
                continue
            kept_entries.append({
                "record_id": e.record_id,
                "student_id": e.student_id,
                "category": e.category,
                "fields": list(e.fields),
            })
        if not kept_entries:
            cert = self._certify_destruction(
                C.TARGET_DATASET, dataset_id, sorted(old.student_ids()),
                task_id=task.task_id, method="secure_deletion_rebuild_empty",
            )
            class _R:
                pass
            r = _R(); r.dataset_id = None; r.certificate_id = cert.certificate_id
            return r
        successor = self.generate_dataset(
            purpose=old.purpose,
            source_records=kept_entries,
            field_categories=old.field_categories,
            recipients=old.recipients,
            minimization_rules=old.minimization,
            policy_id=old.policy_id,
            _preminimized=True,
        )
        cert = self._certify_destruction(
            C.TARGET_DATASET, dataset_id, sorted(removed_students),
            task_id=task.task_id, method=f"rebuild_superseded_by:{successor.dataset_id}",
        )
        class _R:
            pass
        r = _R(); r.dataset_id = successor.dataset_id; r.certificate_id = cert.certificate_id
        return r

    def register_destruction_receipt(
        self,
        target_kind: str,
        target_id: str,
        student_ids: Iterable[str],
        method: str,
        request_key: str,
    ) -> Certificate:
        """登记接收机构发回的销毁回执；同一回执只处理一次。"""
        if not request_key:
            raise DispositionError("销毁回执必须带 request_key 以保证幂等")
        if request_key in self._request_keys:
            cached = self._request_keys[request_key]
            return self.certificates[cached["certificate_id"]]
        cert = self._certify_destruction(
            target_kind, target_id, sorted(student_ids), method=method, request_key=request_key
        )
        self._request_keys[request_key] = {"certificate_id": cert.certificate_id}
        return cert

    def _certify_destruction(
        self,
        target_kind: str,
        target_id: str,
        student_ids: list[str],
        *,
        task_id: Optional[str] = None,
        method: str = "secure_deletion",
        request_key: Optional[str] = None,
    ) -> Certificate:
        if target_kind == C.TARGET_DATASET:
            target = self.datasets.get(target_id)
        else:
            target = self.artifacts.get(target_id)
        if target is None:
            raise DispositionError(f"处置目标不存在: {target_kind}:{target_id}")
        if getattr(target, "destroyed", False):
            raise DispositionError("目标已销毁，不得重复出具销毁证明")
        certificate_id = self._id("C")
        event = self._emit(EV_DESTRUCTION_CERTIFIED, {
            "certificate_id": certificate_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "student_ids": sorted(student_ids),
            "task_id": task_id,
            "method": method,
            "request_key": request_key,
        })
        self._apply(event)
        # 目标既已销毁，其上其余未完成任务一并结案
        for other in self.tasks.values():
            if (other.target_kind == target_kind and other.target_id == target_id
                    and other.status != C.TASK_COMPLETED and other.task_id != task_id):
                self._emit_task_progress(
                    other.task_id, C.TASK_COMPLETED, other.progress_percent,
                    f"目标已由销毁证明 {certificate_id} 处置，任务结案",
                    certificate_id=certificate_id,
                )
        return self.certificates[certificate_id]

    def _emit_task_progress(self, task_id: str, status: str, percent: int, detail: str,
                            *, blocked_reason: str = "", result_dataset_id: Optional[str] = None,
                            result_artifact_id: Optional[str] = None,
                            certificate_id: Optional[str] = None) -> None:
        event = self._emit(EV_TASK_PROGRESSED, {
            "task_id": task_id,
            "status": status,
            "percent": percent,
            "detail": detail,
            "blocked_reason": blocked_reason,
            "result_dataset_id": result_dataset_id,
            "result_artifact_id": result_artifact_id,
            "certificate_id": certificate_id,
        })
        self._apply(event)

    def _get_task(self, task_id: str) -> DispositionTask:
        task = self.tasks.get(task_id)
        if not task:
            raise DispositionError(f"处置任务不存在: {task_id}")
        return task

    # ---------------------------------------------------------- 访问申请（职责分离）
    def submit_request(
        self, requester: str, target_kind: str, target_id: str, purpose: str
    ) -> AccessRequest:
        if target_kind not in C.TARGET_KINDS:
            raise GovernanceError("申请目标类型不合法")
        target = self.datasets.get(target_id) if target_kind == C.TARGET_DATASET else self.artifacts.get(target_id)
        if target is None or getattr(target, "destroyed", False):
            raise GovernanceError("申请目标不存在或已销毁")
        request_id = self._id("Q")
        event = self._emit(EV_ACCESS_REQUEST_SUBMITTED, {
            "request_id": request_id,
            "requester": requester,
            "target_kind": target_kind,
            "target_id": target_id,
            "purpose": purpose,
        })
        self._apply(event)
        return self.requests[request_id]

    def decide_request(self, request_id: str, approver: str, approved: bool, reason: str = "") -> AccessRequest:
        req = self.requests.get(request_id)
        if not req:
            raise GovernanceError(f"访问申请不存在: {request_id}")
        if req.status != "submitted":
            raise GovernanceError("该申请已审批")
        if approver == req.requester:
            raise SegregationOfDutiesError("申请人不得审批本人的访问申请，职责分离要求第二人审批")
        target = (self.datasets.get(req.target_id) if req.target_kind == C.TARGET_DATASET
                  else self.artifacts.get(req.target_id))
        if approved and (target is None or getattr(target, "destroyed", False)):
            raise GovernanceError("目标已销毁，批准无效")
        if approved and getattr(target, "suspended", False):
            raise GovernanceError("目标处于暂停访问状态（撤回/保留期/争议），不能批准")
        event = self._emit(EV_ACCESS_REQUEST_DECIDED, {
            "request_id": request_id,
            "approver": approver,
            "approved": approved,
            "reason": reason,
        })
        self._apply(event)
        return self.requests[request_id]

    # ---------------------------------------------------------- 争议封存
    def open_dispute(
        self,
        student_id: str,
        scope: str = C.SCOPE_ALL,
        policy_id: Optional[str] = None,
        purpose: Optional[str] = None,
    ) -> Dispute:
        """争议期间保留必要审计，暂停正文数据访问；不销毁任何东西。"""
        if scope not in C.SCOPE_KINDS:
            raise DisputeHoldError("争议范围不合法")
        for d in self.disputes:
            if d.active and d.student_id == student_id and d.scope == scope:
                return d
        dispute_id = self._id("U")
        event = self._emit(EV_DISPUTE_OPENED, {
            "dispute_id": dispute_id,
            "student_id": student_id,
            "scope": scope,
            "policy_id": policy_id,
            "purpose": purpose,
        })
        self._apply(event)
        self._propagate_dispute_hold(student_id, scope, policy_id, purpose, suspend=True)
        self._set_tasks_blocked_for_dispute(student_id, blocked=True)
        return self.disputes[-1]

    def close_dispute(self, dispute_id: str) -> Dispute:
        dispute = next((d for d in self.disputes if d.dispute_id == dispute_id), None)
        if dispute is None:
            raise DisputeHoldError("争议不存在")
        if not dispute.active:
            return dispute
        event = self._emit(EV_DISPUTE_CLOSED, {"dispute_id": dispute_id})
        self._apply(event)
        # 争议解除：仅恢复授权仍然有效的学生记录；已撤回/到期的继续暂停
        self._propagate_dispute_hold(
            dispute.student_id, dispute.scope, dispute.policy_id, dispute.purpose, suspend=False
        )
        # 因争议阻塞的处置任务恢复可执行（撤回/到期导致的暂停与任务仍在）
        self._set_tasks_blocked_for_dispute(dispute.student_id, blocked=False)
        return dispute

    def _propagate_dispute_hold(
        self, student_id: str, scope: str, policy_id: Optional[str],
        purpose: Optional[str], *, suspend: bool
    ) -> None:
        for ds in self.datasets.values():
            if ds.destroyed or student_id not in ds.student_ids():
                continue
            if not self._match_scope(scope, policy_id, purpose, ds.policy_id, ds.purpose):
                continue
            if suspend:
                self._suspend(C.TARGET_DATASET, ds.dataset_id, student_id, CAUSE_DISPUTE)
            else:
                self._resume(C.TARGET_DATASET, ds.dataset_id, student_id, CAUSE_DISPUTE)
        for art in self.artifacts.values():
            if art.destroyed or art.immutable or student_id not in art.student_ids:
                continue
            # 仅当某一未销毁根源数据集落入争议范围时才影响该产物
            root_match = any(
                not self.datasets[r].destroyed
                and self._match_scope(
                    scope, policy_id, purpose,
                    self.datasets[r].policy_id, self.datasets[r].purpose,
                )
                for r in self._root_datasets(art.artifact_id)
            )
            if not root_match:
                continue
            if suspend:
                self._suspend(C.TARGET_ARTIFACT, art.artifact_id, student_id, CAUSE_DISPUTE)
            else:
                # 根源数据集仍因撤回/到期暂停该生时，产物保持暂停
                roots = self._root_datasets(art.artifact_id)
                if any(
                    not self.datasets[r].destroyed
                    and (self.datasets[r].suspension_causes.get(student_id, set()) - {CAUSE_DISPUTE})
                    for r in roots
                ):
                    continue
                self._resume(C.TARGET_ARTIFACT, art.artifact_id, student_id, CAUSE_DISPUTE)

    # ---------------------------------------------------------- 保留期限
    def sweep_retention(self) -> dict:
        """扫描保留期限届满的授权，按撤回同等方式传播暂停与处置。

        以数据集为单位重新判定覆盖；只有失效原因确为保留期届满时才挂起因 retention，
        已被撤回或重新同意覆盖的记录不在此处重复处理（处置任务本身按目标幂等）。
        """
        now = self.store.clock()
        expired: list[dict] = []
        seen: set[tuple] = set()
        suspended_datasets: list[str] = []
        suspended_artifacts: list[str] = []
        opened_tasks: list[str] = []
        affected_dataset_ids: set[str] = set()
        affected_students: set[str] = set()

        for ds in self.datasets.values():
            if ds.destroyed:
                continue
            for sid in ds.student_ids():
                causes = ds.suspension_causes.get(sid, set())
                if C.CAUSE_RETENTION in causes:
                    continue  # 该生已按保留期处理
                coverage = self.effective_consent(
                    sid, ds.purpose, ds.field_categories, ds.recipients, at=now
                )
                if coverage.covered or "retention_expired" not in coverage.reasons:
                    continue
                key = (sid, ds.policy_id)
                if key not in seen:
                    seen.add(key)
                    expired.append({"student_id": sid, "policy_id": ds.policy_id})
                affected_dataset_ids.add(ds.dataset_id)
                affected_students.add(sid)
                if self._suspend(C.TARGET_DATASET, ds.dataset_id, sid, C.CAUSE_RETENTION):
                    suspended_datasets.append(ds.dataset_id)
                task_id = self._open_dataset_task(ds, sid, C.CAUSE_RETENTION)
                if task_id:
                    opened_tasks.append(task_id)

        for art in self.artifacts.values():
            if art.destroyed:
                continue
            if not (self._root_datasets(art.artifact_id) & affected_dataset_ids):
                continue
            if art.immutable:
                for sid in art.student_ids & affected_students:
                    self._annotate_conclusion(art, sid, C.CAUSE_RETENTION, "retention-sweep")
                continue
            for sid in art.student_ids & affected_students:
                if C.CAUSE_RETENTION in art.suspension_causes.get(sid, set()):
                    continue
                if self._suspend(C.TARGET_ARTIFACT, art.artifact_id, sid, C.CAUSE_RETENTION):
                    suspended_artifacts.append(art.artifact_id)
                task_id = self._open_artifact_task(art, sid, C.CAUSE_RETENTION)
                if task_id:
                    opened_tasks.append(task_id)

        return {
            "expired": expired,
            "suspended_datasets": suspended_datasets,
            "suspended_artifacts": suspended_artifacts,
            "opened_tasks": opened_tasks,
        }

    # ---------------------------------------------------------- 结论标注
    def annotate_conclusion(self, artifact_id: str, note: str) -> ArtifactRecord:
        art = self.artifacts.get(artifact_id)
        if art is None:
            raise ImmutableConclusionError("产物不存在")
        if not art.immutable:
            raise ImmutableConclusionError("仅研究结论使用追加标注")
        self._append_annotation(art, {"kind": "manual", "note": note})
        return art

    def _annotate_conclusion(self, art: ArtifactRecord, student_id: str, cause: str, trigger_id: str) -> bool:
        marker = f"{cause}:{student_id}:{trigger_id}"
        if any(marker in a.get("ref", "") for a in art.annotations):
            return False
        cause_text = {
            C.CAUSE_WITHDRAWAL: "撤回授权",
            C.CAUSE_RETENTION: "保留期届满",
            C.CAUSE_DENIAL: "作出拒绝决定",
        }.get(cause, cause)
        self._append_annotation(art, {
            "kind": "withdrawal_propagation",
            "ref": marker,
            "text": f"学生 {student_id} 因{cause_text}；本结论正文作为历史记录保持不变，仅追加此标注。",
        })
        return True

    def _append_annotation(self, art: ArtifactRecord, annotation: dict) -> None:
        event = self._emit(EV_CONCLUSION_ANNOTATED, {
            "artifact_id": art.artifact_id,
            "annotation": {"at": self.store.clock().isoformat(), **annotation},
        })
        self._apply(event)

    # ========================================================== 事件应用
    def _apply(self, event: dict) -> None:
        t = event["type"]
        p = event
        if t == EV_POLICY_REGISTERED:
            rev = PolicyRevision(
                policy_id=p["policy_id"], display_name=p["display_name"],
                revision=p["revision"], purposes=frozenset(p["purposes"]),
                field_categories=frozenset(p["field_categories"]),
                retention_days=p["retention_days"], recipients=frozenset(p["recipients"]),
                registered_at=parse_dt(event["at"]), note=p.get("note", ""),
            )
            self.policies.setdefault(p["policy_id"], []).append(rev)
        elif t == EV_CONSENT_RECORDED:
            rec = ConsentRecord(
                event_id=p["event_id"], student_id=p["student_id"],
                policy_id=p["policy_id"], policy_revision=p["policy_revision"],
                decision=p["decision"], purposes=frozenset(p["purposes"]),
                field_categories=frozenset(p["field_categories"]),
                recipients=frozenset(p["recipients"]), retention_days=p["retention_days"],
                recorded_at=parse_dt(event["at"]), seq=p.get("seq", 0),
            )
            self.consents.setdefault(p["student_id"], []).append(rec)
        elif t == EV_CONSENT_WITHDRAWN:
            w = WithdrawalRecord(
                event_id=p["event_id"], student_id=p["student_id"], scope=p["scope"],
                policy_id=p.get("policy_id"), purpose=p.get("purpose"),
                recorded_at=parse_dt(event["at"]), request_key=p.get("request_key"),
                seq=p.get("seq", 0),
            )
            self.withdrawals.setdefault(p["student_id"], []).append(w)
            if p.get("request_key"):
                self._request_keys.setdefault(p["request_key"], {"withdrawal_event_id": p["event_id"]})
        elif t == EV_DATASET_GENERATED:
            ds = DatasetRecord(
                dataset_id=p["dataset_id"], policy_id=p["policy_id"], purpose=p["purpose"],
                field_categories=frozenset(p["field_categories"]),
                recipients=frozenset(p["recipients"]),
                snapshot=tuple(
                    C.SnapshotEntry(
                        record_id=e["record_id"], student_id=e["student_id"],
                        category=e["category"], fields=tuple(e["fields"]),
                        decision_id=e["decision_id"],
                    ) for e in p["snapshot"]
                ),
                minimization=p["minimization"], generated_at=parse_dt(event["at"]),
            )
            self.datasets[p["dataset_id"]] = ds
        elif t == EV_ARTIFACT_DERIVED:
            art = ArtifactRecord(
                artifact_id=p["artifact_id"], kind=p["kind"], name=p["name"],
                dataset_id=p.get("dataset_id"), parent_artifact_id=p.get("parent_artifact_id"),
                student_ids=frozenset(p["student_ids"]), created_at=parse_dt(event["at"]),
                immutable=p["kind"] == C.ARTIFACT_CONCLUSION,
            )
            self.artifacts[p["artifact_id"]] = art
        elif t in (EV_ACCESS_SUSPENDED, EV_ACCESS_RESUMED):
            suspend = t == EV_ACCESS_SUSPENDED
            store = self.datasets if p["target_kind"] == C.TARGET_DATASET else self.artifacts
            target = store[p["target_id"]]
            for sid in p["student_ids"]:
                causes = target.suspension_causes.setdefault(sid, set())
                if suspend:
                    causes.add(p["cause"])
                    target.suspended_students.add(sid)
                else:
                    causes.discard(p["cause"])
                    if not causes:
                        target.suspended_students.discard(sid)
                        target.suspension_causes.pop(sid, None)
            target.suspended = bool(target.suspended_students)
        elif t == EV_TASK_OPENED:
            task = DispositionTask(
                task_id=p["task_id"], target_kind=p["target_kind"], target_id=p["target_id"],
                student_ids=frozenset(p["student_ids"]), action=p["action"], cause=p["cause"],
                status=C.TASK_OPEN, rationale=p["rationale"],
                created_at=parse_dt(event["at"]), updated_at=parse_dt(event["at"]),
                dedupe_key=p["dedupe_key"], trigger=p.get("trigger", ""),
            )
            self.tasks[p["task_id"]] = task
            self._task_dedupe.add(p["dedupe_key"])
        elif t == EV_TASK_PROGRESSED:
            task = self.tasks[p["task_id"]]
            task.status = p["status"]
            task.progress_percent = p["percent"]
            task.detail = p.get("detail", "")
            task.blocked_reason = p.get("blocked_reason", "")
            task.updated_at = parse_dt(event["at"])
            if p.get("result_dataset_id"):
                task.result_dataset_id = p["result_dataset_id"]
            if p.get("result_artifact_id"):
                task.result_artifact_id = p["result_artifact_id"]
            if p.get("certificate_id"):
                task.certificate_id = p["certificate_id"]
        elif t == EV_DESTRUCTION_CERTIFIED:
            cert = Certificate(
                certificate_id=p["certificate_id"], target_kind=p["target_kind"],
                target_id=p["target_id"], student_ids=frozenset(p["student_ids"]),
                task_id=p.get("task_id"), method=p["method"],
                certified_at=parse_dt(event["at"]), request_key=p.get("request_key"),
            )
            self.certificates[p["certificate_id"]] = cert
            target = (self.datasets.get(p["target_id"]) if p["target_kind"] == C.TARGET_DATASET
                      else self.artifacts.get(p["target_id"]))
            if target is not None:
                target.destroyed = True
                target.certificate_id = p["certificate_id"]
                if p["method"].startswith("rebuild_superseded_by:"):
                    target.rebuilt_as = p["method"].split(":", 1)[1]
            if p.get("request_key"):
                self._request_keys[p["request_key"]] = {"certificate_id": p["certificate_id"]}
        elif t == EV_ACCESS_REQUEST_SUBMITTED:
            self.requests[p["request_id"]] = AccessRequest(
                request_id=p["request_id"], requester=p["requester"],
                target_kind=p["target_kind"], target_id=p["target_id"], purpose=p["purpose"],
                status="submitted", submitted_at=parse_dt(event["at"]),
            )
        elif t == EV_ACCESS_REQUEST_DECIDED:
            req = self.requests[p["request_id"]]
            req.status = "approved" if p["approved"] else "rejected"
            req.approver = p["approver"]
            req.reason = p.get("reason", "")
            req.decided_at = parse_dt(event["at"])
        elif t == EV_DISPUTE_OPENED:
            self.disputes.append(Dispute(
                dispute_id=p["dispute_id"], student_id=p["student_id"], scope=p["scope"],
                policy_id=p.get("policy_id"), purpose=p.get("purpose"),
                opened_at=parse_dt(event["at"]),
            ))
        elif t == EV_DISPUTE_CLOSED:
            d = next(x for x in self.disputes if x.dispute_id == p["dispute_id"])
            d.closed_at = parse_dt(event["at"])
        elif t == EV_CONCLUSION_ANNOTATED:
            art = self.artifacts[p["artifact_id"]]
            art.annotations = art.annotations + (p["annotation"],)
        else:
            raise RuntimeError(f"未知事件类型: {t}")
