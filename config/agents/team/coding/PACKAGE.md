# coding 模式包（编码团队）— 包说明

> 状态：v1（2026-09-14，批次 R2v2-close 交付）
> 形态：**配置目录形态 + 包说明/清单**（薄整合层：角色 YAML 只留团队语义骨架，执行流程复用既有执行层配置，不复制）
> 落点：`config/agents/team/coding/`（4 份角色 YAML 已落库，本文件为包说明；本批次不改动 4 份 YAML 内容）

---

## 一、包结构

```
config/agents/team/coding/
├── PACKAGE.md                  # 本文件（包说明：分工/调用链/工具面/启用/档位）
├── manifest.yaml               # 包清单（机读，最简结构）
├── coding_pm_agent.yaml        # 角色① PM（L2，orchestrator）
├── coding_dev_agent.yaml       # 角色② 开发（L3，atomic）
├── coding_test_agent.yaml      # 角色③ 测试（L3，atomic）
└── coding_review_agent.yaml    # 角色④ 评审（L3，specialized）
```

外部引用（**不复制进包**，单一真值在既有位置；各角色经 `{{path:}}` 注入）：

| 引用对象 | 路径 | 引用角色 |
|----------|------|----------|
| 信息诚实原则 | `config/rules/information_integrity_rules.md` | 4 角色（system_prompt） |
| 文档上下文规则 | `config/rules/document_context_rules.md` | 4 角色（system_prompt） |
| 编码领域规则 | `config/rules/per_domain/coding_domain_rules.md` | 4 角色（system_prompt） |
| 执行回顾规则 | `config/rules/execution_review_rules.md` | 4 角色（dynamic_vars） |
| 测试规则 | `config/rules/testing_rules.md` | PM / dev / test |
| 需求结构化流程 | `config/processes/requirement_structuring.md` | PM |
| 派单规则 | `config/rules/dispatcher_rules_exec.md` | PM（dynamic_vars） |
| 代码编写规则 | `config/rules/per_agent/code_writer_rules.md` | dev（dynamic_vars） |
| 状态机测试设计 | `config/rules/per_agent/state_machine_test_design.md` | test（dynamic_vars） |
| 隔离网络规则 | `config/rules/isolation_network_rules.md` | test |
| 设计系统约束 | `config/rules/per_agent/design_system_constraints.md` | review |
| 测试代码模板 | `config/templates/test_code_template.md` | test（static_vars） |
| 代码分析报告模板 | `config/templates/code_analysis_report_template.md` | review（static_vars） |
| 技能：编码实现 | `skills/code-implement/SKILL.md` | dev（提示词内按需加载） |
| 技能：前端领域 | `skills/code-frontend/SKILL.md` | dev / review（按技术栈按需加载） |
| 技能：后端领域 | `skills/code-backend/SKILL.md` | dev / review（按技术栈按需加载） |

> 核验记录：上表 16 个引用路径已逐一核验存在（16/16 OK，2026-09-14 本批次 bash 核验）[来源: 本批次执行核验；各角色 YAML 的 system_prompt / static_vars / dynamic_vars 字段]

---

## 二、分工与调用链

### 2.1 调用链（PM → dev/test → review）

```
L1（灵汐）/ 人类
  │
  └─▶ coding_pm_agent（PM，L2）—— 拆解 → 派单 → 跟踪 → 验收
        │  task_submit（派发目标见下表）
        ├─▶ coding_dev_agent（开发，L3）──▶ 代码 + 测试 + 执行报告
        ├─▶ coding_test_agent（测试，L3）──▶ 测试代码 + 测试报告（失败证据/回归结果）
        └─▶ coding_review_agent（评审，L3）──▶ 审查报告（Must Fix / 一票否决）
        │
        └─ task_evaluate 逐条核对 AC → 验收（未过 AC 不得标记完成）
```

### 2.2 角色分工与派发映射

| 环节 | 派发 target | 职责 | 关键产出 |
|------|------------|------|----------|
| PM | `coding_pm_agent` | 需求拆解（AC 清单按模块分组）、任务派单、执行跟踪、交付验收 | AC 清单 / 派单记录 / 验收结论 |
| 开发 | `coding_dev_agent` | 按 AC 编码、TDD 红绿循环自测、交付可运行代码 | 代码 + 测试 + 执行报告 |
| 测试 | `coding_test_agent` | 测试设计（正常/边界/异常）、运行验证、失败调试修复 | 测试代码 + 测试报告（含失败证据与回归结果） |
| 评审 | `coding_review_agent` | 独立代码审查、一票否决、AC 符合度判断 | 审查报告（静态扫描指标 + 细节清单核对） |

