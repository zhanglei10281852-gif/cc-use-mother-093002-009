# 跨校学习数据授权治理

面向中国—东盟院校联合中文课程项目的**学习数据授权治理服务**。以仅追加事件日志为唯一
事实来源，围绕授权文本版本化、同意生命周期、数据集来源固化与撤回后的下游处置提供完整能力。

## 治理能力

| 需求 | 实现 |
| --- | --- |
| 按用途/字段类别/保留期限/接收机构管理**可版本化授权文本** | `register_policy`；同一文本登记即产生新版本，`policy-diff` 标出扩大的用途 |
| 新研究用途是否被旧授权覆盖 | 覆盖范围在学生决定时**按当时文本快照固化**；新增用途不经重新同意即判定不覆盖 |
| 同意 / 拒绝 / 撤回 / 重新同意 | `consent`、`withdraw`；拒绝推翻先前同意，重新同意自动恢复访问并取消未执行处置 |
| 生成数据集时固化来源快照、最小化规则、授权依据 | 每条快照记录固化 `decision_id`；drop/hash/k-匿名规则随数据集冻结 |
| 派生谱系 | 数据集 → 导出文件 → 分析报告 → 研究结论多级谱系，`lineage` 双向查询 |
| 撤回/用途扩大/授权失效的影响计算 | 自动暂停受影响数据集与下游产物并开出处置任务；尚有其他授权学生时数据集与导出/报告均**重建剔除**，否则销毁 |
| 历史研究结论不可被悄悄改写 | 结论不暂停、不销毁、不重建，只允许**追加标注**（撤回传播标注含事件引用） |
| 暂停访问 / 执行重建 / 销毁证明 | 重建生成后继数据集并出具旧集销毁证明；销毁目标带证书编号，不可重复出证 |
| 接收机构销毁回执只处理一次 | 回执带 `request_key` 幂等登记，重复提交返回同一销毁证明 |
| 重复撤回只处理一次 | 撤回 `request_key` 幂等；处置任务按「目标+学生+动作」去重 |
| 访问申请职责分离 | 申请人不可审批本人申请，须第二人（如 DPO）审批；暂停/已销毁目标不可批准 |
| 争议期间保留审计、限制正文 | 争议封存叠加暂停起因并阻塞处置任务；解除后按授权现状决定恢复或继续暂停 |
| 处置进度重启可恢复 | 所有进度写入事件日志，重放还原（含 open/in_progress/blocked/completed/cancelled） |
| DPO 导出学生授权账本 | `ledger export <student>`：为何曾可用、流向哪些产物、每次撤回如何传播、处置进度 |

## 架构

```
src/consent_governance/
  contracts.py   常量与值对象（授权版本、快照条目、状态枚举）
  errors.py      领域错误（覆盖不足、职责分离、结论不可改写等）
  storage.py     仅追加 JSONL 事件日志（append + fsync，顺序重放）
  models.py      重放后的状态模型（授权文本、同意、撤回、数据集、产物、任务、证明…）
  service.py     核心领域服务（覆盖判定、撤回传播、重建/销毁、审批、争议、保留期）
  ledger.py      学生授权账本构建器（结构化输出，供 CLI 渲染）
run_cli.py       DPO 命令行
tests/           38 个单元测试（含跨进程重启恢复）
```

事件带单调序号 `seq`：即使时钟同刻（批处理/固定时钟测试），撤回与重新同意的先后也不会错序。

## 运行

```bash
# 测试
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src tests run_cli.py

# 端到端演示（授权→建集→派生→职责分离审批→撤回→重建/销毁→重启读账本）
python3 run_cli.py demo
```

## 命令行用法

事件日志路径由 `--store` 指定（默认 `data/governance.jsonl`），全部状态在进程重启后重放恢复。

```bash
# 1. 登记授权文本（同一 --id 再次登记即新版本）
python3 run_cli.py --store data/g.jsonl policy \
  --name "东盟中文课程授权书" \
  --purpose course_improvement --purpose pronunciation_research \
  --category behavior --category assessment \
  --recipient CN-UNIV-A --recipient ASEAN-UNIV-B \
  --retention-days 365

python3 run_cli.py --store data/g.jsonl policy-diff --id P-xxxx

# 2. 同意 / 拒绝 / 撤回 / 保留期扫描
python3 run_cli.py --store data/g.jsonl consent S-001 --policy P-xxxx
python3 run_cli.py --store data/g.jsonl consent S-001 --policy P-xxxx --decision denied
python3 run_cli.py --store data/g.jsonl withdraw S-001 --key wdr-001
python3 run_cli.py --store data/g.jsonl withdraw S-001 --scope purpose --purpose pronunciation_research
python3 run_cli.py --store data/g.jsonl sweep-retention

# 3. 数据集（输入为 JSON：用途、字段类别、接收方、最小化规则、来源记录）
python3 run_cli.py --store data/g.jsonl dataset ds.json
# ds.json: {"purpose": "...", "field_categories": [...], "recipients": [...],
#           "minimization": {"drop_fields": [...], "hash_fields": [...], "k_anonymity": 5},
#           "records": [{"record_id": "...", "student_id": "...", "category": "...", "fields": [...]}]}

# 4. 下游产物与谱系
python3 run_cli.py --store data/g.jsonl artifact export "东盟导出包" --dataset D-xxxx
python3 run_cli.py --store data/g.jsonl artifact report "分析报告" --parent F-xxxx
python3 run_cli.py --store data/g.jsonl artifact conclusion "研究结论" --dataset D-xxxx
python3 run_cli.py --store data/g.jsonl lineage D-xxxx

# 5. 访问申请（职责分离）
python3 run_cli.py --store data/g.jsonl request-submit analyst-li dataset D-xxxx course_improvement
python3 run_cli.py --store data/g.jsonl request-decide Q-xxxx dpo-wang --approve

# 6. 争议封存 / 解除
python3 run_cli.py --store data/g.jsonl dispute open S-001
python3 run_cli.py --store data/g.jsonl dispute close U-xxxx

# 7. 处置任务与销毁回执
python3 run_cli.py --store data/g.jsonl task-list
python3 run_cli.py --store data/g.jsonl task-progress T-xxxx 40 --detail "已定位快照条目"
python3 run_cli.py --store data/g.jsonl task-progress T-xxxx 0 --block --reason "等待回执"
python3 run_cli.py --store data/g.jsonl task-complete T-xxxx
python3 run_cli.py --store data/g.jsonl receipt rcpt.json   # 含 request_key，幂等

# 8. DPO 导出一名学生的完整授权账本（文本 / JSON）
python3 run_cli.py --store data/g.jsonl ledger export S-001
python3 run_cli.py --store data/g.jsonl ledger export S-001 --format json
```

## 账本七节内容

1. **当前授权状态**：每个用途现在是否有效及原因（未同意/拒绝/撤回事件/保留期届满/字段或接收方未覆盖）
2. **授权决定时间线**：同意、拒绝、撤回、争议按序排列，保留期截止时间
3. **记录为何曾可用**：进入了哪些数据集，每条记录最小化后的字段与授权依据（同意事件+文本版本）
4. **流向哪些产物**：根源数据集、多级下游、销毁证明、结论追加标注
5. **每次撤回如何传播**：暂停的数据集/产物、结论标注、开出的任务、后续何时恢复
6. **处置任务与进度**：动作、起因、状态百分比、阻塞原因、后继数据集、销毁证明
7. **销毁证明**：证书编号、目标、方式、时间、关联任务或接收机构回执键
