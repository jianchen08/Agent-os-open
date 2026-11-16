# 物料登记表（material_registry）—— A 部类可改面的权威清单

> B 部类物料（governance）：只允许用户修改。
> 本文件是 §11.1 登记表的文件化版；`plugins/shared/mode_evolution/proposal.py` 的
> `EVOLVABLE_PREFIXES` / `FROZEN_PREFIXES` 与本表保持同步（改此表必须同刀改代码与测试）。

## A 部类（evolve，进化 agent 可提案修改）

| 前缀 | 层 | mutability | 说明 |
|---|---|---|---|
| `config/agents/*/persona/**` | L1 | content_edit | 各 agent persona |
| `config/rules/**`（B 部类冻结清单所列文件除外） | L1 | content_edit | 提示词骨架引用的业务规则文件（dispatcher_rules_exec 等） |
| `config/agents/main/task_dispatch_guide.md` | L1 | content_edit | 派发指南 |
| `config/agents/*/*.yaml` | L2 | param_edit / list_edit | tool_ids、default_params、thinking 等参数面（身份字段冻结，见下） |
| `config/pipelines/*.yaml` | L2 | param_edit | 步骤参数 / when 条件（编排结构调整需人工） |
| `skills/**` | L1 | content_edit | 全局技能物料（运行期唯一写路径 = 本提案管道，提案必过 skill_audit 安检） |
| `plugins/shared/auto_gen_*` | L3 | new_plugin | 进化新增插件专用命名空间前缀（平铺布局，无桶层） |
| `plugins/shared/mode_roleplay/{agents,lorebooks,packs}/*.yaml` | L3 | content_edit | RP 出厂内容面（提案必过 content_audit 安检；三目录内非 .yaml 文件照冻结） |

> 勘误（ADR 2026-09-16）：评估面物料（`config/plugins/evaluation/**`，含
> evaluation_metrics.yaml）已整体移入 B 部类——阈值参数直接改变判定松紧
> （改阈值=改裁判），「参数可调、口径冻结」的折中被否，见各 ADR Alternatives。

### agent yaml 内部冻结段（文件可改，字段冻结）

`config_id`、`name`、`display_name`、`agent_type`、`level`——身份与层级字段不可经提案修改。

## B 部类（governance，冻结，仅用户可改）

| 范围 | 理由 |
|---|---|
| `config/self_evolve/` | 进化流程自身物料（规则/题集；模式 profile 已内打包进模式插件） |
| `plugins/shared/mode_`（mode_roleplay 三内容目录的 .yaml 除外，见 A 部类） | 全部模式插件本体：含评测面 mode_evolution（裁决权载体）与各模式出厂种子（profile 随插件目录内打包，种子单元自包含） |
| `plugins/shared/evaluation/` | 评估闸门插件本体（裁决权载体，ADR 2026-09-16） |
| `config/plugins/evaluation/` | 评估指标配置（阈值=判定松紧，被裁决者不可改，ADR 2026-09-16） |
| `plugins/shared/review/` | 复盘/改进建议插件本体（归因原则承载地，ADR 2026-09-16） |
| `config/plugins/review/` | 分诊与杠杆规则（triage_rules.yaml） |
| `kernel/` | 内核（架构公理） |
| `.env`、`config/kernel/` | 运行底座（含存储配置） |
| `config/plugins/isolation/` | 隔离安全边界 |
| `config/rules/information_integrity_rules.md` | 信息完整性红线（main 硬约束引用） |
| `config/rules/document_context_rules.md` | 文档上下文常驻规则 |
| `tests/eval_bench/` | 评测基线 |
| 仓库外路径（用户空间插件副本、held-out 与 staging 受控目录） | 非合法 target：逃逸检查一票拒绝（target 解析后必须落在项目根内） |

## 与系统机制的关系（分诊前置检查）

提出任何提案前先核对：该问题是否**系统机制已覆盖**？已知机制包括——
任务工作空间（默认 `isolated` 自动分配）、产物定位（评估指标按 workspace glob）、
工作空间指定（`task_submit` 的 `workspace` 参数）、工具面（agent yaml `tool_ids`）、
熔断（内核词表）。凡机制已覆盖的需求，提案一律不成立，走报告通道。

> 违反本节的典型形态：就机制已覆盖的问题提出提案（产物落点类守则即属此类）——案例归档见 evolution_rules.md 反例案例一。
