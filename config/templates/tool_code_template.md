# 内置工具代码模板

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
| 简单工具 | 单文件 `src/tools/builtin/{tool_id}.py` | `get_all_builtin_tools()` | file_read, evaluate |
| 复杂工具 | 目录 `src/tools/builtin/{tool_id}/` | `get_all_builtin_tools()` | bash（含子模块） |
| 需会话工具 | 单文件 | `get_all_builtin_tools_with_session()` | task_submit, task_manage, task_evaluate, memory |
| 评估器 | `src/tools/builtin/evaluators/` | `get_all_builtin_tools()` | schema_evaluator, resource_evaluator |

### 1.3 核心基类与类型

| 类/类型 | 位置 | 说明 |
|---------|------|------|
| `BuiltinTool` | `src/tools/builtin/base.py` | 内置工具基类，需实现 `get_tool_definition()` 和 `execute()` |
| `Tool` | `src/tools/types.py` | 工具定义模型，包含 name、description、input_schema 等 |
| `ToolResult` | `src/tools/types.py`（别名） | 工具执行结果，实际是 `ToolExecutionResult` |
| `ToolCategory` | `src/tools/types.py` | 工具分类枚举：file/file_system/search/web/memory/task/system/execution/analysis/evaluation/agent/monitoring |
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

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

class {ClassName}(BuiltinTool):
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

### 2.2 注册说明

**无需手动注册！** 系统已实现自动发现机制：

1. 将工具文件放入 `src/tools/builtin/` 目录
2. 系统启动时自动扫描并发现所有 `BuiltinTool` 子类
3. 通过 `get_tool_definition()` 获取工具名并注册

**不需要修改以下文件：**
- ~~`src/tools/builtin/__init__.py`~~ — 已废弃手动注册，系统自动发现
- ~~`scripts/tools/collect_tool_info.py`~~ — 已废弃，系统自动发现
- ~~`config/tools/{category}/{tool_id}.yaml`~~ — 已废弃

如果是需会话的工具（需要数据库 session），在 `get_all_builtin_tools_with_session()` 中添加类（不实例化），并在 `register_all_builtin_tools()` 中添加会话注册逻辑。

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

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

class {ClassName}(BuiltinTool):
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

from core.results import ToolExecutionResult
from tools.builtin.base import BuiltinTool
from tools.types import (
    Tool,
    ToolCategory,
    ToolSource,
    create_failure_result,
    create_success_result,
)

logger = logging.getLogger(__name__)

class {ClassName}(BuiltinTool):
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
| `config/tools/system/` | `evaluate.yaml`, `memory_retrieve.yaml`, `task_evaluate.yaml`, `task_manage.yaml`, `task_submit.yaml` | 系统类 |
| `config/tools/web/` | `fetch.yaml` | Web 操作类 |

---

## 六、隔离策略与安全检查配置

**核心原则**：隔离策略和安全检查由外部配置统一管理，**不需要在工具代码中设置**。
工具只需在 `get_tool_definition()` 中正确设置 `category`，系统自动匹配隔离策略。

### 6.1 隔离策略

**配置文件**：`config/isolation/isolation_policy.yaml`

#### 匹配优先级

```
tools（工具名精确匹配）> categories（分类匹配）> default（默认策略）
```

#### 隔离方式

| 隔离方式 | execution 执行方式 | 说明 |
|----------|-------------------|------|
| `host` | `host_direct` | 宿主机直接执行，无隔离 |
| `container` | `command_in_container` | 容器隔离执行 |

#### 降级策略

| 降级策略 | 说明 | 适用场景 |
|----------|------|----------|
| `allow` | 隔离失败时自动降级到宿主机执行 | 一般工具 |
| `deny` | 隔离失败时拒绝执行，不降级 | 高危工具（如命令执行） |

#### 分类级策略（categories: 兜底匹配）

| ToolCategory 值 | isolation | execution | fallback | 说明 |
|-----------------|-----------|-----------|----------|------|
| `execution` | container | command_in_container | **deny** | 命令执行类，禁止降级 |
| `web` | container | command_in_container | allow | Web 操作类 |
| `file` | container | command_in_container | allow | 文件操作类 |
| `file_system` | container | command_in_container | allow | 文件系统操作类（目录、复制、移动等） |
| `search` | container | command_in_container | allow | 搜索类 |
| `analysis` | container | command_in_container | allow | 分析类 |
| `system` | **host** | host_direct | allow | 系统工具类 |
| `evaluation` | container | command_in_container | allow | 评估类 |
| `task` | **host** | host_direct | allow | 任务管理类 |
| `memory` | **host** | host_direct | allow | 记忆类 |
| `agent` | container | command_in_container | allow | Agent 调用类 |
| `monitoring` | container | command_in_container | allow | 监控类 |

