"""领域错误。"""


class GovernanceError(Exception):
    """治理服务领域错误基类。"""


class PolicyError(GovernanceError):
    """授权文本登记或版本错误。"""


class ConsentError(GovernanceError):
    """同意状态不满足操作要求。"""


class CoverageError(GovernanceError):
    """某次授权不覆盖该研究用途/字段/接收方。"""

    def __init__(self, message: str, *, student_id: str, purpose: str):
        super().__init__(message)
        self.student_id = student_id
        self.purpose = purpose


class DatasetError(GovernanceError):
    """数据集或产物错误。"""


class ImmutableConclusionError(GovernanceError):
    """研究结论只能追加标注，正文不得改写。"""


class SegregationOfDutiesError(GovernanceError):
    """访问申请的申请人不得审批本人的申请。"""


class DispositionError(GovernanceError):
    """处置任务状态错误。"""


class DisputeHoldError(GovernanceError):
    """争议封存期间只能保留审计，正文访问与销毁受限。"""
