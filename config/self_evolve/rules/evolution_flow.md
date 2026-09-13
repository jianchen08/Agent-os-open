# 自进化规则文件 —— 内环流程骨架（evolution_flow）

> B 部类物料（governance）：只允许用户修改，进化 agent 只读。

## 与现有 system 族 agent 的分工（不重叠裁定）

- `evaluator_agent`：任务内评估（task_evaluate，按 evaluation_metrics 对单个任务产出判定）——**评估闸门**，已存在；
- `evolution_agent`（本文件约束的对象）：**只负责物料更改事务**——跑题集、归因签名、生成与提交提案、按裁决晋升或回滚。它不执行业务任务、不做任务评估、不改评测面自身；
- 两者数据链路：evaluator 的判定结果（settled/任务终态）是 evolution 归因的输入之一，但 evolution 对判定口径（evaluation_metrics.yaml 的指标定义）只有报告权，没有修改权。

## 一轮循环 = 模式 × 七步

每轮只处理一个任务模式（mode）。**执行全部踩在系统既有机制上**：评测 case = 任务（task_submit 派发，默认 isolated 自动获得工作空间）；判定 = 验收标准（acceptance_criteria 内置指标，评估闸门收尾自动判定）；生效 = G2 + 热重载原生接管；复盘与改进建议 = 复盘插件 review_service。本插件只做题集展开、聚合与提案校验，不自建派发/断言/状态/变更管理。

### T1 测量
1. `eval_view_suite(suite=<该模式题集>)` 拿批次清单；
2. **逐条用自己的 task_submit 派发**（参数照清单，不自建派发通道）；
3. `task_manage` 查各任务终态与验收结果；
4. `eval_summarize(mode, results)` 聚合出 scorecard 与失败分类。
先查 `eval_health`：提案账连续失败时谨慎推进（熔断/预算红线见 T8）。

### T2 归因
调 `review_improvement`（复盘插件的改进建议面）——把 T1 的失败面（case_id/task_status/criteria/症状与轨迹摘要）喂给它，产出结构化建议（机制前置检查 / 三向分诊 / 杠杆映射），原则规则在 `config/self_evolve/rules/triage_rules.yaml`（B 部类）。
基于复盘建议：聚类（同因 ≥2 case 才立项，单 case 记噪声）→ 取 improvement_space 类 → 可证伪假设（改 X 后失败簇 S 消失且其余簇不变）。

### T3 提案
选最高优先级假设 → `file_read` 目标物料（主仓只读）→ 构造 ChangeProposal（见 proposal_contract.md）→ `proposal_submit`。
被拒：按违规清单修正后重提；同 target 被拒 2 次必须换假设。

### T4 试跑
以提案后的物料重新走 T1（题集复跑对照；staging 生效链路接通前口径为"基线复跑对照"，聚合时注明）。
预期证伪检查：假设预测的失败面变化 vs 实际。未复现 → `proposal_reject`（reason=预测失准），回 T3。

### T5 裁决
试跑通过阈值 → `eval_run_heldout(mode)`（隐藏集保密派发，题面不进你的上下文）→ `task_manage` 查终态 → `eval_summarize` 聚合。`delta < 0` 或全败 → `proposal_reject`（reason=heldout_gap），回 T3。

### T6 应用
`proposal_apply(proposal_id, message=归因说明)`——content 写主仓目标文件 + pathspec commit，**配置/插件变更由内核 G2 校验与热重载原生接管生效**（不自行重启、不自行 git 操作）。
L3 插件类提案需人工审：先经复盘插件（review_service）附提案发起人工审，批准后再 apply，等待期间不提交新提案。

### T7 记忆回填
调 `memory` 工具沉淀本轮经验（Summary 形态：哪类签名 + 哪个杠杆 + 效果如何）。杠杆命中率由服务侧自动回填。

### T8 循环决策
- 预算内仍有假设 → 回 T3；
- 假设耗尽 → 回 T1 重新测量归因；
- 轮次预算耗尽（每模式每会话 ≤3 轮提案尝试）→ `eval_health_report` 收尾报告，结束。

## 全谱分支速查

| 情形 | 处置 |
|---|---|
| 试跑未达阈值 | reject（未证实）→ 回 T3 |
| 预测失准 | reject → 回填命中率 → 回 T3 |
| held-out 不过 | reject + 标记 heldout_gap → 回 T3 |
| 冒烟红 | promote 被拒 → 回 T3 |
| 熔断 tripped | 停止提案，输出收尾报告 |
| 红线 | 立即停止一切动作 |