#### 如何为新工具配置隔离

**大多数情况无需配置**：只要在 `get_tool_definition()` 中设置正确的 `category`，系统自动匹配到 `categories:` 下的策略。

**特殊情况需手动配置**（在 `isolation_policy.yaml` 的 `tools:` 下添加）：
- 需要强制容器隔离且禁止降级（`fallback: deny`）
- 需要特殊资源限制（`disk_quota`、`network`）

```yaml
tools:
  my_special_tool:
    isolation: container
    execution: command_in_container
    fallback: deny
    disk_quota: "100m"
    network: restricted
```

#### 隔离判断流程

```
新工具创建后，系统如何确定隔离级别：

1. 检查 tools: 下是否有精确匹配（按工具 name）
   └─ 有 → 使用工具级配置
   └─ 无 ↓

2. 检查 categories: 下是否有分类匹配（按 category 枚举值）
   └─ 有 → 使用分类级配置
   └─ 无 ↓

3. 使用 default: 默认策略（container, command_in_container, allow）
```

### 6.2 安全检查（dangerous_operations）

**核心机制**：`dangerous_operations` 是工具声明自己可能执行的危险操作列表。
系统通过 **隔离级别 + 危险操作** 的组合来决定：放行、拦截（block）、审批（needs_approval）。

#### 安全检查流程（两层）

```
工具执行请求
    │
    ▼
┌─ 第一层：security_check 插件（pipeline 层）────────────────┐
│  1. 路径遍历检测（内置）→ block                            │
│  2. 工作目录边界检查（内置）→ block                        │
│  3. security_rules.yaml 规则匹配 → block / needs_approval │
└─────────────────────────────────────────────────────────────┘
    │ 通过
    ▼
┌─ 第二层：ApprovalDecisionEngine（审批决策引擎）──────────┐
│  检测输入参数是否匹配 dangerous_operations 列表           │
│                                                          │
│  隔离级别        危险操作？        结果                   │
│  ────────────────────────────────────────               │
│  container       任意              → 自动批准            │
│  host            无                → 自动批准            │
│  host            有                → 需要审批            │
└──────────────────────────────────────────────────────────┘
```

#### dangerous_operations 在代码中的定义

在 `get_tool_definition()` 中声明：

```python
@staticmethod
def get_tool_definition() -> Tool:
    return Tool(
        name="my_tool",
        description="工具描述",
        category=ToolCategory.FILE,
        dangerous_operations=[
            "delete_lines:",    # 删除行操作
            "write:/etc/",      # 写入敏感路径
        ],
        # ...
    )
```

#### 现有工具的危险操作声明示例

| 工具 | dangerous_operations | 说明 |
|------|---------------------|------|
| `file_read` | `["read:/etc/", "read:/sys/"]` | 读取敏感路径标记为危险 |
| `file_write` | `["write:/etc/", "delete_lines:"]` | 写入敏感路径/删除行标记为危险 |
| `bash_execute` | `["rm -rf", "format", "shutdown"]` | 破坏性命令标记为危险 |
| `enhanced_search` | `[]` | 无危险操作 |
| `fetch` | `[]` | 无危险操作 |

#### dangerous_operations 匹配机制

`DangerChecker` 检测输入参数是否包含声明的危险操作：

```
匹配规则：
1. 获取工具的命令输入字段（如 bash_execute → command 字段）
2. 如果输入字段值中包含 dangerous_operations 中的字符串 → 匹配
3. 如果没有命令输入字段，检查参数名是否以危险操作开头
   例：参数名 "write:/etc/config" 匹配 "write:/etc/"
```

#### 如何判断工具需要声明哪些 dangerous_operations

```
判断流程：

1. 工具是否会访问敏感路径？
   └─ 是 → 添加 "操作类型:路径前缀"
   └─ 例：file_read → "read:/etc/", "read:C:\Windows\"

2. 工具是否可能执行破坏性操作？
   └─ 是 → 添加具体操作标识
   └─ 例：file_write → "delete_lines:", bash_execute → "rm -rf"

3. 工具只做纯计算/查询？
   └─ 是 → dangerous_operations: []

4. 声明的危险操作什么时候会触发审批？
   └─ 只在 HOST 隔离模式 + 输入匹配到危险操作时才触发
   └─ 容器模式下即使匹配到也自动批准（容器已提供隔离保护）
```

