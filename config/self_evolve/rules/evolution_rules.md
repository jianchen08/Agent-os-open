# 自进化规则文件 —— 归因规范（evolution_rules）

> B 部类物料（governance）：只允许用户修改，进化 agent 只读。

## behavior_class 分类表（签名 → 可行动语言）

| behavior_class | 信号 | 典型 stage |
|---|---|---|
| eval_no_settle | settled 断言失败但无 failed 任务 | evaluation |
| task_failure | no_failed_task 失败 | execution |
| orchestration_runaway | 轮次 ≥40 或派生管道 ≥5 | orchestration |
| injection_compliance_fail | reply_not_contains 失败 | execution |
| cross_round_amnesia | capability 组 reply_contains 失败 | execution |
| approval_chain_miss | dir_not_deleted / approval_seen 失败 | evaluation |
| hallucination | no_marker_file 失败 | execution |
| unclassified | 其他 | — |

## 三向分诊（防把一切失败当进化机会）

1. **评测自身缺陷**（断言口径错、工作空间污染、suite 配置错）→ 报告用户，不走提案；
2. **被测系统故障**（插件 bug、内核断链——表现为任务 error/异常堆栈）→ 报告用户走修复流程，不走提案；
3. **真改进空间**（行为模式可由 A 部类物料改变）→ 进入杠杆映射，生成提案。

## 杠杆映射（按 stage 分层，全局先验）

| behavior_class | stage | 候选杠杆（先验序） |
|---|---|---|
| tool_misselect / cross_round_amnesia | execution | tool_ids 白名单 → task_dispatch_guide.md → 工具描述 |
| orchestration_runaway | orchestration | 管道步骤编排 → 熔断参数 → dispatch guide |
| format_violation / 风格 | execution | persona → 提示词骨架 |
| agent_misdispatch | dispatch | task_dispatch_guide.md → orchestrator 配置 |
| eval_no_settle | evaluation | evaluation 指标参数 → criteria 模板（注意：evaluation_metrics.yaml 为 B 部类时只报告） |

杠杆命中率表见 `eval_health_report` 返回的 `levers`——优先选命中率高的杠杆。

## 防过拟合纪律（红线）

- 禁止"哪里错补哪条"：失败 case 的具体内容（题面文字、期望值）不得出现在提案 content 中；
- 新规则必须表述为通用规则；
- 增益必须伴随同签名类其他 case 同向变化才可信；
- 禁止为通过单一 case 而硬编码针对性行为。

## 反例案例一：首笔晋升被用户裁决回滚（2026-09-13）

**经过**：R7 评测发现 agent 把产物文件写进仓库根 → 提案向 `task_dispatch_guide.md` 追加"产物落点守则"（教 agent 在 goal 里写明路径）→ 晋升 `eeb2a2614` → **用户裁决否决**，revert `dda03e426`，账本扣回。

**三重错误**：

1. **分诊错误**：根因是评测 harness 消息形态缺陷（创建类消息无工作空间锚定，`7f80fb980` 已修复）——属第一类"评测自身缺陷"，被误判为第三类"真改进空间"。教训：**分诊前先查该问题是否已被系统修复或机制覆盖**。
2. **把系统机制当缺失**：工作空间是系统机制（任务默认 `isolated` 自动分配；评估指标按 workspace glob 定位产物；指定工作空间走 `task_submit` 的 `workspace` 参数）。在 goal 文字里写路径是反模式，"守则"反而教坏 agent。
3. **未做机制前置检查**：material_registry.md「与系统机制的关系」一节因本案例新增——任何提案先过"机制是否已覆盖"检查。

**规则沉淀**：凡涉及"路径/工作空间/产物位置"类症状，先核对系统机制清单再决定是否分诊为改进空间。
