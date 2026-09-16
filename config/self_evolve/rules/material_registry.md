# 物料登记表（material_registry）—— A 部类可改面的权威清单

> B 部类物料（governance）：只允许用户修改。
> 本文件是 §11.1 登记表的文件化初版；`eval_harness/proposal.py` 的
> `EVOLVABLE_PREFIXES` / `FROZEN_PREFIXES` 与本表保持同步（改此表必须同刀改代码与测试）。

## A 部类（evolve，进化 agent 可提案修改）

| 前缀 | 层 | mutability | 说明 |
|---|---|---|---|
| `config/agents/*/persona/**` | L1 | content_edit | 各 agent persona |
| `config/rules/**`（安全类除外，见冻结清单） | L1 | content_edit | 提示词骨架引用的业务规则文件（dispatcher_rules_exec 等） |
| `config/agents/main/task_dispatch_guide.md` | L1 | content_edit | 派发指南 |
| `config/agents/*/*.yaml` | L2 | param_edit / list_edit | tool_ids、default_params、thinking 等参数面（身份字段冻结，见下） |
| `config/pipelines/*.yaml` | L2 | param_edit | 步骤参数 / when 条件（编排结构调整需人工） |
| `plugins/shared/**/plugin.json` | L2 | param_edit | manifest `fields` 阈值参数（id/entry/capabilities 结构冻结；evaluation/review 插件除外——见 B 部类） |
| `plugins/shared/{tools,pipeline}/auto_gen_**` | L3 | new_plugin | 进化新增插件专用命名空间 |

> 勘误（ADR 2026-09-16）：评估面物料（`config/plugins/evaluation/**`，含
> evaluation_metrics.yaml）已整体移入 B 部类——阈值参数直接改变判定松紧
> （改阈值=改裁判），「参数可调、口径冻结」的折中被否，见各 ADR Alternatives。

### agent yaml 内部冻结段（文件可改，字段冻结）

`config_id`、`name`、`display_name`、`agent_type`、`level`——身份与层级字段不可经提案修改。

## B 部类（governance，冻结，仅用户可改）

| 范围 | 理由 |
|---|---|
| `config/self_evolve/**` | 进化流程自身物料（规则/题集；模式 profile 已内打包进模式插件） |
| `plugins/shared/system/eval_harness/**` | 评测面本体（裁决权载体） |
| `plugins/shared/system/evaluation/**` | 评估闸门插件本体（裁决权载体，ADR 2026-09-16 新增） |
| `config/plugins/evaluation/**` | 评估指标配置（阈值=判定松紧，被裁决者不可改，ADR 2026-09-16 新增） |
| `plugins/shared/system/review/**` | 复盘/改进建议插件本体（归因原则承载地，ADR 2026-09-16 新增） |
| `config/plugins/review/**` | 分诊与杠杆规则（triage_rules.yaml，ADR 2026-09-16 新增） |
| `plugins/shared/modes/**` | 模式插件出厂种子源（profile 随插件目录内打包，种子单元自包含） |
| `<USER_ROOT>/plugins/modes/**` | 模式插件用户副本（预装播种后归用户所有，同 id 用户赢；对进化 agent 同样冻结） |
| `kernel/**` | 内核（架构公理） |
| `.env`、`config/kernel/storage.yaml`、`config/isolation/**` | 运行底座与安全边界 |
| `config/rules/information_integrity_rules.md` | 信息完整性红线（main 硬约束引用） |
| `config/rules/*security*.md` | 安全类规则 |
| held-out 与 staging 受控目录（仓库外） | 裁决证据与写面 |

## 与系统机制的关系（分诊前置检查）

提出任何提案前先核对：该问题是否**系统机制已覆盖**？已知机制包括——
任务工作空间（默认 `isolated` 自动分配）、产物定位（评估指标按 workspace glob）、
工作空间指定（`task_submit` 的 `workspace` 参数）、工具面（agent yaml `tool_ids`）、
熔断（内核词表）。凡机制已覆盖的需求，提案一律不成立，走报告通道。

> 反例教材：首笔晋升 eeb2a2614（产物落点守则）即违反本节——见 evolution_rules.md 反例案例一。
