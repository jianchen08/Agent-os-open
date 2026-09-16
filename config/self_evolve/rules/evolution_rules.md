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
| budget_blowout | 终态+AC 全过但超 case 预算（rounds/tokens/seconds） | execution |
| unclassified | 其他 | — |

失败描述两级轴（归因分析用语，ADR 2026-09-16 采纳）：**一级=何处断开**
（observation/grounding → decision → action/arguments → environment/tool →
state update → verification → recovery → termination），**二级=为何断开**
（missing_info / ambiguity / hallucination / timeout / external_fault /
constraint / side_effect）。behavior_class 是主键，两级轴用于复盘深分析时
把签名翻译成可行动语言——禁止仅凭结尾文本归因。

## 四向分诊（防把一切失败当进化机会；外因单独成类）

0. **外部原因**（环境变了，不是系统的问题）：供应商超时窗口、内核重启/watcher
   重载窗口、注入故障标记、题集外部环境变更 → `proposal_reject` 带
   `category=external_cause` 记账，复跑 1 次确认；**把外因误判为自身问题就会
   乱改**，外因失败不计入杠杆命中率与学习率归因；
1. **评测自身缺陷**（断言口径错、工作空间污染、suite 配置错）→ 报告用户，
   不走提案（`category=harness_defect`）；
2. **被测系统故障**（插件 bug、内核断链——表现为任务 error/异常堆栈）→ 报告
   用户走修复流程，不走提案（`category=system_fault`）；
3. **真改进空间**（行为模式可由 A 部类物料改变）→ 进入杠杆映射，生成提案。

## 高判断力轨迹分析（条件触发，不强制全量）

**定位**：服务于归因分析，不是每个 case 的必选流程。仅当满足任一启用条件
才执行——简单任务、单一失败原因、能力短板明确的轨迹**跳过**（分层不是目的，
定位判断力失败才是；警惕"为完整性硬凑"）。

**启用条件**（满足任一）：
- 签名簇内多决策点，失败原因难以归因到单一能力短板；
- 怀疑 agent 分不清前提与建议、设计与提及、目标与偏移；
- 归因结果矛盾，或与最终通过率不一致；
- 需要评估"少即是多、敢舍弃"类表现。

**四步路径**：
1. **分层**：把轨迹关键步骤/决策/输出标为——地基（离了它任务不成立）/
   结构（重要但可替代）/ 装修（锦上添花）/ 填充（删掉无影响）；
2. **定锚**：对照该 case 的验收标准（成功标准锚），逐层判断是否服务于它；
3. **取舍**：对每个非地基项做消融提问"删掉它，后果是什么？"，后果严重程度
   必须与层级匹配——填充被当地基、或地基被当可选项，标记**判断力失败**
   （`proposal_reject` 带 `category=judgment_failure`）；
4. **自检**：自问有没有为报告完整而硬凑；轨迹本身"不缺"就敢说"不缺"。

**输出**：每步层级标签、目标对齐结论、瓶颈卡在哪一层（能力问题还是判断力
问题）。分析工具约束的是归因推理路径，不约束被测 agent 的行动空间。

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
