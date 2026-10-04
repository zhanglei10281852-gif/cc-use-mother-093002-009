"""授权账本：同意 / 拒绝 / 撤回 / 重新同意。

事件为只追加记录；每条同意钉住具体的授权文本版本快照，
因此用途是否被覆盖按该快照判定——之后发布的新版本不追溯扩大旧同意。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .contracts import EventKind
from .store import Store
from .texts import GovernanceError, TextService

# 自然去抖窗口：不带幂等键的重复意向（如重复撤回）在此窗口内合并
DEBOUNCE_SECONDS = 5.0


class ConsentService:
    def __init__(self, store: Store, texts: TextService, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.texts = texts
        self.clock = clock

    # ---- 内部查询 -----------------------------------------------------

    def _events_for(self, student_id: str, purpose: str) -> list[dict[str, Any]]:
        return [
            e for e in self.store.state["consent_events"]
            if e["student_id"] == student_id and e["purpose"] == purpose
        ]

    def _latest(self, student_id: str, purpose: str) -> dict[str, Any] | None:
        events = self._events_for(student_id, purpose)
        return events[-1] if events else None

    def _check_grant_target(self, student_id: str, purpose: str, text_id: str, revision: int) -> dict[str, Any]:
        snap = self.texts.get_snapshot(text_id, revision)
        if purpose not in snap["purposes"]:
            raise GovernanceError(
                f"授权版本 {text_id}@{revision} 不覆盖用途 {purpose!r}；扩大用途须取得新版本下的同意"
            )
        return snap

    def _append(
        self, kind: EventKind, student_id: str, purpose: str, text_id: str, revision: int
    ) -> dict[str, Any]:
        events = self.store.state["consent_events"]
        event_id = f"EV-{self.store.next_seq('event'):04d}"
        event = {
            "event_id": event_id,
            "kind": kind.value,
            "student_id": student_id,
            "purpose": purpose,
            "text_id": text_id,
            "text_revision": revision,
            "seq": len(events) + 1,
            "ts": self.clock(),
        }
        events.append(event)
        return event

    def _record_decision(
        self,
        kind: EventKind,
        student_id: str,
        purpose: str,
        text_id: str,
        revision: int,
        *,
        allow_replay: bool,
        request_id: str | None,
    ) -> dict[str, Any]:
        def _do() -> dict[str, Any]:
            latest = self._latest(student_id, purpose)
            if latest is not None and latest["kind"] == kind.value:
                # 自然去抖：重复撤回/拒绝/同意只处理一次
                if allow_replay:
                    return {**latest, "replayed": True}
                raise GovernanceError(f"当前状态已是 {kind.value}，无需重复登记")
            if kind in (EventKind.AGREE, EventKind.REAGREE):
                self._check_grant_target(student_id, purpose, text_id, revision)
            else:
                # 撤回/拒绝也必须指向真实版本；但旧版本存在即可
                self.texts.get_snapshot(text_id, revision)

            if kind is EventKind.AGREE:
                if latest is not None and latest["kind"] in (EventKind.AGREE.value, EventKind.REAGREE.value):
                    raise GovernanceError("已在同意有效期内；用途扩大请针对新版本重新同意")
                if latest is not None and latest["kind"] == EventKind.WITHDRAW.value:
                    raise GovernanceError("撤回之后的同意必须登记为重新同意(reagree)")
            elif kind is EventKind.REAGREE:
                if latest is None or latest["kind"] != EventKind.WITHDRAW.value:
                    raise GovernanceError("重新同意仅能发生在撤回之后")
            elif kind is EventKind.WITHDRAW:
                if latest is None or latest["kind"] not in (
                    EventKind.AGREE.value, EventKind.REAGREE.value
                ):
                    raise GovernanceError("没有有效的同意可供撤回")
            elif kind is EventKind.REFUSE:
                if latest is not None and latest["kind"] in (
                    EventKind.AGREE.value, EventKind.REAGREE.value
                ):
                    raise GovernanceError("已作出的同意应通过撤回终止，而非拒绝")
            return self._append(kind, student_id, purpose, text_id, revision)
        return self.store.mutate(request_id, _do)

    # ---- 公共命令 -----------------------------------------------------

    def agree(self, student_id: str, purpose: str, text_id: str, revision: int, *, request_id: str | None = None):
        return self._record_decision(
            EventKind.AGREE, student_id, purpose, text_id, revision,
            allow_replay=True, request_id=request_id,
        )

    def reagree(self, student_id: str, purpose: str, text_id: str, revision: int, *, request_id: str | None = None):
        return self._record_decision(
            EventKind.REAGREE, student_id, purpose, text_id, revision,
            allow_replay=False, request_id=request_id,
        )

    def refuse(self, student_id: str, purpose: str, text_id: str, revision: int, *, request_id: str | None = None):
        return self._record_decision(
            EventKind.REFUSE, student_id, purpose, text_id, revision,
            allow_replay=True, request_id=request_id,
        )

    def withdraw(self, student_id: str, purpose: str, text_id: str, revision: int, *, request_id: str | None = None):
        return self._record_decision(
            EventKind.WITHDRAW, student_id, purpose, text_id, revision,
            allow_replay=True, request_id=request_id,
        )

    # ---- 效力查询 -----------------------------------------------------

    def effective_basis(self, student_id: str, purpose: str, *, at: float | None = None) -> dict[str, Any] | None:
        """返回某时点（默认当前）有效的同意依据，无效/过期/撤回则 None。"""
        moment = self.clock() if at is None else at
        latest = self._latest(student_id, purpose)
        if latest is None or latest["kind"] not in (EventKind.AGREE.value, EventKind.REAGREE.value):
            return None
        snap = self.texts.get_snapshot(latest["text_id"], latest["text_revision"])
        if moment >= latest["ts"] + snap["retention_days"] * 86400:
            return None
        return {"event": latest, "snapshot": snap}

    def student_purposes(self, student_id: str) -> list[str]:
        return sorted({e["purpose"] for e in self.store.state["consent_events"] if e["student_id"] == student_id})

    def events(self, student_id: str | None = None) -> list[dict[str, Any]]:
        if student_id is None:
            return list(self.store.state["consent_events"])
        return [e for e in self.store.state["consent_events"] if e["student_id"] == student_id]
