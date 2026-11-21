---
doc_id: task-evaluate-module
type: module
status: current
---

# task_evaluate 模块文档

## 需求

任务评估闸门插件：评估裁决在插件侧完成，内核只落库（架构公理：评估闸门插件判定，
内核不判定）。任务收尾时对产出物按指标逐条评估，全部通过任务才 completed；
有评估证据内核才补落默认 completed，无证据 → failed（task_reminder 放行判定）。

- **tool 型指标**（evaluator_type=tool）：本地执行——按指标 params 在任务工作区
  真实检查（file_check 族 / bash_check / scope 一致性）。
- **agent 型指标**（evaluator_type=agent，如 semantic_check）：经 chat 派发评估
  子管道（评估者 agent 对照工作区产出判定），继承任务工作区。
- **验证证据独立**：验收测试与验证证据不得由实现方自产；签发的验收测试对实现方只读。
- **完赛交付证据（双轨）**（ADR `docs/decisions/2026-11-11-evidence-gate-dual-track.md`）：
  - **豁免轨**：任务声明 `acceptance_criteria.files`（task_submit 白话字段，存储为
    `file_check::<路径>` 实例键或字面 files 键）且工作空间非 worktree 模式 → 文件面
    替代 commit 面：清单取 `metadata.required_files` 与 acceptance files 并集（都声明
    都查），逐一核对文件存在且 mtime 晚于任务创建基线，commit 面整体跳过（零 git
    探测）。动因：无 bash 命令工具的 agent（novel/research/orchestrator 家族）执行
    不了 git commit，commit 面恒查 = 结构性死锁；回执按闸分流，只给文件面补救提示。
  - **既有轨**：未声明 acceptance files 或 worktree 挂靠项目任务（交付语义 = commit
    后合并回项目仓库，不豁免）→ 基线后须有新 commit；无 git 记录 → 拒绝完赛
    （result=retry，回执要求 agent 先 `git commit` 再完赛）。声明 `metadata.required_files`
    的任务另加文件面 AND 校验，两面共享同一次 git 探测。
  - 取证环境不可用（工作空间坐标/创建基线缺失、既有轨下非 git 仓、git 不可用或超时）
    降级放行并 warning 留痕（豁免轨纯文件面，无 git 降级形态）。

## 逻辑

- **指标定义单源**：`config/plugins/evaluation/evaluation_metrics.yaml`
  （evaluation_service 插件 config_files 声明的唯一真相源）。执行器经
  `_libs.evaluation_metrics_path.probe_metrics_config_path` 定位（三布局单算法），
  读失败 → 指标未定义诚实失败，不猜测语义。
- **执行分流**（PipelineEvaluationExecutor）：
  - params 含 `command` → bash_check（bash -c 真执行，退出码 0=通过）；
  - params 含 `allowed_paths` → scope 一致性检查（改动文件清单 ⊆ 改动面绑定表）；
  - 否则 → file_check 族（check 枚举：exists / is_directory / not_empty / contains /
    matches_schema）。
  - 白话展开多实例键（`file_check::<路径>`，task_submit 生成）：首段即定义 ID。
- **matches_schema**：产物文件（YAML/JSON）结构校验——schema 必填字段存在且非空、
  字段类型、数组最小长度（min_items）；错误信息聚合列出全部不合规点（观察期误报
  归因需要完整信息）。
- **scope 一致性（scope_consistency）**：`git status --porcelain -uall` 解析任务
  工作区改动文件清单（含未跟踪），对比 params.allowed_paths（文件精确或目录前缀
  匹配），越界清单进 message。假设 workspace = git 仓库/worktree 根（任务默认
  隔离执行语义下成立）。allowed_paths 缺失 → 诚实失败（不放行）。

## 结构

### 文件清单

| 文件 | 用途 | 创建时间 |
|------|------|---------|
| plugin.json | 插件清单（task_evaluate 工具声明） | 2026-08 前 |
| server.py | 插件入口（工具注册/服务） | 2026-08 前 |
| tool.py | task_evaluate 工具实现（评估入口/审批交互） | 2026-08 前 |
| _eval_core.py | 评估核心类型面（EvaluationResult/MetricResult/路径脱敏） | 2026-08 前 |
| _executor.py | 评估执行器（指标装配/tool 本地执行/agent 派发） | 2026-08 前 |
| worktree_merge.py | worktree 合并自愈 | 2026-09 前 |
| bounded/工作区辅助 | 见各文件头注释 | — |
| test_eval_core_executor.py | 评估核心与执行器补充单测（既有） | 2026-09 前 |
| test_l0_executor.py | L0 检查规则执行面单测（matches_schema/scope 一致性） | 2026-10-28 |

### L0 检查规则（2026-10-28 落地，观察模式）

三条 L0 通用指标定义于 evaluation_metrics.yaml，由本模块执行器承载 tool 型执行：

| 指标 id | 检查内容 | 执行路径 |
|---------|---------|---------|
| artifact_schema_check | 执行报告存在且结构合法、AC 清单非空 | file_check 族 + matches_schema |
| antipattern_scan | 空 catch、调试残留、硬编码密钥、无需求 TODO | bash_check（命令组合） |
| scope_consistency | 改动文件清单 ⊆ 任务书改动面绑定表 | scope 一致性检查 |

观察模式（gate_mode=false）语义由 mode 声明面 `ci:` 段承载（如
plugins/shared/mode_coding/mode.yaml）；编排链自动挂清单的消费机制属方案后续步骤
（AI辅助编程体系落地AgentOS方案 §3.3 编排链插门禁段），本期任务评估引用指标 id
即真实执行。

设计真值：`docs/working/重要设计/AI辅助编程体系落地AgentOS方案_20261022.md` §3.2；
ADR `docs/decisions/2026-10-27-ai-coding-system-agentos-landing.md`。
