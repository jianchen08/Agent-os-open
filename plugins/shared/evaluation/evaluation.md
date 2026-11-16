---
doc_id: evaluation-module
type: module
status: current
---

# evaluation 插件模块文档

## 需求

评估指标注册表 + metrics HTTP 读面：前端 `/ext/evaluation_service/metrics` 读写
指标定义（内置只读），`evaluation.run` 维持诚实 stub 语义（执行面权威 =
task_evaluate 插件的 PipelineEvaluationExecutor，本插件不做指标执行）。

## 逻辑

- **配置单源**：`config/plugins/evaluation/evaluation_metrics.yaml`（manifest
  config_files 声明；server.py `_METRICS_YAML` 与之一致）。读失败 → 5xx 错误
  信封；内容为空 → 真实空注册表。
- **注册表消费**：`_load_metric_registry`（name → params_schema/required）与
  task_submit 提交期校验消费同一份定义，杜绝双源口径漂移。
- 指标定义字段：name/description/category/evaluator_type/evaluator_id/level/
  includes/requires/input_schema/tags/default_config。

## 结构

### 文件清单

| 文件 | 用途 | 创建时间 |
|------|------|---------|
| plugin.json | 插件清单（http_endpoints 声明 + config_files） | 2026-08 前 |
| server.py | 插件入口（metrics HTTP 读面 + evaluation.run 诚实 stub） | 2026-08 前 |
| bounded_dict.py | 有界字典（评估结果存储 TTL/上限） | 2026-09 前 |
| test_bounded_dict.py / test_evaluation_server*.py | 单测 | 2026-09 前 |

### 指标配置（config/plugins/evaluation/evaluation_metrics.yaml）

内置指标：file_check / semantic_check / bash_check / human_review / format_valid。

**L0 通用层三条检查规则**（2026-10-28 新增，观察模式；ADR 2026-10-27 步骤 2）：

| 指标 id | 类型 | 检查内容 | 执行路径 |
|---------|------|---------|---------|
| artifact_schema_check | tool | 执行报告存在且结构合法、AC 清单非空 | file_check 族 + `matches_schema` 检查类型（本刀新增枚举值） |
| antipattern_scan | tool | 空 catch、调试残留（print/console.log）、硬编码密钥、无需求 TODO | bash_check 命令组合（default_config 载观察期标准扫描命令） |
| scope_consistency | tool | `git status` 改动文件清单 ⊆ 任务书改动面绑定表 | params.allowed_paths（改动面绑定表），task_evaluate 执行器内置 git 对比 |

启用面：mode 声明 `ci:` 段（如 plugins/shared/mode_coding/mode.yaml），
`gate_mode: false` 观察模式收集误报率，数据支撑后另行评估切 blocking。

设计真值：`docs/working/重要设计/AI辅助编程体系落地AgentOS方案_20261022.md` §3.2。
