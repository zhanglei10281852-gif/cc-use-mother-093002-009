"""状态模型：事件重放后得到的当前世界状态（全部可变，仅由事件驱动）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class PolicyRevision:
    policy_id: str
    display_name: str
    revision: int
    purposes: frozenset[str]
    field_categories: frozenset[str]
    retention_days: int
    recipients: frozenset[str]
    registered_at: datetime
    note: str = ""


@dataclass
class ConsentRecord:
    """一次同意/拒绝决定；其覆盖范围以事件中固化的快照为准。"""
    event_id: str
    student_id: str
    policy_id: str
    policy_revision: int
    decision: str
    purposes: frozenset[str]
    field_categories: frozenset[str]
    recipients: frozenset[str]
    retention_days: int
    recorded_at: datetime
    seq: int = 0

    def retention_deadline(self) -> datetime:
        from datetime import timedelta

        return self.recorded_at + timedelta(days=self.retention_days)


@dataclass
class WithdrawalRecord:
    event_id: str
    student_id: str
    scope: str
    policy_id: Optional[str]
    purpose: Optional[str]
    recorded_at: datetime
    request_key: Optional[str] = None
    seq: int = 0


@dataclass
class DatasetRecord:
    dataset_id: str
    policy_id: str
    purpose: str
    field_categories: frozenset[str]
    recipients: frozenset[str]
    snapshot: tuple  # SnapshotEntry
    minimization: dict
    generated_at: datetime
    suspended: bool = False
    suspended_students: set[str] = field(default_factory=set)
    suspension_causes: dict[str, set] = field(default_factory=dict)  # student -> {cause}
    rebuilt_as: Optional[str] = None
    destroyed: bool = False
    certificate_id: Optional[str] = None

    def student_ids(self) -> set[str]:
        return {entry.student_id for entry in self.snapshot}


@dataclass
class ArtifactRecord:
    artifact_id: str
    kind: str
    name: str
    dataset_id: Optional[str]
    parent_artifact_id: Optional[str]
    student_ids: frozenset[str]
    created_at: datetime
    suspended: bool = False
    suspended_students: set[str] = field(default_factory=set)
    suspension_causes: dict[str, set] = field(default_factory=dict)  # student -> {cause}
    destroyed: bool = False
    certificate_id: Optional[str] = None
    rebuilt_as: Optional[str] = None
    immutable: bool = False  # 研究结论：正文永不改写
    annotations: tuple = ()


@dataclass
class DispositionTask:
    task_id: str
    target_kind: str
    target_id: str
    student_ids: frozenset[str]
    action: str
    cause: str
    status: str
    rationale: str
    created_at: datetime
    updated_at: datetime
    progress_percent: int = 0
    detail: str = ""
    blocked_reason: str = ""
    result_dataset_id: Optional[str] = None
    result_artifact_id: Optional[str] = None
    certificate_id: Optional[str] = None
    dedupe_key: str = ""
    trigger: str = ""


@dataclass
class Certificate:
    certificate_id: str
    target_kind: str
    target_id: str
    student_ids: frozenset[str]
    task_id: str
    method: str
    certified_at: datetime
    request_key: Optional[str] = None


@dataclass
class AccessRequest:
    request_id: str
    requester: str
    target_kind: str
    target_id: str
    purpose: str
    status: str  # submitted / approved / rejected
    submitted_at: datetime
    approver: Optional[str] = None
    decided_at: Optional[datetime] = None
    reason: str = ""


@dataclass
class Dispute:
    dispute_id: str
    student_id: str
    scope: str
    policy_id: Optional[str]
    purpose: Optional[str]
    opened_at: datetime
    closed_at: Optional[datetime] = None

    @property
    def active(self) -> bool:
        return self.closed_at is None
