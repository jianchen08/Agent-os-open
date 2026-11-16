# mode_research 研究模式包

对标 Perplexity/Elicit 范式：问题 → 带引用报告。声明面 = `mode.yaml`；
设计真值 `docs/working/模式包工作模式设计_20260928.md`（D0–D10）。

## 包结构

```
mode_research/
├── mode.yaml          # 声明面（chain/suite/material_scope/levers/plan_spec）
├── server.py          # 面板读数桥 + 写动作（describe/get_profile/研报台端点）
├── skills/            # 包内技能（skill-solution-research，plan_spec 按名消费）
├── rules/             # 口径物料（context_build 模式段）
└── webview/
```

## 编排链（实际行为，照实声明）

```
main ──简单调研──▶ research_agent（config/agents/executor/generation，系统级执行者）
     └─复杂研究──▶ research_orchestrator_agent（五阶段状态机，config/agents/orchestrator）
                        └─信息收集/补充检索──▶ research_agent
```

模式私有执行者池（三工位检索/摘要/报告）已按 ADR 2026-10-09 决策 2 随执行者池机制
全模式退役删除；编排链复用系统级执行者，领域再需专属执行者按四判据重新立项。

规则/模板正文一律经 `{{path:}}` 引用既有文件（research_domain_rules.md /
research_report_template.md 等），不复制；技能 skill-solution-research 经
mode.yaml `plan_spec` 注册（方案段骨架第零步按名查注册表）。

## 工具面注意

- ⛔ 四个外部 MCP 搜索源（mcp_registry_search / smithery_search /
  langchain_hub_search / external_resource_search）已于 2026-09-13 退役删除，
  新配置禁止声明（ADR 2026-09-13-orphan-mcp-search-plugins-removed）。
- ⚠️ enhanced_search 全仓扫描分钟级，仅限定目录浅层检索使用（编排链执行者
  提示词与约束内已标注）。
