---
name: 创建工具
description: 创建或修改工具时加载。0.2 插件化工具创建规范：TDD 流程、plugins/shared 平铺布局、plugin.json 工具声明（input_schema/output_schema/render）、tool_ids 登记、热发现行为。
---

# 创建工具

> **本技能是流程指引，不是逐条打勾清单**：按场景判断使用，遇不适用情况保持裁量，不机械执行。

## 工具创建流程（TDD 模式）

### 1. 需求分析
- 明确工具的名称（tool_id，snake_case）、描述、输入输出接口、使用场景
- 先查 `plugins/shared/` 现有工具：能复用/扩展现有工具时禁止新建

### 2. 编写测试（Red 阶段）
- 测试写到 `plugins/shared/{tool_id}/test_{tool_id}.py`（与实现同层）
- 覆盖：核心功能、边界情况、错误处理
- 测试中引用尚未实现的工具实现，运行确认失败（Red）

### 3. 生成工具代码（Green 阶段）
- 实现写到 `plugins/shared/{tool_id}/`，写最少的代码让测试通过
- 运行测试确认通过（Green）：`pytest plugins/shared/{tool_id}/`

### 4. 重构优化（Refactor 阶段）
- 测试通过前提下优化代码，运行测试确认无回归

### 5. 声明与登记（0.2 必做）
- `plugin.json` 的 `capabilities.tools[]` 声明即注册（无需类型豁免，无需改 `__init__.py`）
- 把 tool_id 加入目标 agent 的 `tool_ids` 白名单（如 `config/agents/main/agentos.yaml`）——不登记则 LLM 工具面不可见
- **或**走动态类路径（见下节「动态类工具」）：不登记 `tool_ids` 也可经
  `resource_search(mode=detailed)` + `call_tool` 可达

## 动态类工具（第三种可见性，ADR 2026-11-15-dynamic-tool-surface）

工具有两条可见路径，二者按需选择、不互斥：

| 路径 | 前置 | 何时可见 | 适用 |
|---|---|---|---|
| **常驻类** | 加入目标 agent 的 `tool_ids` | 该 agent 每轮都在工具面 | 高频、核心工具 |
| **动态类** | 不需要改 `tool_ids` | 模型 `resource_search(mode=detailed)` 搜到并引入后，经 `call_tool` 调用 | 长尾工具、第三方 MCP、按需能力 |

动态类对插件作者的**唯一额外要求**：`capabilities.tools[]` 声明**带 `input_schema`
（`type` 必须是 `object`）**。缺失或非 object 会在**引入侧**被 fail-closed 拒收
（不补 `{}`——补空对象 = 零参数盲调，与「external MCP 缺 schema 拒注册」同口径）；
`output_schema` / `render` 缺失只降级（前端 generic 回退）。

链路口径：

```
LLM → resource_search(resource_type="tool", mode="detailed")
        └─ 返回结构化 tool_defs（真实 input_schema/output_schema/render/plugin_id）
            └─ tool_schema 插件（prepare 步）组装进 state["dynamic_tools"]
                └─ LLM → call_tool(tool=<目标名>, arguments={...})
                      └─ 转发既有 tool-executor.invoke（不新开通道/不新增契约）
```

插件作者需要知道的四条边界：

- **不绕过权限**：动态目标不在 `tool_ids` 里，但 4 档判定按**目标工具名 + 目标参数**
  展开（`security_check` / `level_guard` / `duplicate_check`）——危险目标照旧进审批链。
- **不进 `tool_schemas`**：动态类不写 LLM 原生 `tools` 数组（前缀缓存稳定），也不写注册表。
- **定义真值只认声明**：模型回传的定义不作真值；`plugin_id` 取内核活注册表条目的
  `plugin_id`（= `manifest.id`），**绝不用插件目录名**（目录名与 manifest id 无契约关系，
  按目录名查插件必然内核 `plugin not found`）。
- **零声明 external MCP**：`entry: "mcp:external"` 的受管目录（`plugin.json` 里没有
  `capabilities.tools`）由握手 `tools/list` 观测导入，其 `ToolDescriptor` 自带
  `input_schema`，动态定义从活注册表取；回收沿用既有 idle GC 的空闲阈值
  （**没有「用完即卸」机制**）。

## plugin.json 工具声明（0.2 契约）

目录布局（参照现有插件 `plugins/shared/bash/`、`web_ext/`（拍平布局，ADR 2026-10-13））：

```
plugins/shared/{tool_id}/
├── plugin.json      # 清单：声明即注册
├── server.py        # 入口（entry: "python server.py"）
├── tool.py          # 工具实现
├── test_{tool_id}.py
└── pyproject.toml / uv.lock   # 有第三方依赖时
```

`capabilities.tools[]` 每个工具声明（tool_core 校验 fail-closed）：

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | 工具调用名（snake_case） |
| `description` | ✅ | 给 LLM 看的能力描述（何时用、参数含义） |
| `category` | ✅ | 参照同类工具取值（如 `system`） |
| `input_schema` | ✅ | JSON Schema（type/properties/required） |
| `output_schema` | ✅ | JSON Schema——**工具契约，缺失过不了 tool_core 校验** |
| `render` | ✅ | 前端渲染意图（如 `{"card": "terminal"}`），前端按 render 路由 |

清单其他关键字段：`plugin_type: "tool"`、`host_type`（sidecar 进程内）、`entry`、
`permissions`（filesystem/network/env_vars，最小授权）。

## 热发现行为

- 新建插件目录、修改 plugin.json、修改插件代码均由 watcher 自动处理
  （发现→G2 校验→注册/重注册），无需重启
- 前端 schema 需刷新页面才更新
- G2 校验失败的插件不入面——先修声明再查代码

## 验证清单

创建工具后逐项核对：
- [ ] `plugins/shared/{tool_id}/plugin.json` 存在，capabilities.tools 声明含 output_schema + render
- [ ] `pytest plugins/shared/{tool_id}/` 通过（含 Red 阶段写下的用例）
- [ ] tool_id 已加入目标 agent 的 tool_ids 白名单
- [ ] 权限 permissions 最小授权（不写 `"*"` 除非确有必要）
- [ ] 触碰即清（改动触碰某模块时同刀清该模块治理债）：新代码带测试，不动 mypy/覆盖率基线（细见本仓 AGENTS.md「约定」节）

## tool_policy 分级同步（grants 执法面，声明即必做）

新声明工具（或给既有多动作工具的 `input_schema.properties.action` 枚举加值）必须同步在
`config/plugins/grants/tool_policy.yaml` 登记分级，键 = `tool.action`：

- **多动作工具逐 action 列键**；整工具键仅限单动作工具；整族盖多动作须走 `tool.*` 通配
  且该条 `notes` 必须非空（登记理由，防通配滥用）。
- 不同步的两连红：①运行时未分类 = deny（fail-closed，ADR
  2026-11-19-grants-ledger-and-tool-classification D2）；②CI 覆盖门禁
  `tool-policy-coverage` 红（声明面 ∩ tool_ids 白名单 + mode.yaml inject_tool_ids
  + call_tool/dsh.tool.call 动态可达目标，全部必须被表覆盖，缺口集见门禁 `--report`）。
- verdict 取值口径参照同表既有条目：读→`auto` / 工作空间内可逆写→`task_grant` /
  出网与外部副作用→`domain_grant` / 不可逆与人审门→`per_call_approval` /
  管理面→`deny`；被 `final_approval: true` 规则覆盖的键恒逐次人审（规则面优先，
  grant 不豁免）。