### 2.3 协作边界

- **PM 不直接写代码/改文件**：编码归开发角色、测试归测试角色、审查归评审角色；PM 只承担拆解→派单→跟踪→验收的团队语义 [来源: coding_pm_agent.yaml 边界节]。
- **开发不承担独立审查与功能验证**：评审归评审角色，验证归 PM 编排或专门验证者 [来源: coding_dev_agent.yaml 边界节]。
- **测试不承担独立审查**；调试阶段（复现与追踪）禁止修改代码，只能加诊断标记；同一模块 bug 打回超过 3 次未解决时触发修复熔断并升级 [来源: coding_test_agent.yaml 边界节]。
- **评审只认规则与自动化工具的客观结果**，禁止主观通融；细节清单通过率 < 80% → Request Changes [来源: coding_review_agent.yaml 边界节]。

### 2.4 独立性硬约束（评审不审自写代码）

- 评审角色**不得审查自己参与编写的代码**（同一管道不得既开发又审查）[来源: coding_review_agent.yaml 边界节]。
- 审查/验证独立性：**评审角色不得与被审角色为同一管道** [来源: coding_pm_agent.yaml hard_constraints]。
- 回归重派必须 `inherit pipe` 继承上轮上下文，goal 含「复验上轮 Must Fix + 增量审本次修复范围」[来源: coding_pm_agent.yaml hard_constraints]。

---

## 三、工具面与 skill 引用清单（从 4 份 YAML 汇总，不臆造）

### 3.1 各角色 tool_ids 全集

| 角色 | agent_type / level | tool_ids 全集 |
|------|--------------------|---------------|
| `coding_pm_agent` | orchestrator / L2 | `task_submit`, `task_manage`, `task_evaluate`, `file_read`, `file_write`, `list_directory`, `human_interaction`, `memory` |
| `coding_dev_agent` | atomic / L3 | `file_read`, `file_write`, `list_directory`, `create_directory`, `enhanced_search`, `bash_execute`, `task_evaluate`, `lsp_definition`, `lsp_references`, `lsp_diagnostics`, `lsp_jump_to_file` |
| `coding_test_agent` | atomic / L3 | `file_read`, `file_write`, `list_directory`, `create_directory`, `enhanced_search`, `bash_execute`, `web_operate`, `task_evaluate`, `lsp_definition`, `lsp_references`, `lsp_diagnostics`, `lsp_jump_to_file` |
| `coding_review_agent` | specialized / L3 | `file_read`, `file_write`, `list_directory`, `enhanced_search`, `bash_execute`, `task_evaluate`, `lsp_definition`, `lsp_references`, `lsp_diagnostics`, `lsp_jump_to_file` |

工具并集（16 个）：`task_submit`, `task_manage`, `task_evaluate`, `human_interaction`, `memory`, `file_read`, `file_write`, `list_directory`, `create_directory`, `enhanced_search`, `bash_execute`, `web_operate`, `lsp_definition`, `lsp_references`, `lsp_diagnostics`, `lsp_jump_to_file`。

### 3.2 各角色 skill 引用与规则引用要点

| 角色 | skills 引用（提示词内按需加载） | 规则/模板引用要点 |
|------|-------------------------------|-------------------|
| `coding_pm_agent` | 无 | 4 共同项 + `testing_rules` + `requirement_structuring` + `dispatcher_rules_exec` |
| `coding_dev_agent` | `skills/code-implement/SKILL.md`（编码前加载）；`skills/code-frontend` 或 `skills/code-backend`（按任务领域） | 4 共同项 + `testing_rules` + `code_writer_rules` |
| `coding_test_agent` | 无 | 4 共同项 + `testing_rules` + `isolation_network_rules` + `state_machine_test_design` + `test_code_template` |
| `coding_review_agent` | `skills/code-frontend/SKILL.md` 或 `skills/code-backend/SKILL.md`（按任务技术栈） | 4 共同项 + `design_system_constraints` + `code_analysis_report_template` |

