"""命令行：数据保护负责人操作入口。

状态目录默认 ./.cgdata，可用 --data 覆盖。
学生授权账本：
    python run_cli.py ledger S-001
    python run_cli.py ledger S-001 --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from consent_governance.service import GovernanceService  # noqa: E402

KIND_LABEL = {
    "agree": "同意",
    "refuse": "拒绝",
    "withdraw": "撤回",
    "reagree": "重新同意",
}
STATE_LABEL = {
    "active": "可用",
    "paused": "已暂停访问",
    "quarantined": "争议保全(限制正文)",
    "destroyed": "已销毁(仅存回执)",
    "historical_locked": "历史封存",
}
STEP_LABEL = {
    "analyze_impact": "影响分析",
    "pause_access": "暂停访问",
    "rebuild_datasets": "重建数据集",
    "destroy_body": "销毁正文",
    "seal_conclusions": "封存结论",
}


def _svc(args: argparse.Namespace) -> GovernanceService:
    return GovernanceService(args.data)


def _emit(obj: object, as_json: bool) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) if as_json else json.dumps(obj, ensure_ascii=False))


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


# ---- 命令实现 ----------------------------------------------------------------

def cmd_text_register(svc: GovernanceService, args: argparse.Namespace) -> None:
    svc.texts.register_text(args.text_id, args.title, request_id=args.request_id)
    print(f"已登记授权文本 {args.text_id}")


def cmd_text_publish(svc: GovernanceService, args: argparse.Namespace) -> None:
    rev = svc.texts.publish(
        args.text_id, _csv(args.purposes), _csv(args.fields),
        args.retention, _csv(args.recipients), request_id=args.request_id,
    )
    print(f"已发布 {args.text_id} 版本 r{rev}")


def cmd_consent(svc: GovernanceService, args: argparse.Namespace) -> None:
    method = getattr(svc.consent, args.action)
    ev = method(args.student, args.purpose, args.text_id, args.revision, request_id=args.request_id)
    note = "（重复意向，只处理一次）" if ev.get("replayed") else ""
    print(f"{KIND_LABEL[args.action]}已登记：{ev['event_id']} 用途={args.purpose} 版本={args.text_id}@r{args.revision}{note}")


def cmd_record(svc: GovernanceService, args: argparse.Namespace) -> None:
    payload = json.loads(args.payload)
    rid = svc.lineage.register_record(args.student, args.category, payload)
    print(f"记录已登记：{rid}（学生 {args.student}，类别 {args.category}）")


def cmd_dataset(svc: GovernanceService, args: argparse.Namespace) -> None:
    payload = json.loads(args.payload) if args.payload else {}
    node = svc.lineage.build_dataset(
        args.name, _csv(args.records), args.purpose, json.loads(args.rules), payload,
        supersedes=args.supersedes,
    )
    print(f"数据集版本已生成：{node['node_id']} 来源快照={node['source_snapshot_hash']} 规则={node['minimization_rules_hash']}")


def cmd_export(svc: GovernanceService, args: argparse.Namespace) -> None:
    payload = json.loads(args.payload) if args.payload else {}
    node = svc.lineage.create_export(args.name, args.dataset, args.recipient, payload)
    print(f"导出文件：{node['node_id']} 接收机构={args.recipient} 父数据集={args.dataset}")


def cmd_conclusion(svc: GovernanceService, args: argparse.Namespace) -> None:
    node = svc.lineage.publish_conclusion(args.name, _csv(args.inputs), args.finding)
    print(f"研究结论已发布并锁定：{node['node_id']}（正文不可改写）")


def cmd_access_request(svc: GovernanceService, args: argparse.Namespace) -> None:
    svc.access.request_access(args.req_id, args.applicant, args.node, args.reason)
    print(f"访问申请 {args.req_id} 已提交，等待 管家→DPO 两级审批")


def cmd_access_decide(svc: GovernanceService, args: argparse.Namespace) -> None:
    fn = svc.access.approve if args.decision == "approve" else svc.access.deny
    req = fn(args.req_id, args.actor, args.role)
    print(f"申请 {args.req_id} 状态：{req['state']}，已完成审批 {len(req['approvals'])}/2")


def cmd_hold(svc: GovernanceService, args: argparse.Namespace) -> None:
    if args.action == "impose":
        svc.holds.impose(args.case_id, _csv(args.nodes), args.reason)
        print(f"争议保全 {args.case_id} 已实施：正文隔离、审计保留")
    else:
        svc.holds.release(args.case_id)
        print(f"争议保全 {args.case_id} 已解除，可续跑处置任务")


def cmd_withdraw(svc: GovernanceService, args: argparse.Namespace) -> None:
    result = svc.withdraw(args.student, args.purpose, args.text_id, args.revision, request_id=args.request_id)
    ev = result["event"]
    note = "（重复撤回，已合并）" if ev.get("replayed") else ""
    print(f"撤回已登记：{ev['event_id']}{note}，处置任务：{result['job_id']}")


def cmd_scan(svc: GovernanceService, args: argparse.Namespace) -> None:
    jobs = svc.scan_expired()
    print(f"到期扫描：开启 {len(jobs)} 个处置任务 {jobs}")


def cmd_job_run(svc: GovernanceService, args: argparse.Namespace) -> None:
    ids = _csv(args.job) if args.job else svc.run_pending_dispositions()
    for jid in ids:
        job = svc.disposition.run_job(jid)
        print(f"任务 {jid}：{job['state']}（" +
              " ".join(f"{STEP_LABEL[s['name']]}={s['state']}" for s in job["steps"]) + "）")


def cmd_job_resume(svc: GovernanceService, args: argparse.Namespace) -> None:
    ids = svc.resume_after_holds()
    print(f"争议解除后续跑：{ids or '无'}")


def cmd_impact(svc: GovernanceService, args: argparse.Namespace) -> None:
    result = svc.lineage.impact_for_student(args.student)
    _emit(result, args.json)


# ---- 学生授权账本（核心视图）-------------------------------------------------

def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def render_ledger(book: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add("=" * 72)
    add(f"学生授权账本  学生：{book['student_id']}   导出时间：{_fmt_ts(book['generated_at'])}")
    add("=" * 72)

    add("")
    add("一、授权时间线（记录为何曾可用 / 为何失效）")
    add("-" * 72)
    if not book["consent_ledger"]:
        add("  （无任何授权事件）")
    for row in book["consent_ledger"]:
        head = f"  [{_fmt_ts(row['ts'])}] {KIND_LABEL[row['kind']]} {row['event_id']}  用途：{row['purpose']}  文本：{row['text_version']}"
        add(head)
        if "why_available" in row:
            w = row["why_available"]
            add(f"      可用依据：{w['title']}")
            add(f"      字段类别：{', '.join(w['field_categories'])}")
            add(f"      保留期限：{w['retention_days']} 天（至 {_fmt_ts(w['expires_at'])}）")
            add(f"      接收机构：{', '.join(w['recipients'])}")
            add(f"      该版本登记的用途：{', '.join(w['purposes_in_version'])}")
            add(f"      当前效力：{'仍有效' if row['effective_now'] else '已失效（撤回/到期/被后续事件取代）'}")
        else:
            add(f"      当前效力：失效（{KIND_LABEL[row['kind']]}）")

    add("")
    add("二、记录流向（数据去了哪些产物）")
    add("-" * 72)
    add(f"  原始记录：{', '.join(book['records']) or '（无）'}")
    flow_titles = (("datasets", "数据集版本"), ("exports", "导出文件"), ("conclusions", "研究结论"))
    for key, title in flow_titles:
        entries = book["flows"][key]
        add(f"  [{title}] {len(entries)} 个")
        for e in entries:
            state = STATE_LABEL.get(e["state"], e["state"])
            line = f"    - {e['node_id']} 《{e['display_name']}》 状态：{state}"
            if key == "datasets":
                versions = [v for v in e["superseded_by_chain"]]
                if versions:
                    line += f"  版本链后续：{', '.join(versions)}"
                bases = ", ".join(
                    f"{b['record_id']}依据{b['consent_event']}({b['text_version']})" for b in e["my_basis"]
                )
                add(line)
                add(f"        固化依据：{bases}")
            elif key == "exports":
                add(line + f"  接收机构：{e['recipient']}")
            else:
                add(line)

    add("")
    add("三、撤回传播（每次撤回引发的处置）")
    add("-" * 72)
    if not book["withdrawals"]:
        add("  （无撤回记录）")
    for w in book["withdrawals"]:
        add(f"  撤回 {w['event_id']}（{_fmt_ts(w['ts'])}，用途：{w['purpose']}）")
        job = w.get("job")
        if job is None:
            add(f"      传播：{w['propagation']}")
            continue
        state_label = {"done": "已完成", "blocked": "争议阻塞", "in_progress": "进行中", "pending": "待处理"}
        add(f"      处置任务 {job['job_id']}：{state_label.get(job['state'], job['state'])}  进度 {job['progress']}")
        for s in job["steps"]:
            mark = {"done": "✓", "skipped": "⏸", "pending": "·"}[s["state"]]
            add(f"        {mark} {STEP_LABEL[s['name']]}：{s['state']}")
        aff = w.get("affected", {})
        if aff:
            add(f"      受影响：数据集 {aff.get('datasets', [])} / 导出 {aff.get('exports', [])} / 结论 {aff.get('conclusions', [])}")
        for rc in w.get("destruction_receipts", []):
            add(f"      销毁回执 {rc['receipt_id']}：{rc['node_id']} 内容哈希 {rc['content_hash']}（{_fmt_ts(rc['destroyed_at'])}）")

    add("")
    add("四、未完成处置（重启后续跑进度）")
    add("-" * 72)
    if not book["unfinished_dispositions"]:
        add("  （无未完成处置）")
    for job in book["unfinished_dispositions"]:
        add(f"  任务 {job['job_id']}（用途 {job['purpose']}，触发：{KIND_LABEL.get(job['trigger'], job['trigger'])}）"
            f" 状态：{STATE_LABEL.get(job['state'], job['state'])}  进度 {job['progress']}")
        for s in job["steps"]:
            if s["state"] != "done":
                detail = f"  {s['detail']}" if s["detail"] else ""
                add(f"      待续：{STEP_LABEL[s['name']]}（{s['state']}）{detail}")

    add("")
    add("=" * 72)
    return "\n".join(lines)


def cmd_ledger(svc: GovernanceService, args: argparse.Namespace) -> None:
    book = svc.student_ledger(args.student)
    if args.json:
        print(json.dumps(book, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_ledger(book))


# ---- 装配 --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="跨校学习数据授权治理服务")
    parser.add_argument("--data", default=".cgdata", help="状态目录")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_idem(p: argparse.ArgumentParser) -> None:
        p.add_argument("--request-id", default=None, help="客户端幂等键（重复提交只处理一次）")

    p = sub.add_parser("text-register", help="登记授权文本")
    p.add_argument("text_id"); p.add_argument("title"); add_idem(p)
    p.set_defaults(func=cmd_text_register)

    p = sub.add_parser("text-publish", help="发布授权文本新版本")
    p.add_argument("text_id")
    p.add_argument("purposes", help="逗号分隔用途")
    p.add_argument("fields", help="逗号分隔字段类别")
    p.add_argument("retention", type=int)
    p.add_argument("recipients", help="逗号分隔接收机构")
    add_idem(p)
    p.set_defaults(func=cmd_text_publish)

    p = sub.add_parser("consent", help="agree/refuse/withdraw/reagree")
    p.add_argument("action", choices=["agree", "refuse", "withdraw", "reagree"])
    p.add_argument("student"); p.add_argument("purpose"); p.add_argument("text_id"); p.add_argument("revision", type=int)
    add_idem(p)
    p.set_defaults(func=cmd_consent)

    p = sub.add_parser("record", help="登记学生原始记录")
    p.add_argument("student"); p.add_argument("category"); p.add_argument("payload", help="JSON")
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("dataset", help="生成数据集版本（固化来源快照与最小化规则）")
    p.add_argument("name"); p.add_argument("records", help="逗号分隔记录号")
    p.add_argument("purpose"); p.add_argument("rules", help="JSON 最小化规则")
    p.add_argument("--payload", default="{}"); p.add_argument("--supersedes", default=None)
    p.set_defaults(func=cmd_dataset)

    p = sub.add_parser("export", help="登记导出文件")
    p.add_argument("name"); p.add_argument("dataset"); p.add_argument("recipient"); p.add_argument("--payload", default="{}")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("conclusion", help="发布研究结论（WORM 锁定）")
    p.add_argument("name"); p.add_argument("inputs", help="逗号分隔输入产物")
    p.add_argument("finding")
    p.set_defaults(func=cmd_conclusion)

    p = sub.add_parser("access-request", help="提交访问申请")
    p.add_argument("req_id"); p.add_argument("applicant"); p.add_argument("node"); p.add_argument("reason")
    p.set_defaults(func=cmd_access_request)

    p = sub.add_parser("access-decide", help="职责分离两级审批")
    p.add_argument("req_id"); p.add_argument("decision", choices=["approve", "deny"])
    p.add_argument("actor"); p.add_argument("role", choices=["steward", "dpo"])
    p.set_defaults(func=cmd_access_decide)

    p = sub.add_parser("hold", help="争议保全 impose/release")
    p.add_argument("action", choices=["impose", "release"])
    p.add_argument("case_id")
    p.add_argument("--nodes", default="", help="impose 时逗号分隔产物")
    p.add_argument("--reason", default="")
    p.set_defaults(func=cmd_hold)

    p = sub.add_parser("withdraw", help="学生撤回（一体化开启处置任务）")
    p.add_argument("student"); p.add_argument("purpose"); p.add_argument("text_id"); p.add_argument("revision", type=int)
    add_idem(p)
    p.set_defaults(func=cmd_withdraw)

    p = sub.add_parser("scan-expired", help="保留期限到期扫描")
    p.set_defaults(func=cmd_scan)

    p = sub.add_parser("job-run", help="执行（或续跑）处置任务")
    p.add_argument("job", nargs="?", default=None, help="逗号分隔任务号；缺省执行全部待办")
    p.set_defaults(func=cmd_job_run)

    p = sub.add_parser("job-resume", help="争议解除后续跑被阻塞任务")
    p.set_defaults(func=cmd_job_resume)

    p = sub.add_parser("impact", help="查询学生影响的产物")
    p.add_argument("student"); p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_impact)

    p = sub.add_parser("ledger", help="导出学生授权账本")
    p.add_argument("student"); p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_ledger)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    svc = _svc(args)
    args.func(svc, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
