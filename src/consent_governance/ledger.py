"""学生授权账本：面向数据保护负责人的单人全景导出。

回答四件事：
1. 记录为何曾可用——每条同意钉住的文本版本快照、字段类别、保留期限、接收机构；
2. 流向哪些产物——数据集版本、导出文件、研究结论及当前状态；
3. 每次撤回如何传播——触发的处置任务、受影响产物、暂停/重建/销毁/封存结果；
4. 未完成处置的进度——分步任务在（可能重启后的）当前进度。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .contracts import EventKind, NodeKind, NodeState
from .store import Store

NODE_KIND_LABEL = {
    NodeKind.DATASET.value: "数据集版本",
    NodeKind.EXPORT.value: "导出文件",
    NodeKind.CONCLUSION.value: "研究结论",
}


class LedgerService:
    def __init__(self, store: Store, consent: Any, lineage: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.consent = consent
        self.lineage = lineage
        self.clock = clock

    def _flow_nodes(self, student_id: str) -> dict[str, list[dict[str, Any]]]:
        result = self.lineage.impact_for_student(student_id)
        nodes = self.store.state["nodes"]
        # 对外账本使用复数键（展示友好）；内部枚举单数键在此映射
        plural = {
            NodeKind.DATASET.value: "datasets",
            NodeKind.EXPORT.value: "exports",
            NodeKind.CONCLUSION.value: "conclusions",
        }
        flows: dict[str, list[dict[str, Any]]] = {
            "datasets": [], "exports": [], "conclusions": [],
        }
        for kind, ids in result["impact"].items():
            for nid in ids:
                node = nodes[nid]
                entry = {
                    "node_id": nid,
                    "kind": kind,
                    "display_name": node["display_name"],
                    "state": node["state"],
                    "purpose": node.get("purpose"),
                }
                if kind == NodeKind.DATASET.value:
                    my_sources = [
                        s for s in node["source_snapshot"]["sources"] if s["student_id"] == student_id
                    ]
                    entry["my_basis"] = [
                        {
                            "record_id": s["record_id"],
                            "consent_event": s["basis_event"],
                            "text_version": f"{s['text_id']}@{s['text_revision']}",
                        }
                        for s in my_sources
                    ]
                    entry["source_snapshot_hash"] = node["source_snapshot_hash"]
                    entry["minimization_rules_hash"] = node["minimization_rules_hash"]
                    entry["superseded_by_chain"] = [
                        n for n in node["revisions_chain"] if n != nid
                    ]
                elif kind == NodeKind.EXPORT.value:
                    entry["recipient"] = node.get("recipient")
                flows[plural[kind]].append(entry)
        return flows

    def _jobs_for(self, student_id: str) -> list[dict[str, Any]]:
        return [j for j in self.store.state["jobs"].values() if j["student_id"] == student_id]

    def _job_progress(self, job: dict[str, Any]) -> dict[str, Any]:
        steps = job["steps"]
        done = sum(1 for s in steps if s["state"] == "done")
        return {
            "job_id": job["job_id"],
            "purpose": job["purpose"],
            "trigger": job["trigger"],
            "trigger_event_id": job.get("trigger_event_id"),
            "state": job["state"],
            "progress": f"{done}/{len(steps)}",
            "steps": [{"name": s["name"], "state": s["state"], "detail": s["detail"]} for s in steps],
        }

    def build(self, student_id: str) -> dict[str, Any]:
        now = self.clock()
        # 1) 授权事件与可用依据
        consent_rows: list[dict[str, Any]] = []
        for ev in self.consent.events(student_id):
            row: dict[str, Any] = {
                "event_id": ev["event_id"],
                "kind": ev["kind"],
                "purpose": ev["purpose"],
                "text_version": f"{ev['text_id']}@{ev['text_revision']}",
                "ts": ev["ts"],
            }
            if ev["kind"] in (EventKind.AGREE.value, EventKind.REAGREE.value):
                snap = self.consent.texts.get_snapshot(ev["text_id"], ev["text_revision"])
                row["why_available"] = {
                    "title": snap["title"],
                    "field_categories": snap["field_categories"],
                    "retention_days": snap["retention_days"],
                    "recipients": snap["recipients"],
                    "purposes_in_version": snap["purposes"],
                    "expires_at": ev["ts"] + snap["retention_days"] * 86400,
                }
                basis = self.consent.effective_basis(student_id, ev["purpose"], at=now)
                row["effective_now"] = bool(
                    basis and basis["event"]["event_id"] == ev["event_id"]
                )
            else:
                row["effective_now"] = False
            consent_rows.append(row)

        # 2) 产物流向
        flows = self._flow_nodes(student_id)

        # 3) 每次撤回的传播
        jobs = self._jobs_for(student_id)
        receipts = self.store.state["receipts"]
        withdrawals: list[dict[str, Any]] = []
        for ev in self.consent.events(student_id):
            if ev["kind"] != EventKind.WITHDRAW.value:
                continue
            linked = next(
                (j for j in jobs if j.get("trigger_event_id") == ev["event_id"]), None
            )
            item: dict[str, Any] = {
                "event_id": ev["event_id"],
                "purpose": ev["purpose"],
                "ts": ev["ts"],
                "propagation": "未开启处置任务",
            }
            if linked is not None:
                prog = self._job_progress(linked)
                item["propagation"] = prog["state"]
                item["job"] = prog
                affected = linked.get("affected", {})
                item["affected"] = {
                    "datasets": affected.get(NodeKind.DATASET.value, []),
                    "exports": affected.get(NodeKind.EXPORT.value, []),
                    "conclusions": affected.get(NodeKind.CONCLUSION.value, []),
                }
                affected_ids = set(
                    affected.get(NodeKind.DATASET.value, []) + affected.get(NodeKind.EXPORT.value, [])
                )
                item["destruction_receipts"] = [
                    {
                        "receipt_id": r["receipt_id"],
                        "node_id": r["node_id"],
                        "content_hash": r["content_hash"],
                        "destroyed_at": r["destroyed_at"],
                    }
                    for r in receipts if r["node_id"] in affected_ids
                ]
            withdrawals.append(item)

        # 4) 未完成处置
        unfinished = [
            self._job_progress(j) for j in jobs
            if j["state"] in ("pending", "in_progress", "blocked")
        ]

        return {
            "student_id": student_id,
            "generated_at": now,
            "consent_ledger": consent_rows,
            "records": self.lineage.records_of(student_id),
            "flows": flows,
            "withdrawals": withdrawals,
            "unfinished_dispositions": unfinished,
        }
