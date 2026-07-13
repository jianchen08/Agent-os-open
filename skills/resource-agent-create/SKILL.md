---
name: 创建Agent
description: 创建或修改 Agent 配置时加载。内置 Agent 配置字段规范 + 产出物模板创建规范 + 团队设计方法论（分层/自执行vs外包判定/提示词只留骨架/技能承载规范）。
---

# 创建Agent

## Agent 配置字段规范

### 必填字段

| 字段 | 类型 | 说明 | 规范 |
|------|------|------|------|
| config_id | string | 唯一标识 | snake_case，全局唯一，与文件名（去 .yaml）一致 |
| name | string | 名称 | 日志/显示/内部引用用 |
| description | string | 描述 | 说明职责、能力、适用场景，供上层搜索决策 |
| agent_type | enum | 功能类型 | main/orchestrator/specialized/atomic/system |
| category | enum | 功能分类 | analysis/evaluation/search/code/generation/resource/modification/task/system/others |
| level | enum | 级别 | L1(main)/L2(orchestrator)/L3(specialized/atomic) |
| system_prompt | string | 系统提示词 | 定义角色/职责/行为/流程（见下方"提示词写法"） |
| tool_ids | list | 工具列表 | ≤20 个直接加载；超出的放 static_vars 工具索引 |
| version | string | 版本号 | 语义化版本 |
| is_active | bool | 是否激活 | true/false |
| status | enum | 状态 | active/inactive/deprecated |
| max_iterations | int | 最大迭代 | L1=-1或100，L2=40，L3=100，atomic=10 |
| timeout_seconds | int | 超时秒 | L1=-1，快速=300，常规=600，复杂=1800 |
| tags | list | 标签 | 含层级标签(L1/L2/L3) |

### 可选字段

| 字段 | 说明 |
|------|------|
| display_name | UI 显示名，未设用 name |
| model_tier | large/medium/small；L1/L2 用 large，L3 用 small |
| model_name | 直接指定模型，优先级高于 model_tier |
| max_reminders | L1=10，L2=5，L3=3 |
| static_vars / dynamic_vars | 变量注入（见下方） |
| hard_constraints / soft_constraints | 约束（见下方） |
| input_schema / output_schema | JSON Schema |
| deliverables | 产出物定义 |
| recommended_metrics | 评估指标 |
| plugins | 插件覆盖 |

### agent_type 枚举

| 值 | 场景 |
|---|------|
| main | 主控，系统入口，接收需求/分配任务 |
| orchestrator | 编排，协调多个专业 Agent |
| specialized | 专业领域任务（最常用） |
| atomic | 单一原子任务（等价 specialized） |
| system | 系统级功能（错误处理/恢复/审计） |

## 提示词（system_prompt）写法

### 占位符变量（系统自动替换）

| 占位符 | 含义 |
|--------|------|
| {{workspace}} | 当前工作空间路径 |
| {{project_root}} | 项目根目录 |
| {{task_id}} | 当前任务 ID |
| {{path:文件路径}} | 注入文件内容 |
| {{rules}} | 注入硬约束和软约束 |

### 变量注入（static_vars / dynamic_vars）

| type | 作用 |
|------|------|
| rules | 注入约束 |
| path | 读取文件内容注入 |
| reference | 直接注入 content 文本 |
| timestamp | 时间戳 |
| retrieval | 按标签检索知识库 |

static_vars 会话级不变（缓存）；dynamic_vars 每轮重新生成。

### 工具数量阈值

- 总工具 ≤ 20 → 全部直接加载到 tool_ids
- 总工具 > 20 → 核心 ≤20 个进 tool_ids，其余进 static_vars 工具索引

## 团队设计方法论（创建 L2 编排团队时必遵循）

### 一、核心原则：节点数是 LLM 编排的头号敌人

多 agent 编排失败的主因不是单个 agent 干不好，而是编排层调度出错（路由判错、依赖排错、报告读错、回退失效）。每多一个节点，出错概率叠加。能合并的节点就合并，能砍掉的外包就砍掉。

### 二、自执行 vs 外包——只看"独立性"

唯一标准是独立性，不是能力、不是上下文、不是效率：

| 判定维度 | 自己执行 | 外包 |
|---------|---------|------|
| 独立性/中立性 | 无独立性需求 | 需要第二双眼睛，自己干=形同虚设 |
| 不污染上下文 | 产出是最终交付物 | 产出是大量需过滤的原始信息 |
| 可并行性 | 单一环节 | 能并行且数量多 |
| 失败隔离 | 低风险 | 高风险易失败 |
| 上下文连续性 | 和主线强耦合 | 和主线解耦 |
| 调度开销 | 小任务（开销>执行不值） | 大任务 |