#### 审批结果处理

| 决策类型 | 触发条件 | 结果 |
|----------|----------|------|
| AUTO_APPROVED | 容器模式 / 无危险操作 | 自动执行 |
| NEEDS_APPROVAL | HOST模式 + 匹配到危险操作 | 暂停等待人工审批 |
| BLOCKED | security_rules.yaml 规则匹配 | 直接拦截，不执行 |

#### 全局安全规则（security_rules.yaml）

除了工具级 `dangerous_operations`，系统还有全局安全规则，由 `security_check` 插件统一执行：

| 规则名 | 作用 | 动作 |
|--------|------|------|
| `dangerous_commands` | 拦截危险命令（rm -rf, curl, pip install 等） | block |
| `protected_paths` | 拦截对系统关键路径的访问 | block |
| `ssrf_protection` | 防止 SSRF 攻击（localhost, 内网 IP） | block |
| `high_risk_operations` | 高风险操作需要审批（sudo, docker run） | needs_approval |

**新工具无需修改 security_rules.yaml**，全局规则对所有工具自动生效。

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

| 步骤 | 操作 | 文件 | 备注 |
|------|------|------|------|
| 1 | 创建工具代码文件 | `src/tools/builtin/{tool_id}.py` | 继承 BuiltinTool，设置正确 category |
| 2 | ~~注册到内置工具模块~~ | ~~`src/tools/builtin/__init__.py`~~ | **已废弃，系统自动发现** |
| 3 | ~~运行自动生成脚本~~ | ~~`scripts/tools/collect_tool_info.py`~~ | **已废弃，系统自动发现** |
| 4 | ~~创建工具专属配置文件~~ | ~~`config/tools/{category}/{tool_id}.yaml`~~ | **已废弃** |
| 5 | 配置安全标记（如需要） | `config/tools/builtin_tools_config.yaml` | 设置 requires_approval 和 dangerous_operations |
| 6 | 配置隔离策略（仅特殊需求） | `config/isolation/isolation_policy.yaml` | 大多数情况无需配置，category 自动匹配 |
| 7 | 在需要的 Agent 中引用 | Agent 配置的 `tool_ids` | 在 Agent 的 tool_ids 中添加 |

### 8.2 工具类别与隔离速查

> 详见 [6.1 隔离策略 - 分类级策略](#61-隔离策略) 中的表格。

---

## 九、MCP 工具创建说明

### 9.1 MCP 工具适用场景

MCP 工具适用于：
- 已有成熟的 MCP 服务器实现
- 需要使用外部工具/服务
- 不想在代码中实现复杂逻辑

### 9.2 MCP 工具配置

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

### 9.3 MCP 工具特点

- **无需编写 Python 代码**：MCP 工具由外部 MCP 服务器提供
- **无需注册到 `__init__.py`**：系统在启动时自动发现 MCP 工具
- **配置驱动**：所有工具行为由 MCP 服务器实现

### 9.4 三种工具类型对比

| 类型 | 代码位置 | 注册方式 | 配置位置 |
|------|----------|----------|----------|
| 内置工具（Python） | `src/tools/builtin/{tool_id}.py` | 自动发现 | `builtin_tools_config.yaml` |
| MCP 工具 | 外部 MCP 服务器 | 自动发现 | `mcp_tools_config.yaml` |
| 评估器 | `src/tools/builtin/evaluators/` | 自动发现 | `builtin_tools_config.yaml` |

### 9.5 创建工具的完整检查清单

| 步骤 | 操作 | 适用类型 | 文件位置 |
|------|------|----------|----------|
| 1 | 创建工具代码文件 | Python 内置工具 | `src/tools/builtin/{tool_id}.py` |
| 2 | 继承 BuiltinTool 基类 | Python 内置工具 | 继承 `src/tools/builtin/base.py` |
| 3 | 实现 get_tool_definition() | Python 内置工具 | 返回 `Tool` 对象 |
| 4 | 实现 execute() | Python 内置工具 | 返回 `ToolExecutionResult` |
| 5 | 配置 MCP 服务器 | MCP 工具 | `config/tools/mcp_tools_config.yaml` |
| 6 | 配置隔离策略（可选） | 特殊需求 | `config/isolation/isolation_policy.yaml` |

**注意**：
- **不需要修改 `src/tools/builtin/__init__.py`**
- **不需要运行 `scripts/tools/collect_tool_info.py`**
- 系统启动时会自动扫描并发现所有工具
