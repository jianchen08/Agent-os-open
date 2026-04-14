# 内置工具代码模板

<!--
============================================================
【模板是什么】
内置工具代码模板是创建 AI Agent 系统内置工具的完整指南，涵盖工具
分类、代码结构、注册方式、配置文件和隔离策略的所有细节。

【模板的作用】
1. 标准化开发 — 统一工具的代码结构、注册方式和配置格式
2. 类型全覆盖 — 覆盖简单工具、复杂工具、需会话工具、评估器四种类型
3. 即学即用 — 提供完整的代码模板和配置示例，可直接复制修改
4. 最佳实践 — 内含命名规范、错误处理、测试要求等开发规范

【如何使用本模板】
1. 确定工具类型（简单/复杂/需会话/评估器）
2. 参照对应的代码模板创建工具文件
3. 按配置模板在 builtin_tools_config.yaml 中注册
4. 按测试模板编写测试用例
5. 参照隔离策略配置运行环境

【适用场景】
- 新建内置工具：按照模板创建新的工具
- 理解工具架构：了解工具系统的整体设计
- 工具代码审查：参照模板规范审查工具代码

【工具类型选择指南】
| 类型 | 适用场景 | 示例 |
|------|----------|------|
| 简单工具 | 单一功能、无状态、无子模块 | file_read, todo_manage |
| 复杂工具 | 多功能、有子模块、需要拆分 | bash（含多个子命令） |
| 需会话工具 | 需要访问会话上下文或状态 | task_submit, memory |
| 评估器 | 对任务结果进行质量评估 | schema_evaluator |
============================================================
-->

> 本模板详细说明创建内置工具所需的全部文件和配置。
> 每种工具类型对应不同的代码文件、注册方式、配置文件和隔离策略。

---

## 一、工具系统架构

### 1.1 工具分类体系

| 来源 | 说明 | 代码位置 | 配置位置 |
|------|------|----------|----------|
| 内置工具（builtin） | Python 代码实现的工具 | `src/tools/builtin/` | `config/tools/builtin_tools_config.yaml` |
| MCP 工具 | MCP 协议外部工具 | `src/tools/adapters/` | `config/tools/mcp_tools_config.yaml` |

### 1.2 内置工具类型

| 类型 | 代码组织 | 注册方式 | 示例 |
|------|----------|----------|------|
| 简单工具 | 单文件 `src/tools/builtin/{tool_id}.py` | `get_all_builtin_tools()` | file_read, todo_manage, evaluate |
| 复杂工具 | 目录 `src/tools/builtin/{tool_id}/` | `get_all_builtin_tools()` | bash（含子模块） |
| 需会话工具 | 单文件 | `get_all_builtin_tools_with_session()` | task_submit, task_manage, task_evaluate, memory |
| 评估器 | `src/tools/builtin/evaluators/` | `get_all_builtin_tools()` | schema_evaluator, resource_evaluator |

### 1.3 核心基类与类型

| 类/类型 | 位置 | 说明 |
|---------|------|------|
| `BuiltinTool` | `src/tools/builtin/base.py` | 内置工具基类，需实现 `get_tool_definition()` 和 `execute()` |
| `Tool` | `src/tools/types.py` | 工具定义模型，包含 name、description、input_schema 等 |
| `ToolResult` | `src/tools/types.py`（别名） | 工具执行结果，实际是 `ToolExecutionResult` |
| `ToolCategory` | `src/tools/types.py` | 工具分类枚举：file/search/web/memory/task/system/execution/analysis/evaluation/agent/monitoring |
| `ToolLevel` | `src/tools/types.py` | 工具级别枚举：system/user/l1_only/l1_l2_only/all |
| `ToolSource` | `src/tools/types.py` | 工具来源枚举：code/builtin/mcp/http/database |

---

## 二、简单工具模板（最常用）

### 2.1 代码文件

**文件路径**：`src/tools/builtin/{tool_id}.py`

