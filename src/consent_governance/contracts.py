"""数据授权与数据集来源的基础契约。

保留领域最早期的两个稳定值对象，并补充全服务共用的枚举。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


@dataclass(frozen=True)
class ConsentVersion:
    entity_id: str
    display_name: str
    revision: int

    def __post_init__(self) -> None:
        if not self.entity_id or not self.display_name or self.revision < 1:
            raise ValueError("版本化实体信息不合法")


@dataclass(frozen=True)
class DatasetSource:
    record_id: str
    entity_id: str
    category: str

    def __post_init__(self) -> None:
        if not self.record_id or not self.entity_id or not self.category:
            raise ValueError("关联记录信息不完整")


class EventKind(str, enum.Enum):
    """授权账本事件类型。"""

    AGREE = "agree"            # 同意
    REFUSE = "refuse"          # 拒绝
    WITHDRAW = "withdraw"      # 撤回
    REAGREE = "reagree"        # 撤回后重新同意

    @property
    def is_grant(self) -> bool:
        return self in (EventKind.AGREE, EventKind.REAGREE)


class FieldTag(str, enum.Enum):
    """字段敏感类别标签，驱动最小化规则。"""

    DIRECT = "direct"          # 直接标识
    QUASI = "quasi"            # 准标识
    BEHAVIOR = "behavior"      # 学习行为
    ACADEMIC = "academic"      # 学业成绩
    FREE_TEXT = "free_text"    # 自由文本


class NodeKind(str, enum.Enum):
    DATASET = "dataset"        # 数据集版本（可重建/销毁）
    EXPORT = "export"          # 导出文件（可销毁）
    CONCLUSION = "conclusion"  # 研究结论（历史封存，禁止改写）


class NodeState(str, enum.Enum):
    ACTIVE = "active"
    PAUSED = "paused"                  # 已暂停访问
    QUARANTINED = "quarantined"        # 争议保全：限制正文，保留审计
    DESTROYED = "destroyed"            # 正文已销毁，仅有回执与谱系
    HISTORICAL_LOCKED = "historical_locked"  # 历史结论封存


class StepState(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    SKIPPED = "skipped"


class JobState(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"  # 因争议保全阻塞，争议解除后可续跑


class RequestState(str, enum.Enum):
    OPEN = "open"
    GRANTED = "granted"
    DENIED = "denied"


class Relation(str, enum.Enum):
    DERIVED = "derived"      # 下游派生（影响传播沿此边）
    SUPERSEDES = "supersedes"  # 新版本替代旧版本


GRANT_KINDS = (EventKind.AGREE.value, EventKind.REAGREE.value)
DESTRUCTIBLE_KINDS = (NodeKind.DATASET.value, NodeKind.EXPORT.value)
