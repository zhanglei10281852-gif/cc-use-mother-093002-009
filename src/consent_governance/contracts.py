"""数据授权与数据集来源的基础契约。"""
from dataclasses import dataclass

# 授权决定
DECISION_GRANTED = "granted"
DECISION_DENIED = "denied"

# 撤回范围
SCOPE_ALL = "all"
SCOPE_POLICY = "policy"
SCOPE_PURPOSE = "purpose"
SCOPE_KINDS = (SCOPE_ALL, SCOPE_POLICY, SCOPE_PURPOSE)

# 处置对象
TARGET_DATASET = "dataset"
TARGET_ARTIFACT = "artifact"
TARGET_KINDS = (TARGET_DATASET, TARGET_ARTIFACT)

# 下游产物类型
ARTIFACT_EXPORT = "export"       # 导出文件
ARTIFACT_REPORT = "report"       # 分析报告
ARTIFACT_CONCLUSION = "conclusion"  # 研究结论（不可改写）
ARTIFACT_KINDS = (ARTIFACT_EXPORT, ARTIFACT_REPORT, ARTIFACT_CONCLUSION)

# 处置动作
ACTION_REBUILD = "rebuild"
ACTION_DESTROY = "destroy"

# 处置任务状态
TASK_OPEN = "open"
TASK_IN_PROGRESS = "in_progress"
TASK_BLOCKED = "blocked"
TASK_COMPLETED = "completed"
TASK_CANCELLED = "cancelled"

# 暂停访问的起因
CAUSE_WITHDRAWAL = "withdrawal"
CAUSE_RETENTION = "retention"
CAUSE_DENIAL = "denial"


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


@dataclass(frozen=True)
class SnapshotEntry:
    """数据集生成时固化的单条来源记录。"""
    record_id: str
    student_id: str
    category: str
    fields: tuple
    decision_id: str  # 授权该记录进入数据集的具体同意事件

    def __post_init__(self) -> None:
        if not self.record_id or not self.student_id or not self.category:
            raise ValueError("快照来源记录信息不完整")
        if not self.decision_id:
            raise ValueError("快照记录必须固化授权依据")