```python
"""
{工具名称}工具

暴露接口：
- get_tool_definition() -> Tool：{工具名称}定义
- execute(self, inputs: dict) -> ToolExecutionResult：{工具名称}执行
"""

import logging
from typing import Any

from src.core.results import ToolExecutionResult
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)


logger = logging.getLogger(__name__)


class {ClassName}:
    """{工具名称}工具"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """返回工具定义"""
        return Tool(
            name="{tool_id}",
            description="{工具简短描述}",
            when_to_use=[
                "{适用场景1}",
                "{适用场景2}",
            ],
            when_not_to_use=[
                "{不适用场景1}",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["{action1}", "{action2}"],
                        "description": "操作类型",
                    },
                    "param1": {
                        "type": "string",
                        "description": "{参数说明}",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.{CATEGORY},
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具逻辑"""
        try:
            action = inputs.get("action", "")
            param1 = inputs.get("param1", "")

            if action == "{action1}":
                result = self._handle_action1(param1)
            elif action == "{action2}":
                result = self._handle_action2(param1)
            else:
                return create_failure_result(f"不支持的操作: {action}")

            return create_success_result(data=result)
        except Exception as e:
            logger.error(f"{tool_id} 执行失败: {e}")
            return create_failure_result(str(e))

    def _handle_action1(self, param: str) -> dict[str, Any]:
        """处理 action1"""
        return {"result": param}

    def _handle_action2(self, param: str) -> dict[str, Any]:
        """处理 action2"""
        return {"result": param}
```

### 2.2 注册到 `__init__.py`

**文件路径**：`src/tools/builtin/__init__.py`

在 `get_all_builtin_tools()` 函数中添加：

```python
from .{tool_id} import {ClassName}

# 在返回列表中添加
return [
    # ... 已有工具 ...
    {ClassName}(),
]
```

如果是需会话的工具（需要数据库 session），在 `get_all_builtin_tools_with_session()` 函数中添加：

```python
from .{tool_id} import {ClassName}

# 在返回列表中添加类（不实例化）
return [
    # ... 已有工具 ...
    {ClassName},
]
```

并在 `register_all_builtin_tools()` 中添加会话注册逻辑。

---

## 三、复杂工具模板（目录结构）

### 3.1 目录结构

```
src/tools/builtin/{tool_id}/
├── __init__.py       # 导出工具类
├── tool.py           # 工具主类（BuiltinTool 子类）
├── types.py          # 内部类型定义
└── {helper}.py       # 辅助模块
```

### 3.2 工具主类

**文件路径**：`src/tools/builtin/{tool_id}/tool.py`

```python
"""
{工具名称}工具主模块

暴露接口：
- get_tool_definition() -> Tool：工具定义
- execute(self, inputs: dict) -> ToolExecutionResult：工具执行
"""

import logging
from typing import Any

from src.core.results import ToolExecutionResult
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class {ClassName}:
    """{工具名称}工具"""

    @staticmethod
    def get_tool_definition() -> Tool:
        """返回工具定义"""
        return Tool(
            name="{tool_id}",
            description="{描述}",
            when_to_use=["{场景}"],
            when_not_to_use=["{非场景}"],
            caveats=["{注意事项}"],
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["{action1}", "{action2}"],
                        "description": "操作类型",
                    },
                },
                "required": ["action"],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.{CATEGORY},
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具逻辑"""
        try:
            action = inputs.get("action", "")
            handler = {
                "{action1}": self._handle_action1,
                "{action2}": self._handle_action2,
            }.get(action)

            if not handler:
                return create_failure_result(f"不支持的操作: {action}")

            return create_success_result(data=handler(inputs))
        except Exception as e:
            logger.error(f"{tool_id} 执行失败: {e}")
            return create_failure_result(str(e))

    def _handle_action1(self, inputs: dict) -> dict[str, Any]:
        """处理 action1"""
        return {}
```

---

