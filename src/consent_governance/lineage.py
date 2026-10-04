"""数据记录、产物谱系与影响分析。

- 学生原始记录登记后按 (授权文本版本) 固化内容哈希；
- 数据集版本生成时冻结：来源快照（哪些学生的哪些记录、当时各自的同意依据）、
  最小化规则、产物内容哈希；
- 派生边构成谱系图；撤回/失效时沿逆向边计算全部受影响产物；
- 研究结论为 WORM 节点：只能封存为"历史锁定"，正文永不改写。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from .contracts import GRANT_KINDS, NodeKind, NodeState, Relation
from .store import Store
from .texts import GovernanceError


def content_hash(payload: Any) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


class LineageService:
    def __init__(self, store: Store, consent: Any) -> None:
        self.store = store
        self.consent = consent

    # ---- 学生记录 -----------------------------------------------------

    def register_record(self, student_id: str, category: str, payload: dict[str, Any]) -> str:
        records = self.store.state["records"]
        record_id = f"R-{self.store.next_seq('record'):04d}"
        records[record_id] = {
            "record_id": record_id,
            "student_id": student_id,
            "category": category,
            "payload_hash": content_hash(payload),
            "payload": payload,  # 简化演示：正文保留在库中，处置时隔离/销毁
        }
        self.store.save()
        return record_id

    def records_of(self, student_id: str) -> list[str]:
        return [rid for rid, r in self.store.state["records"].items() if r["student_id"] == student_id]

    # ---- 谱系边 -------------------------------------------------------

    def _add_edge(self, src: str, dst: str, relation: Relation) -> None:
        # 同关系去重
        for e in self.store.state["edges"]:
            if e["src"] == src and e["dst"] == dst and e["relation"] == relation.value:
                return
        self.store.state["edges"].append({
            "edge_id": f"ED-{self.store.next_seq('edge'):04d}",
            "src": src,
            "dst": dst,
            "relation": relation.value,
        })

    def _downstream(self, node_ids: Iterable[str]) -> set[str]:
        """沿 derived 边求逆向闭包（谁消费了这些节点）。"""
        affected: set[str] = set()
        frontier = list(node_ids)
        while frontier:
            current = frontier.pop()
            for edge in self.store.state["edges"]:
                if edge["src"] == current and edge["relation"] == Relation.DERIVED.value:
                    if edge["dst"] not in affected:
                        affected.add(edge["dst"])
                        frontier.append(edge["dst"])
        return affected

    # ---- 数据集版本生成 ----------------------------------------------

    def build_dataset(
        self,
        display_name: str,
        record_ids: list[str],
        purpose: str,
        minimization_rules: dict[str, str],
        payload: Any,
        *,
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        """生成数据集版本：固化来源快照与最小化规则，并校验当下同意有效。"""
        nodes = self.store.state["nodes"]
        records = self.store.state["records"]

        missing = [r for r in record_ids if r not in records]
        if missing:
            raise GovernanceError(f"来源记录不存在：{missing}")
        if not record_ids:
            raise GovernanceError("数据集至少需要一条来源记录")

        sources = []
        by_student: dict[str, list[str]] = {}
        for rid in record_ids:
            rec = records[rid]
            basis = self.consent.effective_basis(rec["student_id"], purpose)
            if basis is None:
                raise GovernanceError(f"记录 {rid} 的学生对用途 {purpose!r} 无有效同意，不能纳入数据集")
            sources.append({
                "record_id": rid,
                "student_id": rec["student_id"],
                "category": rec["category"],
                "record_hash": rec["payload_hash"],
                "basis_event": basis["event"]["event_id"],
                "text_id": basis["event"]["text_id"],
                "text_revision": basis["event"]["text_revision"],
                "granted_at": basis["event"]["ts"],
                "retention_days": basis["snapshot"]["retention_days"],
            })
            by_student.setdefault(rec["student_id"], []).append(rid)

        node_id = f"D-{self.store.next_seq('node'):04d}"
        rules_hash = content_hash(minimization_rules)
        source_snapshot = {
            "purpose": purpose,
            "sources": sorted(sources, key=lambda s: s["record_id"]),
        }
        snapshot_hash = content_hash(source_snapshot)
        node = {
            "node_id": node_id,
            "kind": NodeKind.DATASET.value,
            "display_name": display_name,
            "state": NodeState.ACTIVE.value,
            "purpose": purpose,
            "created_from_records": sorted(record_ids),
            "students": sorted(by_student),
            "source_snapshot": source_snapshot,
            "source_snapshot_hash": snapshot_hash,
            "minimization_rules": dict(minimization_rules),
            "minimization_rules_hash": rules_hash,
            "content_hash": content_hash(payload),
            "payload": payload,
            "revisions_chain": [node_id],
        }
        nodes[node_id] = node
        for rid in record_ids:
            self._add_edge(rid, node_id, Relation.DERIVED)
        if supersedes is not None:
            if supersedes not in nodes:
                raise GovernanceError(f"被替代的数据集不存在：{supersedes}")
            self._add_edge(node_id, supersedes, Relation.SUPERSEDES)
            chain = list(nodes[supersedes]["revisions_chain"]) + [node_id]
            node["revisions_chain"] = chain
            nodes[supersedes]["revisions_chain"] = list(chain)
        self.store.save()
        return node

    def create_export(self, display_name: str, dataset_id: str, recipient: str, payload: Any) -> dict[str, Any]:
        nodes = self.store.state["nodes"]
        parent = nodes.get(dataset_id)
        if parent is None:
            raise GovernanceError(f"数据集不存在：{dataset_id}")
        node_id = f"X-{self.store.next_seq('node'):04d}"
        node = {
            "node_id": node_id,
            "kind": NodeKind.EXPORT.value,
            "display_name": display_name,
            "state": NodeState.ACTIVE.value,
            "purpose": parent["purpose"],
            "recipient": recipient,
            "students": list(parent["students"]),
            "content_hash": content_hash(payload),
            "payload": payload,
        }
        nodes[node_id] = node
        self._add_edge(dataset_id, node_id, Relation.DERIVED)
        self.store.save()
        return node

    def publish_conclusion(self, display_name: str, input_ids: list[str], finding: str) -> dict[str, Any]:
        """登记研究结论。结论一经发布即 WORM 锁定，正文不可改写。"""
        nodes = self.store.state["nodes"]
        for nid in input_ids:
            if nid not in nodes:
                raise GovernanceError(f"结论输入产物不存在：{nid}")
        node_id = f"C-{self.store.next_seq('node'):04d}"
        node = {
            "node_id": node_id,
            "kind": NodeKind.CONCLUSION.value,
            "display_name": display_name,
            "state": NodeState.ACTIVE.value,
            "finding": finding,                 # 冻结的历史结论文本
            "finding_hash": content_hash(finding),
            "students": sorted({s for nid in input_ids for s in nodes[nid].get("students", [])}),
        }
        nodes[node_id] = node
        for nid in input_ids:
            self._add_edge(nid, node_id, Relation.DERIVED)
        self.store.save()
        return node

    def lock_conclusion_history(self, conclusion_id: str) -> dict[str, Any]:
        node = self.store.state["nodes"].get(conclusion_id)
        if node is None or node["kind"] != NodeKind.CONCLUSION.value:
            raise GovernanceError(f"研究结论不存在：{conclusion_id}")
        node["state"] = NodeState.HISTORICAL_LOCKED.value
        self.store.save()
        return node

    # ---- 影响分析 -----------------------------------------------------

    def impact_for_records(self, record_ids: Iterable[str]) -> dict[str, list[str]]:
        """给定学生记录，返回受影响的数据集版本、导出、结论。"""
        affected = self._downstream(record_ids)
        buckets = {
            NodeKind.DATASET.value: [],
            NodeKind.EXPORT.value: [],
            NodeKind.CONCLUSION.value: [],
        }
        for nid in affected:
            node = self.store.state["nodes"].get(nid)
            if node:
                buckets[node["kind"]].append(nid)
        for key in buckets:
            buckets[key].sort()
        return buckets

    def impact_for_student(self, student_id: str) -> dict[str, Any]:
        record_ids = self.records_of(student_id)
        impact = self.impact_for_records(record_ids)
        return {"student_id": student_id, "record_ids": record_ids, "impact": impact}

    def node_consent_is_current(self, node: dict[str, Any]) -> dict[str, Any]:
        """复核数据集版本：来源学生当下是否仍对用途持有 *同版本* 有效同意。

        返回 {record_id: status}，status ∈ current|withdrawn|refused|expired|version_mismatch。
        重新同意若指向新版本，旧数据集版本的依据即 version_mismatch——需要重建。
        """
        if node["kind"] != NodeKind.DATASET.value:
            raise GovernanceError("仅数据集版本可做同意复核")
        report: dict[str, str] = {}
        records = self.store.state["records"]
        for src in node["source_snapshot"]["sources"]:
            rid = src["record_id"]
            student_id = records[rid]["student_id"]
            basis = self.consent.effective_basis(student_id, node["purpose"])
            latest = self.consent._latest(student_id, node["purpose"])
            if basis is None:
                if latest is None:
                    report[rid] = "refused"
                elif latest["kind"] == "withdraw":
                    report[rid] = "withdrawn"
                else:
                    report[rid] = "expired"
            elif basis["event"]["text_revision"] != src["text_revision"]:
                report[rid] = "version_mismatch"
            else:
                report[rid] = "current"
        return report
