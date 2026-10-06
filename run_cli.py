#!/usr/bin/env python3
"""数据保护负责人（DPO）命令行。

示例：
    python run_cli.py --store data/governance.jsonl ledger export S-001
    python run_cli.py --store data/governance.jsonl ledger export S-001 --format json
    python run_cli.py demo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from consent_governance import contracts as C
from consent_governance.errors import GovernanceError
from consent_governance.ledger import CAUSE_LABELS, build_ledger
from consent_governance.service import GovernanceService
from consent_governance.storage import EventStore

SCOPE_LABELS = {
    C.SCOPE_ALL: "全部授权",
    C.SCOPE_POLICY: "指定授权文本",
    C.SCOPE_PURPOSE: "指定用途",
}
ACTION_LABELS = {C.ACTION_REBUILD: "重建", C.ACTION_DESTROY: "销毁"}
TARGET_LABELS = {C.TARGET_DATASET: "数据集", C.TARGET_ARTIFACT: "下游产物"}
TASK_STATUS_LABELS = {
    C.TASK_OPEN: "待处置",
    C.TASK_IN_PROGRESS: "处置中",
    C.TASK_BLOCKED: "阻塞",
    C.TASK_COMPLETED: "已完成",
    C.TASK_CANCELLED: "已取消",
}
KIND_LABELS = {
    C.ARTIFACT_EXPORT: "导出文件",
    C.ARTIFACT_REPORT: "分析报告",
    C.ARTIFACT_CONCLUSION: "研究结论（不可改写）",
}
REASON_LABELS = {
    "no_consent": "从未同意",
    "denied": "已拒绝",
    "purpose_not_covered": "用途不在授权范围内",
    "retention_expired": "保留期限届满",
    "not_covered": "不在授权范围内",
}


def svc(args) -> GovernanceService:
    return GovernanceService(EventStore(args.store))


# ---------------------------------------------------------------- 子命令
def cmd_policy(args) -> None:
    s = svc(args)
    rev = s.register_policy(
        display_name=args.name,
        purposes=args.purpose,
        field_categories=args.category,
        retention_days=args.retention_days,
        recipients=args.recipient,
        note=args.note or "",
        policy_id=args.id,
    )
    print(json.dumps({
        "policy_id": rev.policy_id,
        "revision": rev.revision,
        "purposes": sorted(rev.purposes),
        "field_categories": sorted(rev.field_categories),
        "retention_days": rev.retention_days,
        "recipients": sorted(rev.recipients),
    }, ensure_ascii=False, indent=2))


def cmd_policy_diff(args) -> None:
    print(json.dumps(svc(args).policy_diff(args.id), ensure_ascii=False, indent=2))


def cmd_consent(args) -> None:
    s = svc(args)
    rec = s.record_consent(args.student, args.policy, args.decision)
    print(json.dumps({
        "event_id": rec.event_id,
        "student_id": rec.student_id,
        "decision": rec.decision,
        "policy_id": rec.policy_id,
        "policy_revision": rec.policy_revision,
        "recorded_at": rec.recorded_at.isoformat(),
        "retention_deadline": rec.retention_deadline().isoformat(),
    }, ensure_ascii=False, indent=2))


def cmd_withdraw(args) -> None:
    result = svc(args).withdraw(
        args.student, scope=args.scope, policy_id=args.policy,
        purpose=args.purpose, request_key=args.key,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_dataset(args) -> None:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    s = svc(args)
    ds = s.generate_dataset(
        purpose=payload["purpose"],
        source_records=payload["records"],
        field_categories=payload["field_categories"],
        recipients=payload["recipients"],
        minimization_rules=payload.get("minimization"),
        policy_id=payload.get("policy_id"),
    )
    print(json.dumps({
        "dataset_id": ds.dataset_id,
        "students": sorted(ds.student_ids()),
        "snapshot_size": len(ds.snapshot),
        "policy_id": ds.policy_id,
    }, ensure_ascii=False, indent=2))


def cmd_artifact(args) -> None:
    art = svc(args).derive_artifact(
        kind=args.kind, name=args.name, dataset_id=args.dataset,
        parent_artifact_id=args.parent,
        student_ids=args.student or None,
    )
    print(json.dumps({"artifact_id": art.artifact_id, "kind": art.kind}, ensure_ascii=False))


def cmd_lineage(args) -> None:
    print(json.dumps(svc(args).lineage(args.id), ensure_ascii=False, indent=2))


def cmd_request_submit(args) -> None:
    req = svc(args).submit_request(args.requester, args.target_kind, args.target_id, args.purpose)
    print(json.dumps({"request_id": req.request_id, "status": req.status}, ensure_ascii=False))


def cmd_request_decide(args) -> None:
    req = svc(args).decide_request(args.request_id, args.approver, args.approve, args.reason or "")
    print(json.dumps({"request_id": req.request_id, "status": req.status, "approver": req.approver},
                     ensure_ascii=False))


def cmd_dispute(args) -> None:
    s = svc(args)
    if args.dispute_action == "open":
        d = s.open_dispute(args.student, scope=args.scope, policy_id=args.policy, purpose=args.purpose)
    else:
        d = s.close_dispute(args.dispute_id)
    print(json.dumps({
        "dispute_id": d.dispute_id, "active": d.active,
        "opened_at": d.opened_at.isoformat(),
    }, ensure_ascii=False))


def cmd_sweep(args) -> None:
    print(json.dumps(svc(args).sweep_retention(), ensure_ascii=False, indent=2))


def cmd_task_list(args) -> None:
    rows = [
        {
            "task_id": t.task_id,
            "target": f"{t.target_kind}:{t.target_id}",
            "students": sorted(t.student_ids),
            "action": t.action,
            "cause": t.cause,
            "status": t.status,
            "progress_percent": t.progress_percent,
        }
        for t in svc(args).pending_tasks()
    ]
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def cmd_task_progress(args) -> None:
    s = svc(args)
    t = s.block_task(args.task_id, args.reason) if args.block else s.progress_task(
        args.task_id, args.percent, args.detail or "")
    print(json.dumps({"task_id": t.task_id, "status": t.status, "percent": t.progress_percent},
                     ensure_ascii=False))


def cmd_task_complete(args) -> None:
    t = svc(args).complete_task(args.task_id, args.method)
    print(json.dumps({
        "task_id": t.task_id,
        "status": t.status,
        "certificate_id": t.certificate_id,
        "result_dataset_id": t.result_dataset_id,
        "result_artifact_id": t.result_artifact_id,
    }, ensure_ascii=False))


def cmd_receipt(args) -> None:
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    cert = svc(args).register_destruction_receipt(
        payload["target_kind"], payload["target_id"],
        payload["student_ids"], payload.get("method", "recipient_secure_deletion"),
        payload["request_key"],
    )
    print(json.dumps({"certificate_id": cert.certificate_id}, ensure_ascii=False))


# ------------------------------------------------------------ 账本导出
def reason_text(code: str) -> str:
    if code.startswith("withdrawn:"):
        parts = code.split(":")
        return f"已被撤回事件 {parts[-1]} 撤回"
    if code.startswith("field_categories_not_covered:"):
        return "字段类别未授权: " + code.split(":", 1)[1]
    if code.startswith("recipients_not_covered:"):
        return "接收机构未授权: " + code.split(":", 1)[1]
    return REASON_LABELS.get(code, code)


def render_ledger_text(ledger: dict) -> str:
    L: list[str] = []
    push = L.append
    push(f"学生授权账本  学生 {ledger['student_id']}  生成于 {ledger['generated_at']}")
    push("=" * 78)

    push("\n【一、当前授权状态】")
    if not ledger["current_coverage"]:
        push("  （无任何授权记录）")
    for purpose, cov in ledger["current_coverage"].items():
        if cov["covered"]:
            push(f"  ✔ 用途 {purpose}：有效（依据同意事件 {cov['decision_id']}）")
        else:
            push(f"  ✘ 用途 {purpose}：不可用 —— " + "；".join(reason_text(r) for r in cov["reasons"]))

    push("\n【二、授权决定时间线】（覆盖范围为决定当时固化的文本快照）")
    timeline = []
    for d in ledger["decisions"]:
        label = "同意" if d["kind"] == C.DECISION_GRANTED else "拒绝"
        if d["re_consent"]:
            label = "重新同意"
        timeline.append((d["at"], f"  {d['at']}  {label}（{d['event_id']}，文本 {d['policy_id']} 第 {d['policy_revision']} 版）"
                                  f" 用途={','.join(d['purposes'])} 字段={','.join(d['field_categories'])}"
                                  f" 接收方={','.join(d['recipients'])} 保留 {d['retention_days']} 天"
                                  f"（至 {d['retention_deadline']}{'，已届满' if d['expired'] else ''}）"))
    for w in ledger["withdrawals"]:
        scope = SCOPE_LABELS[w["scope"]]
        detail = f"，文本={w['policy_id']}" if w["policy_id"] else ""
        detail += f"，用途={w['purpose']}" if w["purpose"] else ""
        key = f"，请求键={w['request_key']}" if w["request_key"] else ""
        timeline.append((w["at"], f"  {w['at']}  撤回（{w['event_id']}，范围：{scope}{detail}{key}）"))
    for d in ledger["disputes"]:
        timeline.append((d["opened_at"], f"  {d['opened_at']}  争议开启（{d['dispute_id']}）"
                                         f"{'，已解除于 ' + d['closed_at'] if d['closed_at'] else '，争议封存中'}"))
    for _, line in sorted(timeline):
        push(line)

    push("\n【三、记录为何曾可用：数据集来源快照】")
    if not ledger["dataset_flows"]:
        push("  （该生记录未进入任何数据集）")
    for f in ledger["dataset_flows"]:
        state = "已销毁" if f["destroyed"] else ("该生记录已暂停访问" if f["suspended_now"] else "可访问")
        causes = "、".join(CAUSE_LABELS.get(c, c) for c in f["suspension_causes"])
        cause = f"，起因：{causes}" if causes else ""
        push(f"  数据集 {f['dataset_id']}（用途 {f['purpose']}，文本 {f['policy_id']}，{state}{cause}）")
        push(f"    字段类别：{','.join(f['field_categories'])}；接收机构：{','.join(f['recipients'])}")
        push(f"    最小化规则：{json.dumps(f['minimization'], ensure_ascii=False)}")
        if f["certificate_id"]:
            push(f"    销毁证明：{f['certificate_id']}")
        for r in f["records"]:
            push(f"    - 记录 {r['record_id']}（类别 {r['category']}）"
                 f" 最小化后字段={','.join(r['fields_after_minimization']) or '（无）'}")
            push(f"      授权依据：同意事件 {r['decision_id']}（{r['decided_at']}，"
                 f"文本 {r['policy_id']} 第 {r['policy_revision']} 版覆盖用途 {f['purpose']}）")

    push("\n【四、流向哪些产物】（派生谱系）")
    if not ledger["artifact_flows"]:
        push("  （无下游产物）")
    for f in ledger["artifact_flows"]:
        if f["destroyed"]:
            state = "已销毁"
        elif f["immutable_conclusion"]:
            marked = any(a.get("kind") == "withdrawal_propagation" for a in f["annotations"])
            state = "历史结论保留，正文不改写，撤回已追加标注" if marked else "历史结论（不可改写）"
        elif f["suspended_now"]:
            state = "该生记录已暂停"
        else:
            state = "可访问"
        push(f"  {KIND_LABELS[f['kind']]} {f['artifact_id']}「{f['name']}」（{state}）")
        push(f"    根源数据集：{','.join(f['root_datasets'])}"
             + (f"；上游产物：{f['parent_artifact_id']}" if f["parent_artifact_id"] else ""))
        if f["descendants"]:
            push(f"    再下游产物：{','.join(f['descendants'])}")
        if f["certificate_id"]:
            push(f"    销毁证明：{f['certificate_id']}")
        if f.get("rebuilt_as"):
            push(f"    后继产物：{f['rebuilt_as']}")
        for ann in f["annotations"]:
            push(f"    ［追加标注·{ann['at']}］{ann.get('text', ann.get('note', ''))}")

    push("\n【五、授权失效如何传播】（撤回或之后作出的拒绝）")
    if not ledger["withdrawal_propagation"]:
        push("  （无失效传播记录）")
    for b in ledger["withdrawal_propagation"]:
        trigger_label = "拒绝决定" if b.get("kind") == "denial" else "撤回"
        push(f"  {trigger_label} {b['withdrawal_event_id']}（{b['at']}，范围 {SCOPE_LABELS[b['scope']]}）：")
        push(f"    暂停数据集：{', '.join(b['suspended_datasets']) or '无'}")
        push(f"    暂停下游产物：{', '.join(b['suspended_artifacts']) or '无'}")
        push(f"    研究结论仅追加标注：{', '.join(b['annotated_conclusions']) or '无'}")
        push(f"    开出处置任务：{', '.join(b.get('opened_tasks', [])) or '无'}")
        if b.get("resumed_after"):
            for r in b["resumed_after"]:
                push(f"    后续恢复：{TARGET_LABELS[r['target_kind']]} {r['target_id']} 于 {r['at']} 恢复访问")

    push("\n【六、处置任务与进度】（进度由事件日志重放，重启后可恢复）")
    if not ledger["dispositions"]:
        push("  （无处置任务）")
    for t in ledger["dispositions"]:
        push(f"  任务 {t['task_id']}：{ACTION_LABELS[t['action']]} {TARGET_LABELS[t['target_kind']]}"
             f" {t['target_id']} 中学生 {','.join(sorted(t['student_ids']))} 的记录")
        push(f"    状态：{TASK_STATUS_LABELS[t['status']]}（{t['progress_percent']}%），"
             f"起因：{CAUSE_LABELS.get(t['cause'], t['cause'])}；{t['rationale']}")
        if t["detail"]:
            push(f"    进度说明：{t['detail']}")
        if t["blocked_reason"]:
            push(f"    阻塞原因：{t['blocked_reason']}")
        if t["result_dataset_id"]:
            push(f"    重建后继数据集：{t['result_dataset_id']}")
        if t.get("result_artifact_id"):
            push(f"    重建后继产物：{t['result_artifact_id']}")
        if t["certificate_id"]:
            push(f"    销毁证明：{t['certificate_id']}")

    push("\n【七、销毁证明】")
    if not ledger["certificates"]:
        push("  （无）")
    for c in ledger["certificates"]:
        push(f"  {c['certificate_id']}：{TARGET_LABELS[c['target_kind']]} {c['target_id']}"
             f" 于 {c['certified_at']} 按「{c['method']}」销毁"
             + (f"（任务 {c['task_id']}）" if c["task_id"] else "")
             + (f"（接收机构回执 {c['request_key']}）" if c["request_key"] else ""))
    return "\n".join(L)


def cmd_ledger_export(args) -> None:
    ledger = build_ledger(svc(args), args.student)
    if args.format == "json":
        print(json.dumps(ledger, ensure_ascii=False, indent=2))
    else:
        print(render_ledger_text(ledger))


# ---------------------------------------------------------------- demo
def cmd_demo(args) -> None:
    """端到端演示：授权→建集→派生→撤回→重建/销毁→重新同意→争议→重启读账本。"""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="cg-demo-")) / "log.jsonl"

    def open_service():
        return GovernanceService(EventStore(tmp))

    s = open_service()
    p = s.register_policy(
        "东盟联合项目中文学习数据授权书",
        purposes=["course_improvement", "pronunciation_research"],
        field_categories=["behavior", "assessment"],
        retention_days=365,
        recipients=["CN-UNIV-A", "ASEAN-UNIV-B"],
    )
    s.record_consent("S-001", p.policy_id)
    s.record_consent("S-002", p.policy_id)
    ds = s.generate_dataset(
        "course_improvement",
        [
            {"record_id": "R-1", "student_id": "S-001", "category": "behavior",
             "fields": ["video_face", "click_stream", "exercise_log"]},
            {"record_id": "R-2", "student_id": "S-002", "category": "behavior",
             "fields": ["video_face", "click_stream"]},
        ],
        field_categories=["behavior"],
        recipients=["CN-UNIV-A", "ASEAN-UNIV-B"],
        minimization_rules={"drop_fields": ["video_face"], "hash_fields": ["student_no"]},
    )
    export = s.derive_artifact(C.ARTIFACT_EXPORT, "发给东盟院校的导出包", dataset_id=ds.dataset_id)
    conclusion = s.derive_artifact(
        C.ARTIFACT_CONCLUSION, "2026 春季声调教学结论", dataset_id=ds.dataset_id
    )

    # 访问申请：申请人自批被拒；第二人审批通过
    req = s.submit_request("analyst-li", C.TARGET_DATASET, ds.dataset_id, "course_improvement")
    try:
        s.decide_request(req.request_id, "analyst-li", True)
    except GovernanceError as exc:
        print(f"[职责分离] {exc}")
    s.decide_request(req.request_id, "dpo-wang", True, "DPO 复核授权与最小化规则")

    # S-001 撤回
    print(f"[撤回] {json.dumps(s.withdraw('S-001', request_key='wdr-0001'), ensure_ascii=False)}")
    # 重复撤回（同一请求键）只处理一次
    print(f"[重复撤回] {json.dumps(s.withdraw('S-001', request_key='wdr-0001'), ensure_ascii=False)}")

    task = next(t for t in s.pending_tasks()
                if t.target_kind == C.TARGET_DATASET and t.action == C.ACTION_REBUILD)
    s.progress_task(task.task_id, 40, "已定位快照条目与下游导出包")
    s.complete_task(task.task_id)

    # 导出包的重建处置推进到 60% 后模拟崩溃
    export_task = next(t for t in s.pending_tasks() if t.target_id == export.artifact_id)
    s.progress_task(export_task.task_id, 60, "正在剔除 S-001 的导出记录")

    # 研究结论不可改写：服务层不提供正文修改入口，只能追加标注
    s.annotate_conclusion(conclusion.artifact_id, "DPO 复核：撤回传播标注已核对")
    reloaded = s.artifacts[conclusion.artifact_id]
    assert reloaded.name == "2026 春季声调教学结论"
    assert len(reloaded.annotations) >= 1
    print(f"[不可改写] 研究结论正文保持「{reloaded.name}」，仅追加 {len(reloaded.annotations)} 条标注")

    # 模拟重启：重新打开服务，未完成的导出重建任务进度仍在并继续执行
    s2 = open_service()
    pending = s2.pending_tasks()
    print(f"[重启恢复] 未完成处置任务 {len(pending)} 个："
          f"{[(t.task_id, t.status, t.progress_percent) for t in pending]}")
    for t in pending:
        s2.complete_task(t.task_id)
    ledger = build_ledger(s2, "S-001")
    print("\n" + render_ledger_text(ledger))


# ---------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="学习数据授权治理 CLI")
    parser.add_argument("--store", default="data/governance.jsonl", help="事件日志文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("policy", help="登记/版本化授权文本")
    p.add_argument("--id")
    p.add_argument("--name", required=True)
    p.add_argument("--purpose", action="append", required=True)
    p.add_argument("--category", action="append", required=True)
    p.add_argument("--recipient", action="append", required=True)
    p.add_argument("--retention-days", type=int, required=True)
    p.add_argument("--note")
    p.set_defaults(func=cmd_policy)

    p = sub.add_parser("policy-diff", help="查看授权文本最新两版差异")
    p.add_argument("--id", required=True)
    p.set_defaults(func=cmd_policy_diff)

    p = sub.add_parser("consent", help="记录同意/拒绝")
    p.add_argument("student")
    p.add_argument("--policy", required=True)
    p.add_argument("--decision", choices=[C.DECISION_GRANTED, C.DECISION_DENIED],
                   default=C.DECISION_GRANTED)
    p.set_defaults(func=cmd_consent)

    p = sub.add_parser("withdraw", help="撤回授权并传播")
    p.add_argument("student")
    p.add_argument("--scope", choices=C.SCOPE_KINDS, default=C.SCOPE_ALL)
    p.add_argument("--policy")
    p.add_argument("--purpose")
    p.add_argument("--key", help="撤回请求幂等键")
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("dataset", help="从 JSON 描述生成数据集")
    p.add_argument("file")
    p.set_defaults(func=cmd_dataset)

    p = sub.add_parser("artifact", help="派生导出/报告/结论")
    p.add_argument("kind", choices=C.ARTIFACT_KINDS)
    p.add_argument("name")
    p.add_argument("--dataset")
    p.add_argument("--parent")
    p.add_argument("--student", action="append")
    p.set_defaults(func=cmd_artifact)

    p = sub.add_parser("lineage", help="查看某目标的派生谱系")
    p.add_argument("id")
    p.set_defaults(func=cmd_lineage)

    p = sub.add_parser("request-submit", help="提交访问申请")
    p.add_argument("requester")
    p.add_argument("target_kind", choices=C.TARGET_KINDS)
    p.add_argument("target_id")
    p.add_argument("purpose")
    p.set_defaults(func=cmd_request_submit)

    p = sub.add_parser("request-decide", help="第二人审批访问申请")
    p.add_argument("request_id")
    p.add_argument("approver")
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_request_decide)

    p = sub.add_parser("dispute", help="开启/关闭争议封存")
    p.add_argument("dispute_action", choices=["open", "close"])
    p.add_argument("value", help="open 时为学生编号；close 时为争议编号")
    p.add_argument("--scope", choices=C.SCOPE_KINDS, default=C.SCOPE_ALL)
    p.add_argument("--policy")
    p.add_argument("--purpose")
    p.set_defaults(func=_dispatch_dispute)

    p = sub.add_parser("sweep-retention", help="扫描保留期届满并处置")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("task-list", help="列出未完成处置任务")
    p.set_defaults(func=cmd_task_list)

    p = sub.add_parser("task-progress", help="更新处置进度")
    p.add_argument("task_id")
    p.add_argument("percent", type=int)
    p.add_argument("--detail", default="")
    p.add_argument("--block", action="store_true")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_task_progress)

    p = sub.add_parser("task-complete", help="执行处置（重建/销毁证明）")
    p.add_argument("task_id")
    p.add_argument("--method", default="secure_deletion")
    p.set_defaults(func=cmd_task_complete)

    p = sub.add_parser("receipt", help="登记接收机构销毁回执（幂等）")
    p.add_argument("file")
    p.set_defaults(func=cmd_receipt)

    ledger = sub.add_parser("ledger", help="学生授权账本")
    lsub = ledger.add_subparsers(dest="ledger_command", required=True)
    e = lsub.add_parser("export", help="导出一名学生的完整授权账本")
    e.add_argument("student")
    e.add_argument("--format", choices=["text", "json"], default="text")
    e.set_defaults(func=cmd_ledger_export)

    sub.add_parser("demo", help="端到端演示").set_defaults(func=cmd_demo)
    return parser


def _dispatch_dispute(args) -> None:
    if args.dispute_action == "open":
        args.student = args.value
        args.dispute_id = None
    else:
        args.dispute_id = args.value
        args.student = None
    cmd_dispute(args)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except GovernanceError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