> 「4 共同项」= `information_integrity_rules` / `document_context_rules` / `coding_domain_rules` / `execution_review_rules`（见 §一 引用表）。
> 完整路径清单见 §一 外部引用表；以上均逐字段摘录自各角色 YAML，未增删 [来源: config/agents/team/coding/*.yaml]。

### 3.3 各角色复用基线（reuse_base）与能力标签（capabilities）

| 角色 | reuse_base（复用既有执行层） | capabilities |
|------|------------------------------|--------------|
| `coding_pm_agent` | `mode_coding/programming_orchestrator_agent_v2` | requirement_structuring / task_dispatch / task_tracking / acceptance_verification |
| `coding_dev_agent` | `mode_coding/code_writer` | code_generation / code_refactoring / unit_test_writing / acceptance_criteria_driven |
| `coding_test_agent` | `mode_coding/test_debug_agent` | test_case_design / test_code_generation / coverage_analysis / bug_reproduction / root_cause_analysis / regression_testing |
| `coding_review_agent` | `mode_coding/code_reviewer_agent` | static_analysis / code_review / bug_detection / detail_checklist_enforcement / acceptance_criteria_verification |

---

## 四、如何启用

> 无「一键导入/启用」机制；当前启用 = 文件就位 + 加载链自动发现 + 会话绑定 agent_id。

1. **文件就位**：4 份角色 YAML 已在 `config/agents/team/coding/`（本包已落库）。
2. **自动发现（加载口径）**：agent 配置加载链（管道输入插件 `context_build`）按 agent_id 递归扫描 `config/agents/**`——文件名 `<agent_id>.yaml` 优先，未命中回退按 `config_id` 匹配；**无需注册、无需重启** [来源: plugins/shared/pipeline/input/context_build/plugin.py `_find_agent_yaml`]。双根解析：用户配置层 `agents/` 优先，未命中回落 factory（`AGENTOS_CONFIG_ROOT`）[来源: 同上 `_load_agent_config`]。yaml 改动按 mtime 缓存失效热生效 [来源: 同上插件缓存注释]。
3. **会话绑定 agent_id**：`PATCH /api/v1/sessions/{id}/agent`，body `{"agent_id": "coding_pm_agent"}`——绑定/切换会话的主 Agent；三写：① registry 线程绑定（内存热路径）② sessions 表 agent_id（跨重启 DB 冷兜底）③ 主管道 state `agent.id`（绑定真值落管道 state）[来源: kernel/crates/api/src/session_routes.rs:320 `update_session_agent_handler`]。
4. **验证**：管理面 `GET /ext/agent_manager/agents` 应列出 4 个新 agent（管理面同样扫描 `config/agents/**/*.yaml`）[来源: research 模式包 README 转引（待复核）]；或按 agent_id 派发一次小任务验证。
5. **使用**：按 §2.1 调用链派发——PM 经 `task_submit(target_id=coding_dev_agent / coding_test_agent / coding_review_agent)`；单角色也可独立派发（如只做审查）。

---

## 五、模型档位说明

| 角色 | model_tier | 依据 |
|------|-----------|------|
| `coding_pm_agent` | medium | 需求拆解/派单/验收为开放式推理任务 [来源: coding_pm_agent.yaml `model_tier`] |
| `coding_dev_agent` | medium | 编码/TDD 为开放式实现任务 [来源: coding_dev_agent.yaml `model_tier`] |
| `coding_test_agent` | medium | 测试设计/调试根因分析为开放式推理任务 [来源: coding_test_agent.yaml `model_tier`] |
| `coding_review_agent` | small | 评审为规则/清单驱动的收敛型核对任务 [来源: coding_review_agent.yaml `model_tier`] |

> 档位事实（medium/medium/medium/small）逐字段摘录自各 YAML `model_tier` 字段。
> 「依据」列为解释性说明：**YAML 未显式声明档位理由**，此处按任务性质归纳（评审=按规则与自动化工具逐项核对、模式化程度高；PM/dev/test=拆解/编码/测试设计等开放式推理），属推断性口径，**待复核**。

---

## 六、待复核与已知事项

| # | 事项 | 说明 |
|---|------|------|
| 1 | **MODEPACK.md 引用缺失** | 4 份角色 YAML 的 `description` 均引用 `config/agents/team/coding/MODEPACK.md`（"coding 模式包说明"）作为来源，但该文件当前**未落库**（2026-09-14 核验 MISS）——本包说明文档实际为 `PACKAGE.md`；如需对齐 YAML 内引用文本，属后续收口项（待复核） |
| 2 | **manifest 契约未定义** | 仓内无团队包清单先例（`config/` 下无 `manifest*` 文件；research 包 README 第七节亦记录「模式包清单/manifest 契约未定义」）——本包 `manifest.yaml` 采用最简结构（包名/角色列表/文件+一句话职责/版本），契约待复核 |
| 3 | **管理面验证端点** | `GET /ext/agent_manager/agents` 来源为 research 包 README 转引，本批次未独立核验（待复核） |
| 4 | **模式包落点规范** | 本包落点 `config/agents/team/coding/`（team 树）与 research 包落点 `config/agents/modes/research/`（modes 树）不一致——统一收口属后续决策项 [来源: research 包 README 第七节升级项 #4] |
