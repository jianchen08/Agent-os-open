---
name: experience-ops
description: 从 AgentOS 历史 traces 与复盘报告构建经验轨迹数据集、校验质量闸、聚合学习曲线报告。当需要生成/校验经验集、把复盘 step_credits 对齐到 trace、或汇总 campaign 结果时使用；不用于在线检索或任务派发。
---

# 经验数据运维

## 用途

把「任务执行 traces + 复盘报告」转成 `agentos.experience.v1` episode，做质量校验，并聚合学习曲线评测报告。

## 何时使用

- 需要从历史任务生成经验轨迹集；
- 需要校验经验集的 schema / 复盘覆盖 / 去重 / 泄漏 / 校准；
- 需要把复盘报告的 `step_credits` 对齐到 trace 步骤；
- 需要把 campaign 的逐 trial 结果聚合成 Avg / Final / Δ / slope / cost。

## 不适用

- 在线任务执行中的实时记忆检索 → 用 `memory` 工具；
- 触发复盘 → 用主 agent 的 `review_ops`；
- 派发评测任务 → 用主 agent 的 `task_submit`；
- 经验入库 → P1 才接 hindsight（当前 `ingest` 显式失败）。

## 前置

- Python 3.11+，仅标准库；
- 读库经 `scripts/kernel_db_query.py`（临时快照），禁止直连活库；
- 默认固定执行空间 `reports/eval/experience_ops/`，可用 `--root` 或 `AGENTOS_EXPERIENCE_OPS_DIR` 覆盖；
- 复盘报告本体在被复盘任务的工作空间 `docs/working/reviews/{review_id}.json`，只读引用。

## 输入 / 输出

输入：
- task_id 清单（DB 模式可省略，按 `--since` 全量筛）；
- 复盘报告（`step_credits`）；
- 可选 `--from-json` bundle 文件（测试/离线导入）。

输出（`--root` 下）：
- `datasets/<suite>/<run_id>/raw.jsonl`：原始 bundle
- `datasets/<suite>/<run_id>/normalized.jsonl`：credit 暂空
- `datasets/<suite>/<run_id>/episodes.jsonl`：最终经验集
- `datasets/<suite>/<run_id>/manifest.json`：质量闸与计数
- `datasets/<suite>/latest`：最新 run 指针
- `campaigns/<run_id>/report.{json,md}`：学习曲线报告
- `datasets/<suite>/<run_id>/offline_auc.json`：留一验证结果

## 流程

1. 确认执行空间：`OPS="${AGENTOS_EXPERIENCE_OPS_DIR:-reports/eval/experience_ops}"`。
2. 构建：`python skills/experience-ops/scripts/experience_build.py build --root "$OPS" --suite <suite> [--since YYYY-MM-DD] [--from-json <file>]`。
3. 看 `manifest.json` 的 `quality_gates`；未全绿按失败分支处理。
4. 离线验证：`python skills/experience-ops/scripts/learning_curve.py offline --root "$OPS" --suite <suite> [--min-auc 0.6]`。
5. 聚合 campaign：`python skills/experience-ops/scripts/learning_curve.py report --root "$OPS" --run <run_id>`。
6. 完成判据：`manifest.json` 中 `quality_gates.all_passed=true`，且 `offline` 的 AUC ≥ 0.6（弱信号即可）。

## 脚本

- `scripts/experience_build.py`：`harvest / normalize / credit / validate / build / ingest`；`--help` 看参数。
- `scripts/learning_curve.py`：`report` 聚合、`offline` 留一 AUC；不派发任务。
- `scripts/experience_lib.py`：纯函数库（归一/回报/质量闸/AUC），不直接运行。

## 失败分支

- 复盘缺失或未完成：该 episode 的 `credit.review_score=null`，不阻塞其他 episode；覆盖率闸会失败。
- schema 非法：`validate` 退出码 2，输出 `schema_errors`；按字段路径修。
- 库读取失败：`kernel_db_query.py` 非零退出；改用只读 db 端点或 `--from-json`。
- 输出过大：脚本只写摘要计数到 stdout，明细在文件。
- `ingest`：退出码 4，提示 P1 未实现；不要把它当成功。

## 验证

- `build` 退出码 0 且 `quality_gates.all_passed=true`；
- `offline` AUC ≥ 0.6（有正负样本时）；
- 抽样 20 条 episode 人审：动作归一准确率 ≥90%，负分步 `root_cause` 可操作率 ≥80%；
- 单元测试：`pytest skills/experience-ops/tests/ -q`。

## Gotchas

- trace 只保留 `tool_results` 摘要（`count/chars/tool_name`），完整结果不在 traces；`observation.digest` 只做摘要，不回查全文。
- 复盘用 `iteration` 取证，经验集用 `step_ref`（`trace:<seq>:<call_index>`）对齐；两者不可混用。
- `reports/*` 已被 gitignore；数据集不要写进 `docs/`。
- 失败 episode 的最后一步 -10 只认复盘报告里的分；脚本不伪造。
- 同一 case 的多次 trial 必须用全新工作空间；经验 bank 共享，环境状态不共享。

## 参考

- `config/self_evolve/schemas/experience_v1.schema.json`：episode JSON Schema。
- `config/self_evolve/suites/dev/learning_curve_example.yaml`：题集 v3 结构示例。
- `docs/working/JitRL迁移_经验轨迹数据集与评测集_方案_20261113.md`：设计真值。
