"""JSON 文件持久化层：原子写入、命令幂等索引、重启恢复。

存储为一个目录：
  state.json   全部领域状态（单文件，原子替换）
幂等键只在命令成功提交后记录；崩溃在写入前发生则命令视为未执行。
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, TypeVar

STATE_FILE = "state.json"

T = TypeVar("T")


def default_state() -> dict[str, Any]:
    return {
        "schema": 1,
        "texts": {},          # text_id -> {display, current_revision, revisions: {rev: snapshot}}
        "consent_events": [],  # 追加式授权事件
        "records": {},         # record_id -> {student_id, category, hashes:{text_rev: hash}}
        "nodes": {},           # node_id -> 产物节点
        "edges": [],           # {edge_id, src, dst, relation, created_at}
        "jobs": {},            # job_id -> 处置任务
        "receipts": [],        # 销毁回执
        "access_requests": {},  # request_id -> 访问申请
        "holds": {},           # hold_id -> 争议保全 {node_ids, reason, active, ...}
        "commands": {},        # request_id(幂等键) -> 命令提交结果
        "counters": {},        # 各类自增序号
    }


class Store:
    """加载/保存整个状态。所有服务方法通过 mutate() 获得可变 dict。"""

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self.directory = Path(directory)
        self.path = self.directory / STATE_FILE
        self.state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if self.path.exists():
            with self.path.open("r", encoding="utf-8") as fh:
                state = json.load(fh)
            # 轻量迁移：补齐新字段
            for key, value in default_state().items():
                state.setdefault(key, value)
            return state
        self.directory.mkdir(parents=True, exist_ok=True)
        state = default_state()
        self._persist(state)
        return state

    def _persist(self, state: dict[str, Any]) -> None:
        fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=str(self.directory))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def save(self) -> None:
        self._persist(self.state)

    def next_seq(self, kind: str) -> int:
        n = int(self.state["counters"].get(kind, 0)) + 1
        self.state["counters"][kind] = n
        return n

    # ---- 命令幂等 -----------------------------------------------------

    def idempotent(self, request_id: str | None, build: Callable[[], T]) -> T:
        """以 request_id 去重：同一键只执行一次 build，之后回放原结果。"""
        if request_id is None:
            return build()
        seen = self.state["commands"].get(request_id)
        if seen is not None:
            return seen["result"]  # type: ignore[no-any-return]
        result = build()
        self.state["commands"][request_id] = {"result": result}
        return result

    def mutate(self, request_id: str | None, build: Callable[[], T]) -> T:
        result = self.idempotent(request_id, build)
        self.save()
        return result