## 四、需会话工具模板（需要数据库依赖）

### 4.1 适用工具

以下工具需要数据库 session，不能直接实例化：
- `memory` - 记忆检索（需 session）
- `task_submit` - 任务提交（需 session）
- `task_manage` - 任务管理（需 session）
- `task_evaluate` - 任务评估（需 session）

### 4.2 代码模板

```python
"""
{工具名称}工具（需要数据库会话）
"""

import logging
from typing import Any

from src.core.results import ToolExecutionResult
from src.tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)


class {ClassName}:
    """{工具名称}工具"""

    def __init__(self, session: Any):
        """初始化（需要数据库 session）"""
        self.session = session

    @staticmethod
    def get_tool_definition() -> Tool:
        """返回工具定义"""
        return Tool(
            name="{tool_id}",
            description="{描述}",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            source=ToolSource.BUILTIN,
            category=ToolCategory.{CATEGORY},
            injected_params=["session_id"],  # 系统注入参数，不暴露给 LLM
        )

    async def execute(self, inputs: dict[str, Any]) -> ToolExecutionResult:
        """执行工具逻辑"""
        try:
            return create_success_result(data={})
        except Exception as e:
            logger.error(f"{tool_id} 执行失败: {e}")
            return create_failure_result(str(e))
```

---

## 五、工具配置文件

### 5.1 内置工具配置

**文件路径**：`config/tools/builtin_tools_config.yaml`

此文件由 `scripts/tools/collect_tool_info.py` **自动生成**，通常不需要手动修改。

配置结构：

```yaml
# 工具缓存配置
tool_cache:
  enabled: true
  default_ttl: 300
  tools:
    {tool_id}:
      enabled: true/false
      ttl: {seconds}

# 工具定义列表（自动生成）
tools:
  - name: {tool_id}
    description: "{工具描述}"
    category: {category}
    level: user/system/l1_l2_only/all
    requires_approval: true/false
    dangerous_operations:
      - "{危险操作标识}"
    tags:
      - {tag1}
      - {tag2}

# 权限策略
permission_policies:
  admin:
    can_approve: true
    auto_approve_tools:
      - '*'
  developer:
    can_approve: false
    auto_approve_tools:
      - {tool_id}
    require_approval_tools:
      - {tool_id}
  readonly:
    can_approve: false
    auto_approve_tools:
      - {tool_id}
    require_approval_tools:
      - {tool_id}
```

### 5.2 MCP 工具配置

**文件路径**：`config/tools/mcp_tools_config.yaml`

```yaml
mcp_servers:
  - name: "{server_name}"
    command: "{启动命令}"
    args: ["{参数}"]
    env:
      {KEY}: "{VALUE}"
    tools:
      - name: "{tool_name}"
        description: "{工具描述}"
        category: "{category}"
```

### 5.3 子目录配置

内置工具配置按类别放在子目录中：

| 目录 | 配置文件 | 工具类别 |
|------|----------|----------|
| `config/tools/search/` | `resource_search.yaml`, `web_search.yaml` | 搜索类 |
| `config/tools/shell/` | `shell_execute.yaml` | Shell 执行类 |
| `config/tools/system/` | `evaluate.yaml`, `memory_retrieve.yaml`, `task_evaluate.yaml`, `task_manage.yaml`, `task_submit.yaml`, `todo_manage.yaml` | 系统类 |
| `config/tools/web/` | `fetch.yaml` | Web 操作类 |

---

## 六、隔离策略配置

**文件路径**：`config/isolation/isolation_policy.yaml`

隔离策略由配置文件统一管理，**不需要在工具代码中设置**。

### 6.1 隔离级别

| 隔离方式 | 说明 | 适用工具类型 |
|----------|------|-------------|
| `host` | 宿主机直接执行 | 文件操作、搜索、任务管理、评估验证 |
| `container` | 容器隔离执行 | Shell 命令、网络请求、桌面控制 |

### 6.2 匹配优先级

