# 计划模式工作流（plan 态口径）

本段约束**计划模式下主 agent 的方案讨论段**（项目 `workflow_state = plan`）。
过门转 running 后本段不再适用，改按 `skills/skill-container-task-flow/SKILL.md`
执行任务链。职责分界一句话：**讨论与定稿自己执行，调研与执行外包**。

## 前置绑定（动笔前先做）

1. 确认 `project_id`（用户提供，或先 `project_create` 登记取得）；
2. 读项目登记确认 `workflow_state = plan`（running 态收到新需求 = 修订申请，
   见文末「修订」节，禁止直接改方案）；
3. 工作空间基准 = 本轮 state `project_roots` 注入的项目根；所有产物路径
   **一律相对项目根**书写（`.project/…`、`docs/…`），通知用户时 file_paths 同基准。

## 第零步：任务类型与领域判断

- **任务类型**：技术实现型（精确方案+实施计划）/ 精确创意型（方向明确+多方案比选）/
  开放探索型（广泛调研+收敛）/ 开放创意型（发散+聚焦验证）。在初步方案文档开头标注。
- **领域与 Skill 选择（三级）**：
  1. 内置命中：小说→`skills/skill-solution-novel/SKILL.md`；软件→`skills/skill-solution-software/SKILL.md`；
     游戏→`skills/skill-solution-game/SKILL.md`；调研→`skills/skill-solution-research/SKILL.md`；
  2. 未命中 → `resource_search(resource_type="skill", query="{领域关键词}")`，命中后 file_read 读 SKILL.md；
  3. 仍无 → human_interaction 请示用户确认方案框架。
- 选定后按该 Skill 的「方案内容框架」与「产出物拆分约定」组织方案与产物。

## 第一步：调研 + 初步方案

- 调研**外包** `research_agent`（task_submit 携 `project_id`；goal 写调研目标+范围；
  复杂调研可并行多方向；必须声明 `file_check`，路径 `docs/working/{title}_research_report.md`）；
- 读领域 Skill，按框架形成初步方案写入 `docs/{title}_solution.md`【自己执行】；
- human_interaction（conversation 模式）通知用户：文件路径 + 核心要点 2-3 句，
  **必须传 file_paths**。

## 第二步：讨论 + 细化（多轮，【自己执行】）

- 与用户多轮讨论；不确定的问题再派 research_agent 补充调研；
- 主动细化模糊部分（实施细节、量化指标、结构划分），细化到可直接生成任务链；
- 每次修改后通知用户（必须传 file_paths）；重复直至用户明确确认。

## 第三步：定稿三类产物 + 检查点 + 可视化【自己执行】

**产物拆三类，职责不重叠**（按领域 Skill 的拆分约定；非标注领域请示用户）：

1. `.project/` 项目文档（稳定真相源）：架构决策、API 契约、领域模型/业务规则、
   世界观设定、数值公式——方案阶段由你直接写入；平铺不分子目录；
   禁止写入模块清单、目录树、代码示例。
2. `docs/{title}_solution.md` 方案总纲（**只讲 why**）：背景与目标、调研摘要、
   关键决策理由、方案级 AC 总表（机读 YAML：id/title/must/category/verify_hint）、
   测试蓝图（软件类必填：用户旅程表+影响矩阵表，验证方式留空）、变更记录。
   禁止装具体设计/接口签名/实施步骤。
3. `docs/tasks/` 自包含任务文件：每任务一个 md，frontmatter 含
   `task_id/task_name/executor/depends_on/status/acceptance_criteria`（任务级 AC
   带 `traces_to: AC-{方案级编号}`，可二值判定）；正文含目标 / 执行方案与设计
   （接口契约级，拿到即可开工）/ 验收标准 / 备注。按内容边界打包，禁止按
   分析→设计→实现→测试工序拆；**最后一个任务固定为端到端验证+修复，执行者
   `container_verification_agent`**。
4. `docs/{title}_checkpoints.md` 检查点设计（自动通过 / Agent 互审 / 里程碑门控）。
5. 任务链可视化：经 resource_search 加载 bash_execute 后调用
   `python skills/skill-task-chain-viewer/scripts/generate_task_chain_html.py --title "{项目名}"`
   生成 `docs/working/{title}_task_chain.html`。**bash_execute 仅限此脚本**，
   禁止用于改码/测试/构建/git。

**定稿前自检清单**（全过才算定稿）：三类产物齐全且不重叠；AC 总表机读可二值；
每个任务级 AC 可 traces_to 方案级 AC；任务文件自包含；末位验证任务在；
任务链 HTML 生成成功。

## 过门（plan → running）

用户明确确认方案后：用 `project_state` 工具申请 `plan → running`
（触发用户审批，审批通过前禁止派发执行类子任务）。plan 态内只许派
调研（research_agent）、环境准备（environment_setup_agent）与经用户确认的
打样（产出仅作参考材料）。

## 修订边界

- **plan 态内**（过门前）：方案是你的工作稿，随讨论随时修改，定稿即冻结申请；
- **过门后**（running 态）：方案文档冻结，**禁止边跑边改**——修订分级
  （执行层修正 vs 方案层回门）与反向审批流程以
  `skills/skill-container-task-flow/SKILL.md`「四、方案不可变原则与修订分级」
  为唯一口径，此处不重复。
