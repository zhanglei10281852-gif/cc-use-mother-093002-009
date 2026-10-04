# 跨校学习数据授权治理

面向中国—东盟院校联合学习分析项目的数据保护治理服务：按用途、字段类别、保留期限和接收机构
管理**可版本化的授权文本**，记录同意/拒绝/撤回/重新同意，在生成数据集时**固化来源快照、
最小化规则与派生谱系**；授权失效或用途扩大后计算受影响产物，编排**暂停访问 → 重建 → 销毁
（出具回执）→ 历史结论封存**，处置任务分步持久化、可在进程重启后续跑。

仅依赖 Python 3.11 标准库，状态以原子写入的 JSON 文件保存。

## 领域模型

| 概念 | 说明 |
|---|---|
| 授权文本 Text | 按用途组织；每版固化 `purposes / field_categories / retention_days / recipients`，发布后不可变 |
| 授权事件 | `agree / refuse / withdraw / reagree` 追加式账本，每条同意**钉住具体文本版本号** |
| 学生记录 Record | 原始学习数据登记，带内容哈希 |
| 产物节点 Node | 数据集版本 / 导出文件 / 研究结论，由派生边构成谱系图 |
| 数据集版本 | 生成时冻结来源快照（每条记录当时的同意事件、文本版本）、最小化规则哈希、内容哈希 |
| 处置任务 Job | 5 步：影响分析、暂停访问、重建数据集、销毁正文、封存结论；每步落盘 |
| 销毁回执 Receipt | 对节点内容哈希出具的一次性销毁证明 |
| 争议保全 Hold | 正文隔离为 `quarantined`、阻塞销毁/重建，审计完整保留；解除后续跑 |
| 访问申请 | 申请人不可自批；管家(steward) → DPO 两级且必须不同人审批 |

### 关键规则

- **用途扩大不溯及既往**：旧版本同意不含新用途，按旧版本为新用途授权将被拒绝；须发布新版本并重新取得同意。
- **版本错配即需重建**：学生撤回后按新版本重新同意，旧数据集版本固化的依据变为 `version_mismatch`。
- **历史结论不可改写**：研究结论为 WORM 节点，依据失效后只转为 `historical_locked` 并附说明，结论文本与哈希不变。
- **重复只处理一次**：同一 (学生,用途) 的未完成处置任务复用；重复撤回自然去抖；`--request-id` 提供跨调用幂等；销毁回执对每个节点只出具一次。
- **争议期间**：正文限制访问，销毁与重建阻塞，账本/事件/影响清单等审计全部保留。

## 运行

```bash
python3 -m unittest discover -s tests -v          # 34 个测试
python3 -m compileall -q src tests run_cli.py     # 编译检查
python3 run_cli.py --help                          # 命令行
```

## CLI 快速示例

```bash
# 1. 授权文本与版本
python3 run_cli.py text-register T-CN "跨校中文课程学习数据授权书"
python3 run_cli.py text-publish T-CN "中文学习过程分析" "learning_behavior,academic_result" 365 "广西师大,东盟文理学院"
# 用途扩大：发布 r2（旧同意不覆盖新用途）
python3 run_cli.py text-publish T-CN "中文学习过程分析,自适应辅导AI研究" "learning_behavior,academic_result" 365 "广西师大,东盟文理学院"

# 2. 授权事件
python3 run_cli.py consent agree   S-001 中文学习过程分析 T-CN 1
python3 run_cli.py consent refuse  S-003 中文学习过程分析 T-CN 1
python3 run_cli.py withdraw        S-001 中文学习过程分析 T-CN 1   # 撤回并开启处置任务
python3 run_cli.py consent reagree S-001 中文学习过程分析 T-CN 2

# 3. 记录 → 数据集（固化快照/规则）→ 导出 → 结论
python3 run_cli.py record   S-001 learning_behavior '{"lessons":12}'
python3 run_cli.py dataset  "期中行为数据集v1" "R-0001,R-0002" 中文学习过程分析 '{"learning_behavior":"脱敏聚合"}'
python3 run_cli.py export   "给东盟的导出" D-0001 东盟文理学院
python3 run_cli.py conclusion "投入度结论" D-0001 "行为与成绩正相关"

# 4. 处置（可随时中断/重启，分步续跑）
python3 run_cli.py job-run            # 执行全部待办；也可 job-run J-0001
python3 run_cli.py hold impose CASE-9 --nodes D-0001,X-0002 --reason "授权范围投诉"
python3 run_cli.py job-resume         # 争议解除后续跑被阻塞任务
python3 run_cli.py scan-expired       # 保留期限到期扫描

# 5. 职责分离访问审批
python3 run_cli.py access-request AR-1 alice D-0001 "论文复现"
python3 run_cli.py access-decide AR-1 approve bob steward
python3 run_cli.py access-decide AR-1 approve carol dpo

# 6. 数据保护负责人导出一名学生的授权账本（核心视图）
python3 run_cli.py ledger S-001            # 可读文本
python3 run_cli.py ledger S-001 --json     # 机器可读
```

`ledger` 输出四部分：① 授权时间线及**为何曾可用**（字段类别、保留期限、接收机构、版本用途、
到期时间）；② 记录**流向**哪些数据集/导出/结论及当前状态；③ 每次**撤回如何传播**
（处置任务逐步结果、受影响产物、销毁回执）；④ **未完成处置**在重启后的续跑进度。

## 代码结构

```
src/consent_governance/
  contracts.py    枚举与基础值对象
  store.py        JSON 原子持久化、幂等命令索引、重启加载
  texts.py        授权文本版本管理（不可变快照、用途扩大规则）
  consent.py      同意/拒绝/撤回/重新同意账本与效力判定（含到期）
  lineage.py      记录登记、数据集固化、派生谱系、影响分析、结论 WORM
  disposition.py  处置编排、销毁回执、争议保全、访问审批
  ledger.py       学生授权账本聚合
  service.py      门面（撤回即开任务、到期扫描、续跑）
  cli.py          命令行与账本可读视图
tests/            契约/文本授权/谱系/处置审批/账本 CLI 共 34 个测试
```