**冲突优先级**：中立性/独立性（第一优先级，一票否决）> 不污染上下文+可审计 > 可并行/失败隔离/专业能力 > 连续性/调度开销。

### 三、四层分层（职责严格不重叠）

| 层 | 放什么 | 不放什么 |
|---|--------|---------|
| 常驻规则（注入） | 所有任务/agent 都要用的通用铁律 | 技术栈专属规范、特定场景流程 |
| 技能（按需加载） | 特定场景完整工作流/特定技术栈规范 | 通用铁律（引用常驻规则） |
| 派发（task_submit） | 外包独立 L3 | 自执行的事 |
| 定位（提示词正文） | 是谁、有哪些路径、每条路径加载什么/派发什么 | 规则细节、技能内部步骤 |

**铁律：按需的东西必须做成技能。** 配置文件提示词不注入就读不到，技能是唯一可靠的按需通道。在提示词写 `file_read xxx_rules.md` 是把通道接到死胡同。

### 四、提示词只写骨架

只该有：你是谁 + 核心职责（哪些自执行/派发，标清楚）+ 路径骨架（每步加载什么技能/派发什么）。

**多余类型（必须清除）**：动机解释、半引用半复述、技能内部步骤泄进提示词、复述自己配置、自执行读自己产出、重复的映射/边界表。

### 五、自执行与派发模型不混用

| 动作 | 自执行（加载技能） | 派发（外包 L3） |
|------|------------------|---------------|
| 要不要读报告 | 不要（自己产出自己知道） | 要（读下级产出） |
| 回归要不要 inherit | 不要（上下文常在） | 要（inherit pipe） |
| 措辞 | "加载 X 技能执行" | "派发给 X" |

## 约束写法

- hard_constraints 只写该 agent 特有的、system_prompt 没明确描述的约束
- 不重复 system_prompt 已有的规则
- 不重复 L3 标准约束（task_evaluate/评估通过才结束/必须输出产出物——由本规范统一说明）
- L2 有固定团队成员的加："提交任务时直接用映射表中的 Agent ID，禁止用 resource_search 搜索 Agent"

## 路径规范

- Agent 配置：`config/agents/<category>/<agent_id>.yaml`（扁平单文件，禁止文件夹结构）
- config_id 必须与文件名（去 .yaml）一致
- 工具代码：`src/tools/builtin/{tool_id}.py`
- 工具配置：`config/tools/`

## 产出物模板创建规范

当 Agent 配置有 deliverables（产出物）且需要配套模板时，按本规范创建。

### 三段式结构

模板分三段：

**第一段：头部说明**
```markdown
# {模板标题}

简述本模板用途和适用场景。
```

**第二段：正文章节**

用章节标注组织正文：[必填] / [可选] / [按需] / [条件必填]。字段说明格式：
```markdown
## 章节名 [必填/可选]

- **字段名**: 说明
```
章节间用 `---` 分隔。

**第三段：评估指南（可选）**
```markdown
## 评估指南 [可选]

### 检查维度
| 维度 | 通过标准 |

### 评估结论
- 通过：满足所有必填 + 关键维度达标
- 不通过：缺必填项或关键维度不达标
```

### 命名和存放

| 项目 | 规范 |
|------|------|
| 文件命名 | `{purpose}_template.md`（如 execution_report_template.md） |
| 存放位置 | `config/templates/` |
| 在 Agent YAML 引用 | deliverables 的 template_name 字段 |

### 填写示例

- 用 `{占位符}` 表示动态值（如 `{task_id}`、`{date}`）
- 示例要真实具体，不用"xxx""foobar"

### 创建流程

1. 确定模板类型（报告/配置/代码模板）
2. 确定用途和适用场景
3. 按三段式结构创建
4. 在 Agent YAML 的 deliverables 中引用（template_source: knowledge, template_name: 文件名）

## 验证清单

创建 Agent 后逐项核对：
- [ ] config_id 与文件名一致
- [ ] 必填字段齐全
- [ ] agent_type/category/level 匹配
- [ ] tool_ids 中的工具确实存在
- [ ] L2 的 team 字段 = system_prompt 中提到的成员 = 实际 L3 的 config_id 列表
- [ ] system_prompt 中引用的下级 Agent 名称与实际 config_id 拼写一致
- [ ] 同一 agent_id 没有出现在多个路径
- [ ] 配置符合本规范字段要求
