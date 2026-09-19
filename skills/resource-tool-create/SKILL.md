---
name: 创建工具
description: 创建或修改工具时加载。0.2 插件化工具创建规范：TDD 流程、plugins/shared/tools 布局、plugin.json 工具声明（input_schema/output_schema/render）、tool_ids 登记、热发现行为。
---

# 创建工具

> **本技能是流程指引，不是逐条打勾清单**：按场景判断使用，遇不适用情况保持裁量，不机械执行。

## 工具创建流程（TDD 模式）

### 1. 需求分析
- 明确工具的名称（tool_id，snake_case）、描述、输入输出接口、使用场景
- 先查 `plugins/shared/tools/` 现有工具：能复用/扩展现有工具时禁止新建

### 2. 编写测试（Red 阶段）
- 测试写到 `plugins/shared/tools/{tool_id}/test_{tool_id}.py`（与实现同层）
- 覆盖：核心功能、边界情况、错误处理
- 测试中引用尚未实现的工具实现，运行确认失败（Red）

### 3. 生成工具代码（Green 阶段）
- 实现写到 `plugins/shared/tools/{tool_id}/`，写最少的代码让测试通过
- 运行测试确认通过（Green）：`pytest plugins/shared/tools/{tool_id}/`

### 4. 重构优化（Refactor 阶段）
- 测试通过前提下优化代码，运行测试确认无回归

### 5. 声明与登记（0.2 必做）
- `plugin.json` 的 `capabilities.tools[]` 声明即注册（无需类型豁免，无需改 `__init__.py`）
- 把 tool_id 加入目标 agent 的 `tool_ids` 白名单（如 `config/agents/main/agentos.yaml`）——不登记则 LLM 工具面不可见

## plugin.json 工具声明（0.2 契约）

目录布局（参照现有插件 `plugins/shared/tools/bash/`、`download/`）：

```
plugins/shared/tools/{tool_id}/
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
- [ ] `plugins/shared/tools/{tool_id}/plugin.json` 存在，capabilities.tools 声明含 output_schema + render
- [ ] `pytest plugins/shared/tools/{tool_id}/` 通过（含 Red 阶段写下的用例）
- [ ] tool_id 已加入目标 agent 的 tool_ids 白名单
- [ ] 权限 permissions 最小授权（不写 `"*"` 除非确有必要）
- [ ] 触碰即清：新代码带测试，不动 mypy/覆盖率基线（见 AGENTS.md 治理债条款）
