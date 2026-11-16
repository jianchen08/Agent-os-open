---
doc_id: mode-coding-module
type: module
status: current
---

# mode_coding 模块文档

## 需求

编码模式声明面包（模式包）：mode.yaml 为单源声明面，经 `mode.get_profile` 服务
透出完整 dict。承载：域路由知识（description）、编排链（chain）、自进化 suite、
物料范围（material_scope）、调参杠杆（levers）、验证器族（verifier_families）、
预算（budget）。

包内物料：agents/（团队声明）、skills/（域技能，随工作空间并集快照按名可读）、
rules/、templates/、webview/。

## 逻辑

- mode.yaml 由用户维护出厂种子；包内 mode.yaml 为缺省本包种子（fail-closed
  实现在 _libs 基面）。
- `ci:` 段（2026-10-28 新增）：任务级门禁声明——L0 通用指标清单 + 观察模式开关。
  与 `verifier_families` 分工：verifier_families 服务自进化 suite 回归；`ci:` 是
  任务级门禁声明（方案 §3.2 裁定，二期二者收敛为单一声明）。
- 观察模式（gate_mode=false）：L0 指标执行结果只记录不阻塞，收集误报率数据；
  数据支撑后另行评估切 blocking（ADR 2026-10-27 步骤 6）。

## 结构

### 文件清单

| 文件 | 用途 | 创建时间 |
|------|------|---------|
| plugin.json | 插件清单 | 2026-09 前 |
| mode.yaml | 编码模式声明面（2026-10-28 增 ci: 段：L0 评估清单观察模式） | 2026-09 前 |
| server.py | 插件入口（mode.get_profile 等服务） | 2026-09 前 |
| test_agent_tool_ids.py | 团队工具面白名单测试 | 2026-09 前 |
| agents/ skills/ rules/ templates/ webview/ | 包内物料目录 | 2026-09 前 |

### ci: 段（2026-10-28 新增）

```yaml
ci:
  base: [artifact_schema_check, antipattern_scan, scope_consistency]  # L0 通用层，引用指标 id
  gate_mode: false  # 观察模式：只记录不阻塞，收误报率
```

- base 引用的指标 id 必须存在于 `config/plugins/evaluation/evaluation_metrics.yaml`
  （防漂移测试：tests/plugins/shared/test_l0_metrics_declaration.py）。
- domain 段（L1 域门禁 lint/typecheck/test_lane_smoke）待 L1 落位后补，防悬空引用。

设计真值：`docs/working/重要设计/AI辅助编程体系落地AgentOS方案_20261022.md` §3.2；
ADR `docs/decisions/2026-10-27-ai-coding-system-agentos-landing.md`。
