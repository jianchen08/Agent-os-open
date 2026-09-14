# research 模式包 · skill 与规则引用清单

> 薄整合层：仅引用，不复制内容；单一真值在既有位置。

---

## 一、技能（Skill）

| 技能 | 路径 | 用途 | 加载时机 | 来源 |
|------|------|------|----------|------|
| `skill-solution-research` | `skills/skill-solution-research/SKILL.md` | 研究方案规划：调研框架设计、方法论选择、分析维度定义、报告结构规划、产出物拆分约定 | 调研框架/方案规划阶段（检索工位按需加载；上游编排层规划时加载） | [来源: skills/skill-solution-research/SKILL.md 文件头] |
| `skill-container-task-flow` | `skills/skill-container-task-flow/SKILL.md` | 项目任务链流程（项目挂靠/workspace 决策/进度触发器/方案不可变） | 上游 L1 组织项目任务链时（非本包角色直接加载，列此备查） | [来源: skills/skill-container-task-flow/SKILL.md 文件头] |

> 说明：`.zcode/skills/` 为本地镜像目录（`.gitignore` 整体忽略，不入库）——技能以 `skills/` 为准 [来源: .gitignore「IDE / Agent 本地工作区」节]。

## 二、规则文档（3 角色经 `{{path:}}` 注入，不复制）

| 规则 | 路径 | 用途 |
|------|------|------|
| `information_integrity_rules.md` | `config/rules/information_integrity_rules.md` | 信息诚实原则（来源标注/达成度如实/失败记录） |
| `document_context_rules.md` | `config/rules/document_context_rules.md` | 上下文读取与文档规范 |
| `research_domain_rules.md` | `config/rules/per_domain/research_domain_rules.md` | 调研领域规则（信源分级/交叉验证/收集-写入循环/结论-来源绑定） |
| `execution_review_rules.md` | `config/rules/execution_review_rules.md` | 执行回顾流程（dynamic_vars 注入） |

## 三、模板

| 模板 | 路径 | 用途 |
|------|------|------|
| `research_report_template.md` | `config/templates/research_report_template.md` | 报告章节结构（报告工位 static_vars 引入） |

## 四、既有 Agent 层（引用，不复制、不改动）

| Agent | 路径 | 关系 |
|-------|------|------|
| `research_orchestrator_agent` | `config/agents/orchestrator/research_orchestrator_agent.yaml` | 既有 L2 编排者（五阶段状态机 + 闭环补充检索）；本包 3 工位为其下游执行面候选 |
| `research_agent` | `config/agents/executor/generation/research_agent.yaml` | 既有 L3 单体调研（轻量单工位场景保留） |

## 五、评测侧引用（备查，不改动）

| 对象 | 路径 | 说明 |
|------|------|------|
| research 题集 | `config/self_evolve/suites/dev/research.yaml` | 评测题集（mode: research，target: research_orchestrator_agent）；本包未改动 [来源: 该文件] |
| coding 模式 profile | `config/self_evolve/modes/coding.yaml` | 评测 harness 的模式 profile 先例（B 部类物料，由用户维护）；research 侧 profile 未建，属评测面事项，不在本包范围 [来源: 该文件头注] |