```
tools（工具名精确匹配）> categories（分类匹配）> default（默认策略）
```

### 6.3 新工具的隔离配置

创建新工具时，需要：

1. 为工具设置准确的 `category` 标签（对应 `ToolCategory` 枚举）
2. 确保工具名称能反映其功能
3. 在 `isolation_policy.yaml` 中添加对应配置：

```yaml
tools:
  {tool_id}:
    isolation: host/container
    execution: host_direct/command_in_container
    fallback: allow/deny
```

| 工具行为 | 推荐隔离策略 | 示例配置 |
|----------|-------------|----------|
| 只读文件/目录 | host, host_direct, allow | file_read, enhanced_search |
| 读写文件 | host, host_direct, allow | file_write |
| 执行命令 | container, command_in_container, deny | bash_execute |
| 网络请求 | container, command_in_container, deny | fetch, web_search |
| 纯计算/验证 | host, host_direct, allow | evaluate, yaml_validate |
| 任务管理 | host, host_direct, allow | task_submit, todo_manage |

---

## 七、工具定义关键字段说明

### 7.1 Tool 模型核心字段

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | 工具唯一标识，对应 tool_ids 中的值 |
| `description` | string | 是 | 简短功能描述 |
| `when_to_use` | list[str] | 否 | 适用场景，注入 LLM 帮助决策 |
| `when_not_to_use` | list[str] | 否 | 不适用场景 |
| `caveats` | list[str] | 否 | 注意事项 |
| `examples` | list[ToolExample] | 否 | 使用示例（最多 2 个注入 LLM） |
| `input_schema` | dict | 是 | JSON Schema 格式的输入参数定义 |
| `output_schema` | dict | 否 | JSON Schema 格式的输出定义 |
| `injected_params` | list[str] | 否 | 系统注入参数，不暴露给 LLM |
| `source` | ToolSource | 是 | 来源：BUILTIN / MCP / CODE / HTTP / DATABASE |
| `category` | ToolCategory | 否 | 功能分类 |
| `level` | ToolLevel | 否 | 级别：system / user / l1_l2_only / all |
| `dangerous_operations` | list[str] | 否 | 危险操作标识，用于审批决策 |

### 7.2 工具结果创建函数

| 函数 | 用途 | 示例 |
|------|------|------|
| `create_success_result(data)` | 成功结果 | `create_success_result(data={"files": [...]})` |
| `create_failure_result(error)` | 失败结果 | `create_failure_result("文件不存在")` |
| `create_failure_result_with_code(code, detail)` | 带错误码的失败 | `create_failure_result_with_code(ErrorCode.FILE_NOT_FOUND, path)` |

---

## 八、创建新工具的完整步骤

### 8.1 检查清单

| 步骤 | 操作 | 文件 |
|------|------|------|
| 1 | 创建工具代码文件 | `src/tools/builtin/{tool_id}.py` |
| 2 | 注册到内置工具模块 | `src/tools/builtin/__init__.py` |
| 3 | 运行自动生成脚本更新配置 | `scripts/tools/collect_tool_info.py` |
| 4 | 创建工具专属配置文件 | `config/tools/{category}/{tool_id}.yaml` |
| 5 | 配置隔离策略 | `config/isolation/isolation_policy.yaml` |
| 6 | 在需要的 Agent 中引用 | Agent 配置的 `tool_ids` |

### 8.2 工具类别与隔离速查

| ToolCategory 值 | 典型工具 | 默认隔离 |
|-----------------|----------|----------|
| FILE | file_read, file_write | host |
| SEARCH | enhanced_search, resource_search | host |
| WEB | fetch, web_search | container |
| MEMORY | memory | host |
| TASK | task_submit, todo_manage | host |
| SYSTEM | evaluate, yaml_validate | host |
| EXECUTION | bash_execute | container |
| ANALYSIS | lsp_definition | host |
| EVALUATION | schema_evaluator | host |
