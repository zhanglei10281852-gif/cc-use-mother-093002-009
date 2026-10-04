"""授权文本（同意书）版本管理。

文本按 *用途* 组织，每个版本固化：字段类别、保留期限、接收机构。
已发布版本不可变；用途扩大（新增用途）必须发布新版本，旧同意不自动覆盖。
"""
from __future__ import annotations

import copy
from typing import Any

from .store import Store


class GovernanceError(ValueError):
    """领域规则冲突。"""


REQUIRED_FIELDS = ("purposes", "field_categories", "retention_days", "recipients")


def _validate_snapshot(snapshot: dict[str, Any]) -> None:
    for key in REQUIRED_FIELDS:
        if key not in snapshot:
            raise GovernanceError(f"授权文本缺少必要组成：{key}")
    purposes = snapshot["purposes"]
    fields = snapshot["field_categories"]
    recipients = snapshot["recipients"]
    if not isinstance(purposes, list) or not purposes or not all(isinstance(p, str) and p for p in purposes):
        raise GovernanceError("用途列表不能为空")
    if not isinstance(fields, list) or not fields or not all(isinstance(f, str) and f for f in fields):
        raise GovernanceError("字段类别列表不能为空")
    if not isinstance(recipients, list) or not recipients or not all(
        isinstance(r, str) and r for r in recipients
    ):
        raise GovernanceError("接收机构列表不能为空")
    retention = snapshot["retention_days"]
    if not isinstance(retention, int) or retention < 1:
        raise GovernanceError("保留期限必须为正整数（天）")
    if not isinstance(snapshot.get("title"), str) or not snapshot.get("title"):
        raise GovernanceError("授权文本标题不能为空")
    # 用途集合不允许在同一版本内重复
    if len(set(purposes)) != len(purposes):
        raise GovernanceError("同一版本内用途重复")


class TextService:
    def __init__(self, store: Store) -> None:
        self.store = store

    def register_text(self, text_id: str, title: str, *, request_id: str | None = None) -> str:
        def _do() -> str:
            if text_id in self.store.state["texts"]:
                raise GovernanceError(f"授权文本已存在：{text_id}")
            self.store.state["texts"][text_id] = {
                "title": title,
                "current_revision": 0,
                "revisions": {},
            }
            return text_id
        return self.store.mutate(request_id, _do)

    def publish(
        self,
        text_id: str,
        purposes: list[str],
        field_categories: list[str],
        retention_days: int,
        recipients: list[str],
        *,
        request_id: str | None = None,
    ) -> int:
        """发布新版本。用途/字段/接收机构只能扩大或保持；收窄需另立文本。

        返回新版本号。版本一经发布即冻结。
        """

        def _do() -> int:
            text = self.store.state["texts"].get(text_id)
            if text is None:
                raise GovernanceError(f"授权文本不存在：{text_id}")
            snapshot = {
                "title": text["title"],
                "purposes": list(purposes),
                "field_categories": list(field_categories),
                "retention_days": retention_days,
                "recipients": list(recipients),
            }
            _validate_snapshot(snapshot)
            new_rev = int(text["current_revision"]) + 1
            if text["revisions"]:
                prev = text["revisions"][str(text["current_revision"])]
                if not set(snapshot["purposes"]) >= set(prev["purposes"]):
                    raise GovernanceError("新版本不得删减既有用途；收窄用途应发布新的授权文本")
                if not set(snapshot["field_categories"]) >= set(prev["field_categories"]):
                    raise GovernanceError("新版本不得删减既有字段类别")
                if not set(snapshot["recipients"]) >= set(prev["recipients"]):
                    raise GovernanceError("新版本不得删减既有接收机构")
                core = ("title", "purposes", "field_categories", "retention_days", "recipients")
                if all(snapshot[k] == prev[k] for k in core):
                    raise GovernanceError("新版本与当前版本内容完全相同，无需发布")
            snapshot["revision"] = new_rev
            text["revisions"][str(new_rev)] = copy.deepcopy(snapshot)
            text["current_revision"] = new_rev
            return new_rev
        return self.store.mutate(request_id, _do)

    def get_snapshot(self, text_id: str, revision: int) -> dict[str, Any]:
        text = self.store.state["texts"].get(text_id)
        if text is None:
            raise GovernanceError(f"授权文本不存在：{text_id}")
        snap = text["revisions"].get(str(revision))
        if snap is None:
            raise GovernanceError(f"授权版本不存在：{text_id}@{revision}")
        return copy.deepcopy(snap)

    def current_revision(self, text_id: str) -> int:
        text = self.store.state["texts"].get(text_id)
        if text is None or int(text["current_revision"]) == 0:
            raise GovernanceError(f"授权文本尚无已发布版本：{text_id}")
        return int(text["current_revision"])

    def covers_purpose(self, text_id: str, revision: int, purpose: str) -> bool:
        """判断某已固化版本是否覆盖指定用途（版本快照判定，不看新版本）。"""
        return purpose in self.get_snapshot(text_id, revision)["purposes"]
