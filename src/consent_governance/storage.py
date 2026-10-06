"""事件存储：仅追加的 JSONL 事件日志，服务重启后通过重放恢复全部状态。"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

# 事件类型
EV_POLICY_REGISTERED = "policy_registered"
EV_CONSENT_RECORDED = "consent_recorded"
EV_CONSENT_WITHDRAWN = "consent_withdrawn"
EV_DATASET_GENERATED = "dataset_generated"
EV_ARTIFACT_DERIVED = "artifact_derived"
EV_ACCESS_SUSPENDED = "access_suspended"
EV_ACCESS_RESUMED = "access_resumed"
EV_TASK_OPENED = "disposition_task_opened"
EV_TASK_PROGRESSED = "disposition_task_progressed"
EV_DESTRUCTION_CERTIFIED = "destruction_certified"
EV_ACCESS_REQUEST_SUBMITTED = "access_request_submitted"
EV_ACCESS_REQUEST_DECIDED = "access_request_decided"
EV_DISPUTE_OPENED = "dispute_opened"
EV_DISPUTE_CLOSED = "dispute_closed"
EV_CONCLUSION_ANNOTATED = "conclusion_annotated"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}-{uuid.uuid4().hex[:12]}"


class EventStore:
    """文件型仅追加事件日志。

    每次事件追加后 flush + fsync，保证处置进度在进程重启后仍可恢复。
    """

    def __init__(
        self,
        path: Optional[str | Path] = None,
        clock: Callable[[], datetime] = utc_now,
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        self.path = Path(path) if path else None
        self.clock = clock
        self.id_factory = id_factory
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event_type: str, payload: dict) -> dict:
        event = {"type": event_type, "at": self.clock().isoformat(), **payload}
        if self.path is None:
            return event
        # 同目录临时文件 + 单行追加，避免并发写出半截记录
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return event

    def load(self) -> Iterator[dict]:
        if self.path is None or not self.path.exists():
            return iter(())
        with self.path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(f"事件日志第 {line_no} 行损坏: {exc}") from exc

    def replay_into(self, sink: Callable[[dict], None]) -> None:
        for event in self.load():
            sink(event)

    def next_id(self, prefix: str) -> str:
        return self.id_factory(prefix)
