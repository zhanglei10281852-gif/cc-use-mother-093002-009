"""测试夹具：可控时钟 + 标准两校场景。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from consent_governance.service import GovernanceService  # noqa: E402


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, days: float = 0.0, seconds: float = 0.0) -> None:
        self.t += days * 86400 + seconds


def make_service() -> tuple[GovernanceService, FakeClock, str]:
    clock = FakeClock()
    directory = tempfile.mkdtemp(prefix="cg-test-")
    return GovernanceService(directory, clock=clock), clock, directory


PURPOSE = "中文学习过程分析"
PURPOSE_NEW = "自适应辅导AI研究"
TEXT = "T-CROSS"
FIELDS = ["learning_behavior", "academic_result"]
RECIPIENTS = ["广西师大", "东盟文理学院"]


def build_basic_scenario(svc: GovernanceService, *, retention: int = 365):
    """两学生同意 → 各登记记录 → 数据集 → 导出 → 结论。返回各 ID。"""
    svc.texts.register_text(TEXT, "跨校中文课程学习数据授权书")
    rev = svc.texts.publish(TEXT, [PURPOSE], FIELDS, retention, RECIPIENTS)

    ids: dict[str, str] = {}
    ids["rev"] = rev
    svc.consent.agree("S-001", PURPOSE, TEXT, rev)
    svc.consent.agree("S-002", PURPOSE, TEXT, rev)

    ids["r1"] = svc.lineage.register_record(
        "S-001", "learning_behavior", {"student_id": "S-001", "lessons": 12}
    )
    ids["r2"] = svc.lineage.register_record(
        "S-002", "academic_result", {"student_id": "S-002", "score": 88}
    )
    rules = {"learning_behavior": "脱敏学号后聚合", "academic_result": "仅保留区间"}
    payload = {"rows": [
        {"student_id": "S-001", "v": 12},
        {"student_id": "S-002", "v": 88},
    ]}
    ds = svc.lineage.build_dataset("期中行为数据集v1", [ids["r1"], ids["r2"]], PURPOSE, rules, payload)
    ids["ds"] = ds["node_id"]

    ex = svc.lineage.create_export("给东盟文理学院的导出", ids["ds"], "东盟文理学院", {"file": "x.csv"})
    ids["ex"] = ex["node_id"]

    cc = svc.lineage.publish_conclusion("期中学习投入度结论", [ids["ds"]], "学习行为与成绩正相关")
    ids["cc"] = cc["node_id"]
    return ids
