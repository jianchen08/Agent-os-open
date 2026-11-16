# scripts · 脚本面模块文档

## 需求

`scripts/` 是仓内全部机械检查 / 生成 / 运维脚本的统一落位：CI 门禁脚本（`check_*.py`）、
生成器（`gen_*.py` / `generate_*.py`）、统一门禁入口（`run_gates.py`）、运维工具
（备份 / 日志压缩 / 迁移）等。运行纪律：**门禁类脚本必须纯标准库**（CI 的
traceability-gate 等 job 只装 CPython，不装第三方依赖），需要第三方的走 `uv run`。

## 逻辑

- `run_gates.py` 是唯一门禁入口：`GATES` 列表声明一次，本地 `--mode` 与 CI `--filter`
  消费同一份定义（单一真值防第二口径）；fast 档成员由 `Gate.fast=True` 自动推导，
  fast 估时总和受 `FAST_BUDGET_SECONDS` 预算约束（元门禁 V5 执法）。
- `check_gate_wiring.py` 是元门禁：校验门禁接线元数据（wiring / 预算 / 账本位置 /
  死门禁），配套回归在 `tests/gates/test_gate_wiring.py`。
- 各 `check_*.py` 遵循同一形态：纯标准库、可独立 CLI 执行、失败退出非 0、
  配套回归测试在 `tests/gates/test_check_*.py`。

## 结构

- 门禁入口：`run_gates.py`（GATES / MODES / 并行执行器 / `--list --json`）
- 元门禁：`check_gate_wiring.py`
- 项目全景图组件：见 `graph_pipeline.md`（graph_model / graph_extract /
  graph_aggregate / graph_check 四件）

### 文件清单

| 文件 | 用途 | 创建时间 |
|------|------|----------|
| graph_model.py | 全景图数据模型：节点/边/意图 + frontmatter 与双链解析原语（纯函数，无副作用） | 2026-10-27 |
| graph_extract.py | 全景图代码半边/文档半边机械提取：按路径分派提取器，产出单文件事实 | 2026-10-28 |
| graph_aggregate.py | 全景图增量聚合 + 落盘 graph.js + 调校验（三合一入口，run_gates fast 档命令） | 2026-10-28 |
| graph_check.py | 全景图校验纯函数：frontmatter schema / 双链悬空 / status 矩阵（无独立 CLI） | 2026-10-28 |

关联测试（`tests/gates/`，该目录无独立模块文档，随本清单登记）：
`test_graph_model.py`（模型层行为）、`test_graph_pipeline.py`（提取→聚合→校验端到端）。


## 提交即闸与执法档位（方案 AI辅助编程体系落地AgentOS_20261022 §3.6 步骤 5，2026-10-28）

- 门禁入口：`run_gates.py`（本刀增：Gate.paths 守护路径字段 + `--changed-files` 变更模块过滤 + `--dry-run` 预演；新增 gate-protected / enforcement-tier 两门禁挂 fast 档，CI 接线于 ci.yml python-lint job filter）

| 文件 | 用途 | 创建时间 |
|------|------|----------|
| githooks/pre-commit | 现役提交即闸钩子（车道感知 CI 触发器，用户裁定 2026-10-30）：产物红闸 + 改动车道快检，委托 check_pre_commit.py。注意：受保护清单 --strict 执法**不在 pre-commit 链**（旧面 hooks/pre-commit 已于 2026-11-04 摘除）——gate-protected 闸在 run_gates fast/all 恒跑 + ci.yml python-lint filter | 2026-10-30 |
| install_hooks.py | core.hooksPath → scripts/githooks 幂等安装（--repo 可注入；工作区还原后一条命令重装） | 2026-10-28 |
| check_gate_protected.py | 机械闸受保护清单检查器（门禁护门禁；变更集三级回退；gate-change 标记 CI 层放行） | 2026-10-28 |
| check_rule_enforcement_tier.py | 规范执法档位检查器（三档执法位注册表 + 基线棘轮 + 防悬空引用；--init 出基线） | 2026-10-28 |

配套面（不在本目录）：`.github/gate-protected-paths.txt`（受保护清单）、
`config/rules/enforcement_tiers.yaml`（执法档位注册表）、
`.github/enforcement-tier-baseline.txt`（存量棘轮，只减不增）。

关联测试（`tests/gates/`）：`test_gate_protected.py`、`test_run_gates_changed_filter.py`、
`test_enforcement_tier.py`、`test_install_hooks.py`、`test_scaffold_project.py`（脚手架
生成物回归，脚本本体在 `skills/skill-dev-toolkit/scripts/scaffold_project.py`）。
