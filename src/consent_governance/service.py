"""授权治理服务门面：装配各子服务，提供端到端操作。"""
from __future__ import annotations

import time
from typing import Any, Callable

from .consent import ConsentService
from .disposition import AccessService, DispositionService, HoldService
from .ledger import LedgerService
from .lineage import LineageService
from .store import Store
from .texts import TextService


class GovernanceService:
    def __init__(self, directory: str, *, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self.store = Store(directory)
        self.texts = TextService(self.store)
        self.consent = ConsentService(self.store, self.texts, clock=clock)
        self.lineage = LineageService(self.store, self.consent)
        self.holds = HoldService(self.store, clock=clock)
        self.disposition = DispositionService(self.store, self.lineage, self.holds, clock=clock)
        self.access = AccessService(self.store, self.lineage, clock=clock)
        self.ledger = LedgerService(self.store, self.consent, self.lineage, clock=clock)

    # ---- 授权失效的一体化入口 ----------------------------------------

    def withdraw(
        self, student_id: str, purpose: str, text_id: str, revision: int, *, request_id: str | None = None
    ) -> dict[str, Any]:
        """登记撤回并立即开启（或复用）处置任务，返回事件与任务编号。"""
        event = self.consent.withdraw(
            student_id, purpose, text_id, revision, request_id=request_id
        )
        # 已撤回后重放（重复撤回只处理一次）不会产生新事件
        if event.get("replayed"):
            job_id = self.disposition._find_open_job(student_id, purpose)
        else:
            job_id = self.disposition.open_job(
                student_id, purpose, trigger="withdraw", trigger_event_id=event["event_id"]
            )
        return {"event": event, "job_id": job_id}

    def scan_expired(self) -> list[str]:
        """保留期限到期扫描：为失效同意开启处置任务（已有未完成任务的不重复开启）。"""
        opened: list[str] = []
        seen: set[tuple[str, str]] = set()
        now = self.clock()
        for ev in self.consent.events():
            if ev["kind"] not in ("agree", "reagree"):
                continue
            key = (ev["student_id"], ev["purpose"])
            if key in seen:
                continue
            latest = self.consent._latest(ev["student_id"], ev["purpose"])
            if latest is None or latest["event_id"] != ev["event_id"]:
                continue
            snap = self.consent.texts.get_snapshot(ev["text_id"], ev["text_revision"])
            expired = now >= ev["ts"] + snap["retention_days"] * 86400
            if not expired or self.consent.effective_basis(ev["student_id"], ev["purpose"]) is not None:
                continue
            seen.add(key)
            if self.disposition._find_open_job(ev["student_id"], ev["purpose"]) is not None:
                continue
            jid = self.disposition.open_job(
                ev["student_id"], ev["purpose"], trigger="retention_expired",
                trigger_event_id=ev["event_id"],
            )
            opened.append(jid)
        return opened

    def run_pending_dispositions(self) -> list[str]:
        return self.disposition.run_all_pending()

    def resume_after_holds(self) -> list[str]:
        return self.disposition.resume_blocked()

    def student_ledger(self, student_id: str) -> dict[str, Any]:
        return self.ledger.build(student_id)
