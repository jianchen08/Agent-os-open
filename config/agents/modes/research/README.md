# research 模式包（调研分析团队：检索 + 摘要 + 报告）

> 状态：v1（2026-09-13，批次 R5v2 交付）
> 路线图出处：0.4.0「预置技能包与 Agent 团队」——调研分析团队（检索 + 摘要 + 报告）[来源: ROADMAP.md 0.4.0 节]
> 形态：**配置目录形态 + 清单文档**（薄整合层：引用既有配置，不复制；提示词只留骨架；tool_ids 按实况）
> 任务简报：[来源: docs/working/batch_20260913/R5v2_research_modepack.md]

---

## 一、包结构

```
config/agents/modes/research/
├── README.md                       # 本文件（包说明：分工/调用链/启用步骤/决策升级）
├── TOOL_SURFACE.md                 # 检索/摘要工具面清单（含健康标注）
├── SKILL_REFS.md                   # skill 与规则引用清单
├── research_retrieval_agent.yaml   # 角色① 检索工位（L3）
├── research_summary_agent.yaml     # 角色② 摘要工位（L3）
└── research_report_agent.yaml      # 角色③ 报告工位（L3）
```

外部引用（**不复制进包**，单一真值在既有位置）：

| 引用对象 | 路径 | 用途 |
|----------|------|------|
| 调研领域规则 | `config/rules/per_domain/research_domain_rules.md` | 信源分级/交叉验证/收集-写入循环（3 角色经 `{{path:}}` 注入） |
| 报告模板 | `config/templates/research_report_template.md` | 报告章节结构（报告工位 static_vars 引入） |
| 技能 | `skills/skill-solution-research/SKILL.md` | 调研框架/方法论（检索工位按需加载） |
| 既有 L2 编排者 | `config/agents/orchestrator/research_orchestrator_agent.yaml` | 五阶段状态机编排层（本包不改动） |
| 既有 L3 单体调研 | `config/agents/executor/generation/research_agent.yaml` | 轻量单工位场景保留（本包不改动） |

---

## 二、3 角色分工

| 工位 | agent_id | 职责 | 输入 | 产出（默认路径） | tool_ids |
|------|----------|------|------|------------------|----------|
| ① 检索 | `research_retrieval_agent` | 按 research_questions 检索、信源标注、收集-写入循环、覆盖度自查 | research_goal + research_questions + scope + preferred_sources | `docs/working/{title}_retrieval_notes.md` | universal_search / web_operate / enhanced_search（限定浅层） / file_read / file_write / list_directory / create_directory / memory / task_evaluate |
| ② 摘要 | `research_summary_agent` | 检索笔记压缩：逐问题摘要、去重、矛盾标注、信源等级分布统计（禁止发明笔记外事实） | 检索笔记路径 + research_questions | `docs/working/{title}_summary.md` | file_read / file_write / list_directory / create_directory / memory / task_evaluate |
| ③ 报告 | `research_report_agent` | 按报告模板整合最终报告：关键发现（结论-来源强制绑定）、对比、矛盾、参考来源表、审计日志摘要 | 摘要稿路径 + research_goal | `docs/working/{title}_research_report.md` | file_read / file_write / list_directory / create_directory / memory / task_evaluate |

设计口径：

- **薄整合层**：3 角色的 system_prompt 只留骨架（职责/流程要点/纪律），规则正文一律经 `{{path:}}` 引用既有文件，不复制 [来源: 各角色 yaml 的 system_prompt 头部引用]。
- **tool_ids 按实况**：全部工具名已逐一核对注册面（见 `TOOL_SURFACE.md`），未核验名称一律不声明。
- **角色分工对齐路线图**：检索=信息收集、摘要=压缩结构化、报告=最终交付 [来源: ROADMAP.md 0.4.0 节]。

---

## 三、调用链

```
L1（灵汐）/ L2（research_orchestrator_agent 等编排者）
  │  任务拆解：research_questions / scope / preferred_sources
  ├─▶ research_retrieval_agent（检索工位）──▶ {title}_retrieval_notes.md
  ├─▶ research_summary_agent（摘要工位）  ──▶ {title}_summary.md
  └─▶ research_report_agent（报告工位）   ──▶ {title}_research_report.md
```

- **串行依赖**：检索 → 摘要 → 报告（后一工位读前一工位产出；路径经 goal 传递）。
- **可并行点**：检索工位可按问题组拆多个并行子任务，汇总后再进摘要工位。
- **与既有编排层的关系**：既有 `research_orchestrator_agent`（五阶段状态机 + 闭环补充检索）继续作为 L2 编排层使用；本包 3 工位为其下游执行面候选。将编排者「下级 Agent 映射」切换到 3 工位属后续增强项（记录于第七节，本包不改动既有编排者配置，避免与并行批次冲突）。
- **派发方式**：`task_submit(target_id=research_retrieval_agent / research_summary_agent / research_report_agent)`；L1/L2 均可派发（L3 执行层）[来源: config/agents/main/agentos.yaml 的 L1→L2→L3 协作口径]。

---

## 四、依赖插件面（引用，不复制）

