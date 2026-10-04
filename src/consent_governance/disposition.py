"""处置任务、争议保全、销毁回执与访问申请审批。

处置以分步任务持久化，任一步在进程重启后可从断点续跑；
每步 DONE 后不重复执行（销毁回执对同一产物只出具一次）。
争议保全期间：正文隔离（quarantined）、销毁阻塞，但审计材料完整保留。
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .contracts import JobState, NodeKind, NodeState, Relation, StepState
from .store import Store
from .texts import GovernanceError

STEWARD = "steward"   # 数据管家
DPO = "dpo"           # 数据保护负责人
APPROVAL_CHAIN = (STEWARD, DPO)


class AccessService:
    """访问申请：职责分离（申请人不可审批；管家与 DPO 两级、不同人）。"""

    def __init__(self, store: Store, lineage: Any, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.lineage = lineage
        self.clock = clock

    def request_access(self, request_id: str, applicant: str, node_id: str, reason: str) -> dict[str, Any]:
        if request_id in self.store.state["access_requests"]:
            raise GovernanceError("访问申请编号重复")
        node = self.store.state["nodes"].get(node_id)
        if node is None:
            raise GovernanceError(f"产物不存在：{node_id}")
        req = {
            "request_id": request_id,
            "applicant": applicant,
            "node_id": node_id,
            "reason": reason,
            "state": "open",
            "approvals": [],
            "created_at": self.clock(),
        }
        self.store.state["access_requests"][request_id] = req
        self.store.save()
        return req

    def approve(self, request_id: str, actor: str, role: str) -> dict[str, Any]:
        req = self.store.state["access_requests"].get(request_id)
        if req is None:
            raise GovernanceError(f"访问申请不存在：{request_id}")
        if req["state"] != "open":
            raise GovernanceError(f"申请已终结：{req['state']}")
        if actor == req["applicant"]:
            raise GovernanceError("职责分离：申请人不得审批自己的访问申请")
        if role not in APPROVAL_CHAIN:
            raise GovernanceError(f"未知审批角色：{role}")
        expected = APPROVAL_CHAIN[len(req["approvals"])]
        if role != expected:
            raise GovernanceError(f"当前需要 {expected} 审批，收到的是 {role}")
        if any(a["actor"] == actor for a in req["approvals"]):
            raise GovernanceError("同一审批人不得重复或跨级审批")
        req["approvals"].append({"actor": actor, "role": role, "ts": self.clock()})
        if len(req["approvals"]) == len(APPROVAL_CHAIN):
            node = self.store.state["nodes"][req["node_id"]]
            if node["state"] in (NodeState.PAUSED.value, NodeState.QUARANTINED.value, NodeState.DESTROYED.value):
                req["state"] = "denied"
                self.store.save()
                raise GovernanceError(f"产物当前状态 {node['state']}，审批链完成但不予放行")
            req["state"] = "granted"
        self.store.save()
        return req

    def deny(self, request_id: str, actor: str, role: str) -> dict[str, Any]:
        req = self.store.state["access_requests"].get(request_id)
        if req is None:
            raise GovernanceError(f"访问申请不存在：{request_id}")
        if actor == req["applicant"]:
            raise GovernanceError("职责分离：申请人不得审批自己的访问申请")
        if role not in APPROVAL_CHAIN:
            raise GovernanceError(f"未知审批角色：{role}")
        if req["state"] != "open":
            raise GovernanceError("申请已终结")
        req["state"] = "denied"
        req["approvals"].append({"actor": actor, "role": role, "ts": self.clock(), "decision": "deny"})
        self.store.save()
        return req


class HoldService:
    """争议保全：限制正文数据、保留必要审计。"""

    def __init__(self, store: Store, *, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock

    def impose(self, case_id: str, node_ids: list[str], reason: str) -> dict[str, Any]:
        if case_id in self.store.state["holds"]:
            raise GovernanceError(f"争议案件已登记：{case_id}")
        nodes = self.store.state["nodes"]
        for nid in node_ids:
            if nid not in nodes:
                raise GovernanceError(f"产物不存在：{nid}")
        hold = {
            "case_id": case_id,
            "node_ids": sorted(node_ids),
            "reason": reason,
            "active": True,
            "imposed_at": self.clock(),
            "released_at": None,
        }
        self.store.state["holds"][case_id] = hold
        for nid in node_ids:
            node = nodes[nid]
            if node["kind"] in (NodeKind.DATASET.value, NodeKind.EXPORT.value):
                node["state"] = NodeState.QUARANTINED.value
            node.setdefault("holds", []).append(case_id)
        self.store.save()
        return hold

    def release(self, case_id: str) -> dict[str, Any]:
        hold = self.store.state["holds"].get(case_id)
        if hold is None:
            raise GovernanceError(f"争议案件不存在：{case_id}")
        if not hold["active"]:
            return hold
        hold["active"] = False
        hold["released_at"] = self.clock()
        nodes = self.store.state["nodes"]
        for nid in hold["node_ids"]:
            node = nodes.get(nid)
            if node is None:
                continue
            node["holds"] = [c for c in node.get("holds", []) if c != case_id]
            if node.get("state") == NodeState.QUARANTINED.value and not node["holds"]:
                # 回到暂停态，等待处置任务继续决定其命运
                node["state"] = NodeState.PAUSED.value
        self.store.save()
        return hold

    def is_held(self, node_id: str) -> bool:
        return any(h["active"] and node_id in h["node_ids"] for h in self.store.state["holds"].values())

    def active_case(self, node_id: str) -> str | None:
        for case_id, hold in self.store.state["holds"].items():
            if hold["active"] and node_id in hold["node_ids"]:
                return case_id
        return None


class DispositionService:
    """授权失效后的处置编排：影响分析 → 暂停 → 重建 → 销毁 → 结论封存。"""

    def __init__(
        self,
        store: Store,
        lineage: Any,
        holds: HoldService,
        *,
        clock: Callable[[], float] = time.time,
        rebuild_transformer: Callable[[dict[str, Any], set[str]], Any] | None = None,
    ) -> None:
        self.store = store
        self.lineage = lineage
        self.holds = holds
        self.clock = clock
        self.rebuild_transformer = rebuild_transformer or self._default_rebuild

    @staticmethod
    def _default_rebuild(node: dict[str, Any], excluded_students: set[str]) -> Any:
        payload = node.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("rows"), list):
            kept = [r for r in payload["rows"] if r.get("student_id") not in excluded_students]
            return {**payload, "rows": kept}
        return {
            "rebuilt_from": node["node_id"],
            "based_on_content_hash": node["content_hash"],
            "excluded_students": sorted(excluded_students),
        }

    def _find_open_job(self, student_id: str, purpose: str) -> str | None:
        for job in self.store.state["jobs"].values():
            if (
                job["student_id"] == student_id
                and job["purpose"] == purpose
                and job["state"] in (JobState.PENDING.value, JobState.IN_PROGRESS.value, JobState.BLOCKED.value)
            ):
                return job["job_id"]
        return None

    def open_job(
        self,
        student_id: str,
        purpose: str,
        trigger: str,
        *,
        trigger_event_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        """登记处置任务。同一(学生,用途)存在未完成任务时复用——重复撤回只处理一次。"""

        def _do() -> str:
            existing = self._find_open_job(student_id, purpose)
            if existing is not None:
                return existing
            job_id = f"J-{self.store.next_seq('job'):04d}"
            steps = [
                {"name": "analyze_impact", "state": StepState.PENDING.value, "detail": None},
                {"name": "pause_access", "state": StepState.PENDING.value, "detail": None},
                {"name": "rebuild_datasets", "state": StepState.PENDING.value, "detail": None},
                {"name": "destroy_body", "state": StepState.PENDING.value, "detail": None},
                {"name": "seal_conclusions", "state": StepState.PENDING.value, "detail": None},
            ]
            self.store.state["jobs"][job_id] = {
                "job_id": job_id,
                "student_id": student_id,
                "purpose": purpose,
                "trigger": trigger,
                "trigger_event_id": trigger_event_id,
                "state": JobState.PENDING.value,
                "steps": steps,
                "created_at": self.clock(),
                "updated_at": self.clock(),
            }
            return job_id
        return self.store.mutate(request_id, _do)

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.store.state["jobs"].get(job_id)
        if job is None:
            raise GovernanceError(f"处置任务不存在：{job_id}")
        return job

    def pending_jobs(self) -> list[dict[str, Any]]:
        return [
            j for j in self.store.state["jobs"].values()
            if j["state"] in (JobState.PENDING.value, JobState.IN_PROGRESS.value, JobState.BLOCKED.value)
        ]

    # ---- 各步骤 -------------------------------------------------------

    def _analyze(self, job: dict[str, Any]) -> None:
        result = self.lineage.impact_for_student(job["student_id"])
        ids = [rid for rid in result["record_ids"]]
        impact = result["impact"]
        job["record_ids"] = ids
        job["affected"] = impact
        job["steps"][0]["detail"] = {
            "records": ids,
            "datasets": impact[NodeKind.DATASET.value],
            "exports": impact[NodeKind.EXPORT.value],
            "conclusions": impact[NodeKind.CONCLUSION.value],
        }

    def _pause(self, job: dict[str, Any]) -> None:
        nodes = self.store.state["nodes"]
        paused = []
        for nid in job["affected"][NodeKind.DATASET.value] + job["affected"][NodeKind.EXPORT.value]:
            node = nodes[nid]
            if node["state"] == NodeState.ACTIVE.value:
                node["state"] = NodeState.PAUSED.value
                paused.append(nid)
        job["steps"][1]["detail"] = {"paused": paused}

    def _rebuild(self, job: dict[str, Any]) -> bool:
        nodes = self.store.state["nodes"]
        excluded = {job["student_id"]}
        rebuilt: list[str] = []
        blocked: list[dict[str, str]] = []
        for ds_id in job["affected"][NodeKind.DATASET.value]:
            old = nodes[ds_id]
            if old["state"] == NodeState.DESTROYED.value:
                continue
            case_id = self.holds.active_case(ds_id)
            if case_id is not None:
                # 争议保全期间不得基于被隔离的正文重建
                blocked.append({"node_id": ds_id, "case_id": case_id})
                continue
            kept_records = [
                src["record_id"]
                for src in old["source_snapshot"]["sources"]
                if src["student_id"] != job["student_id"]
                and self.lineage.consent.effective_basis(src["student_id"], old["purpose"]) is not None
            ]
            if not kept_records:
                continue
            new_payload = self.rebuild_transformer(old, excluded)
            new_node = self.lineage.build_dataset(
                f"{old['display_name']}（重建）",
                kept_records,
                old["purpose"],
                old["minimization_rules"],
                new_payload,
                supersedes=ds_id,
            )
            rebuilt.append(new_node["node_id"])
        job["steps"][2]["detail"] = {"rebuilt": rebuilt, "blocked": blocked}
        return not blocked

    def _receipt_exists(self, node_id: str) -> bool:
        return any(r["node_id"] == node_id for r in self.store.state["receipts"])

    def _destroy_body(self, job: dict[str, Any]) -> bool:
        """销毁正文并出具回执。返回 False 表示被争议保全阻塞。"""
        nodes = self.store.state["nodes"]
        candidates = list(job["affected"][NodeKind.DATASET.value]) + list(job["affected"][NodeKind.EXPORT.value])
        destroyed: list[str] = []
        blocked: list[dict[str, str]] = []
        for nid in candidates:
            node = nodes[nid]
            if node["state"] == NodeState.DESTROYED.value or self._receipt_exists(nid):
                continue  # 销毁回执只处理一次
            case_id = self.holds.active_case(nid)
            if case_id is not None:
                blocked.append({"node_id": nid, "case_id": case_id})
                continue
            if "payload" in node:
                del node["payload"]
            node["state"] = NodeState.DESTROYED.value
            receipt = {
                "receipt_id": f"RC-{self.store.next_seq('receipt'):04d}",
                "node_id": nid,
                "kind": node["kind"],
                "content_hash": node["content_hash"],
                "destroyed_at": self.clock(),
                "job_id": job["job_id"],
                "student_id": job["student_id"],
            }
            self.store.state["receipts"].append(receipt)
            destroyed.append(nid)
        job["steps"][3]["detail"] = {"destroyed": destroyed, "blocked": blocked}
        return not blocked

    def _seal_conclusions(self, job: dict[str, Any]) -> None:
        nodes = self.store.state["nodes"]
        sealed = []
        for cid in job["affected"][NodeKind.CONCLUSION.value]:
            node = nodes[cid]
            # 历史结论绝不销毁/改写，仅标注其依据已失效并封存
            node["state"] = NodeState.HISTORICAL_LOCKED.value
            node["history_note"] = (
                f"依据的部分授权已失效（学生 {job['student_id']} / 用途 {job['purpose']}）；"
                "结论文本保持原样封存，重建后的数据集可另出新结论。"
            )
            sealed.append(cid)
        job["steps"][4]["detail"] = {"sealed": sealed}

    def run_job(self, job_id: str) -> dict[str, Any]:
        """执行（或重启后继续）处置任务。每步幂等，可安全重入。"""
        job = self.get_job(job_id)
        runners = [self._analyze, self._pause, self._rebuild, self._destroy_body, self._seal_conclusions]
        if job["state"] == JobState.DONE.value:
            return job
        job["state"] = JobState.IN_PROGRESS.value

        blocked = False
        for index, runner in enumerate(runners):
            step = job["steps"][index]
            if step["state"] == StepState.DONE.value:
                continue
            ok = runner(job)
            if ok is False:  # 销毁被争议保全阻塞
                step["state"] = StepState.SKIPPED.value
                blocked = True
                break
            step["state"] = StepState.DONE.value
            job["updated_at"] = self.clock()
            self.store.save()  # 每步落盘：重启后从下一步继续

        if blocked:
            job["state"] = JobState.BLOCKED.value
        elif all(s["state"] == StepState.DONE.value for s in job["steps"]):
            job["state"] = JobState.DONE.value
        job["updated_at"] = self.clock()
        self.store.save()
        return job

    def resume_blocked(self) -> list[str]:
        """争议解除后续跑被阻塞的任务；仍有保全的继续等待。"""
        resumed = []
        for job in list(self.store.state["jobs"].values()):
            if job["state"] != JobState.BLOCKED.value:
                continue
            held_nodes = {
                nid for nid in self.store.state["nodes"] if self.holds.is_held(nid)
            }
            still_blocked = False
            for step in job["steps"]:
                if step["state"] != StepState.SKIPPED.value or not step["detail"]:
                    continue
                blocked_items = step["detail"].get("blocked", [])
                if any(b["node_id"] in held_nodes for b in blocked_items):
                    still_blocked = True
                    break
            if still_blocked:
                continue
            for step in job["steps"]:  # 重置被跳过的步骤，其余 DONE 步骤不重跑
                if step["state"] == StepState.SKIPPED.value:
                    step["state"] = StepState.PENDING.value
                    step["detail"] = None
            self.run_job(job["job_id"])
            resumed.append(job["job_id"])
        return resumed

    def run_all_pending(self) -> list[str]:
        done = []
        for job in self.pending_jobs():
            self.run_job(job["job_id"])
            done.append(job["job_id"])
        return done