| 插件 | 提供工具 | 档案状态 | 来源 |
|------|----------|----------|------|
| omnisearch（external MCP） | universal_search 等 8 工具 | ✅ `default_profile.yaml` 显式 `enabled: true` | [来源: config/kernel/default_profile.yaml] |
| agentos-builtin-tools | file_read / file_write / enhanced_search / list_directory / create_directory 等 | ✅ 显式 `enabled: true` | [来源: config/kernel/default_profile.yaml] |
| memory_tool | memory | ✅ 显式 `enabled: true` | [来源: config/kernel/default_profile.yaml] |
| task_evaluate_tool | task_evaluate | 未显式列出（按 `defaults.enabled: true` 默认启用） | [来源: config/kernel/default_profile.yaml] |
| web_ext（web_operate_tool） | web_operate | 未显式列出（按 `defaults.enabled: true` 默认启用） | [来源: plugins/shared/tools/web_ext/plugin.json] |

> omnisearch 为 external MCP：`plugin.json` 的 `mcp.endpoint` 指向本地部署（`D:/myproject/omnisearch-mcp`，PyPI 发布后可切 uvx）；部署缺失时 `universal_search` 不可用——如实标注 [来源: plugins/shared/tools/external_mcp/omnisearch/plugin.json]。

---

## 五、启用步骤（手动）

> 无「一键导入/启用」机制（见第七节决策升级项）。当前启用 = 文件就位 + 加载链自动发现：

1. **文件就位**：本包已提交于 `config/agents/modes/research/`（3 个角色 yaml）。
2. **自动发现**：agent 配置加载链（管道输入插件 `context_build`）按 agent_id 递归扫描 `config/agents/**`——文件名 `<agent_id>.yaml` 优先，未命中回退按 `config_id` 匹配；**无需注册、无需重启** [来源: plugins/shared/pipeline/input/context_build/plugin.py `_find_agent_yaml`]。
3. **热生效**：agent 配置 mtime 热生效（yaml 改动下一轮执行即生效）[来源: AGENTS.md「配置与热生效」节]；前端 Agents 页面需刷新页面后可见新配置（schema 刷新语义）[来源: .project/widget_contracts.md 第九节]。
4. **验证**：管理面 `GET /ext/agent_manager/agents` 应列出 3 个新 agent（管理面同样扫描 `config/agents/**/*.yaml`）[来源: plugins/shared/system/agent_manager/server.py `collect_yaml_files`]；或直接按 agent_id 派发一次小任务验证。
5. **使用**：按第三节调用链派发；单工位也可独立使用（如只做检索笔记）。

---

## 六、工具健康标注（摘要）

> 完整清单与来源见 `TOOL_SURFACE.md`。要点：

- ⚠️ **enhanced_search 已知卡顿**：全仓扫描分钟级（2026-09-13 实测单次 15m13s，D1 修复中）；本包仅在检索工位声明且限定「限定目录浅层检索」，提示词与约束中均标注该风险，不掩盖 [来源: docs/working/自主批次_路线图执行_20260913.md；R5v2 简报]。
- ⛔ 四个外部 MCP 搜索源（mcp_registry_search / smithery_search / langchain_hub_search / external_resource_search）已于 2026-09-13 退役删除，**禁止**在新配置中声明 [来源: docs/decisions/2026-09-13-orphan-mcp-search-plugins-removed.md]。

---

## 七、决策升级项（不自行扩契约）

| # | 升级项 | 说明 |
|---|--------|------|
| 1 | **模式包导入/启用机制缺失** | 当前无「一键导入/启用」通道，本包以配置目录形态 + 手动启用步骤交付；若模式包成为正式分发单元，需定义导入机制（决策升级，未扩契约） |
| 2 | **模式包清单/manifest 契约未定义** | 本包以 README + 清单文档（人读）形态交付，未发明机读 manifest 契约；如未来需要（安装器/市场消费），另行定契约 |
| 3 | **编排层下级映射切换** | 将 `research_orchestrator_agent` 下级映射从 `research_agent` 切到 3 工位属后续增强项（本包不改动既有编排者，避免与并行批次冲突） |
| 4 | **模式包落点规范** | 本包落点 `config/agents/modes/<mode>/` 为自定约定（创建时 R2v2/R3v2 先例未出，按「薄整合层」自定并注明）；R2v2 先例已出（`config/agents/team/coding/`，落点与本包不同）——统一收口属后续决策项 |

---

## 八、为什么是「配置目录形态」而非「插件包」

用户方向为「每模式一个插件包」，但当前架构下 **agent 配置无法由插件承载**，故取配置目录形态 + 清单文件：

- agent 配置的唯一加载链是 `context_build` 插件扫描 `config/agents/**`（agent 配置解析已归插件域，但**文件位置**固定在 config 目录）[来源: docs/decisions/2026-09-02-agent-config-decouple-tool-surface.md]；
- 插件协议 manifest 的 `contributes` 面为前端贡献点（views/widgets/menus 等），**无 agent 配置注册面** [来源: docs/guides/plugin-protocol.md §2 字段表]；
- 若未来要让模式包成为插件形态分发，需扩插件契约（决策升级，未自行扩）。
