# 工具工匠模板与机制改造方案

## 一、问题诊断

### 1.1 tool_maker.yaml 与现有系统的不对齐问题

#### 1.1.1 工具ID不匹配

| tool_maker.yaml 声明的 tool_ids | 实际代码中的工具ID | 状态 |
|-------------------------------|------------------|------|
| `file_read` | `file_read` (FileReadTool) | ✅ |
| `file_write` | `file_write` (FileWriteTool) | ✅ |
| `bash_execute` | `bash` (BashTool)，但配置文件用 `bash_execute` | ⚠️ 不一致 |
| `fetch` | `fetch` (WebTool) | ✅ |
| `resource_search` | `resource_search` (ResourceSearchTool) | ✅ |
| `memory` | `memory` (MemoryTool) | ✅ |
| `task_evaluate` | `task_evaluate` (TaskEvaluateTool) | ✅ |

**问题详情**：代码中 `src/tools/builtin/__init__.py` 使用的是：
```python
(".bash", "BashTool"),  # 模块名是 bash
```
但 `builtin_tools_config.yaml` 中注册为 `bash_execute`。

#### 1.1.2 ToolCategory 不匹配

**tool_maker.yaml 第110-117行提到的分类**：
```
file/search/execution/network/system/analysis/evaluation/task/memory
```

**实际 ToolCategory 枚举** (`src/tools/types.py`)：
```python
class ToolCategory(str, Enum):
    FILE = "file"           # 文件操作
    SEARCH = "search"       # 搜索
    WEB = "web"            # Web 操作 ← 模板中缺少！
    MEMORY = "memory"       # 记忆检索
    TASK = "task"          # 任务管理
    SYSTEM = "system"       # 系统工具
    EXECUTION = "execution" # 执行
    ANALYSIS = "analysis"   # 分析
    EVALUATION = "evaluation" # 评估
    AGENT = "agent"        # Agent调用 ← 小写 vs 大写
    MONITORING = "monitoring" # 监控
```

**问题汇总**：
- `tool_maker.yaml` 写 `memory`，实际是 `MEMORY`（全大写枚举）
- `tool_maker.yaml` 提到 `network`，但 **ToolCategory 中没有 NETWORK**
- `tool_maker.yaml` 缺少 `WEB` 分类
- 大小写不一致

#### 1.1.3 隔离配置字段不完整

**tool_maker.yaml 第110-117行**只提到：
```
为工具设置准确的 category 标签
确保工具名称能反映其功能
```

**实际 isolation_policy.yaml 需要配置的字段**：
```yaml
bash_execute:
  isolation: container          # 隔离方式 (container/host)
  execution: command_in_container  # 执行方式 (command_in_container/host_direct)
  fallback: deny                # 降级策略 (deny/allow)
  disk_quota: "100m"           # 磁盘配额
  network: restricted           # 网络策略 (restricted/nat/deny)
```

**遗漏字段**：
- `execution` 字段说明
- `fallback` 策略说明
- `disk_quota`、`network` 等高级配置

#### 1.1.4 工具创建流程遗漏关键步骤

**tool_maker.yaml 提到的流程**：
1. 读取工具代码模板
2. 选择生成策略
3. 编写测试
4. 在工作空间生成代码
5. 运行测试
6. 自验证
7. 评估

**实际 tool_code_template.md 第516-526行要求的完整步骤**：
| 步骤 | 操作 | 文件 |
|------|------|------|
| 1 | 创建工具代码文件 | `src/tools/builtin/{tool_id}.py` |
| 2 | ~~注册到内置工具模块~~ | ~~`src/tools/builtin/__init__.py`~~ （已废弃，自动发现） |
| 3 | ~~运行自动生成脚本更新配置~~ | ~~`scripts/tools/collect_tool_info.py`~~ （已废弃，自动发现） |
| 4 | ~~创建工具专属配置文件~~ | ~~`config/tools/{category}/{tool_id}.yaml`~~ （已废弃） |
| 5 | 配置隔离策略 | `config/isolation/isolation_policy.yaml` |
| 6 | 在需要的 Agent 中引用 | Agent 配置的 `tool_ids` |

**问题**：tool_maker.yaml 原有流程要求注册到 `__init__.py`，但在分支合并场景下会导致合并冲突。**改造后已解决**：通过自动发现机制，无需手动注册。

#### 1.1.5 产出物定义问题

**旧版 tool_maker.yaml 问题**：
- 使用模板变量 `{{tool_type}}/{{tool_id}}.py` 含义模糊
- 产出物需要手动注册到 `__init__.py`

**改造后**：
- 产出物直接在工作空间生成：`{tool_id}.py`
- 通过自动发现机制，无需注册

#### 1.1.6 生成策略问题

**旧版 tool_maker.yaml 问题**：
- 流程中要求 tool_maker "选择生成策略"
- 但策略应该在上级阶段确定，不应由执行者选择

**改造后**：
- tool_maker 接收上级已确定的 strategy 参数
- 只需按策略实现，无需选择

---

## 二、改造方案总览

### 2.1 核心目标

**实现工具的自助发现机制**，使得：
- 工具工匠创建工具文件 → 合并到主分支即可用
- **无需修改 `__init__.py`**
- **无需手动注册**
- **无需运行配置收集脚本**

### 2.2 改造范围

| 改造项 | 文件 | 改造内容 |
|--------|------|----------|
| 1 | `src/tools/builtin/__init__.py` | 改用自动发现替代手动列表 |
| 2 | `src/tools/loader.py` | 增强 DynamicToolLoader 对 `get_all_builtin_tools()` 的支持 |
| 3 | `config/agents/executor/generation/tool_maker.yaml` | 修正分类、更新流程、移除注册步骤 |
| 4 | `config/templates/tool_code_template.md` | 补充 MCP 工具创建说明、更新创建流程 |
| 5 | `config/agents/orchestrator/resource_generator_agent.yaml` | 添加 strategy 字段传递给 tool_maker |

### 2.3 隔离与审查现状（无需改造）

经过审查，现有隔离和审查体系已经完善，**不需要额外改造**。以下是现有机制说明：

#### 隔离机制

隔离由以下三层配置协同工作：

1. **isolation_policy.yaml** - 工具级隔离策略
   - 按工具名精确匹配（`tools:` 优先级最高）
   - 按分类兜底匹配（`categories:` 优先级次之）
   - 未匹配到走 `default:` 默认策略
   - 新工具会通过 category 自动匹配到隔离策略

2. **isolation_config.yaml** - 隔离系统全局配置
   - 工作空间、协调器、提供者、权限策略等
   - `system_config_policy` 要求修改配置前需创建检查点
   - `special_directories["config/"]` 要求写配置文件前需创建检查点

3. **security_rules.yaml** - 安全规则（由 security_check 插件使用）
   - 危险命令拦截（`block`）
   - 受保护路径拦截（`block`）
   - 高风险操作审批（`needs_approval`）
   - SSRF 防护

#### 审查机制

审查由 pipeline 层和上级 agent 共同保障：

1. **l2-subtask.yaml（pipeline 层）**
   - `tool_execute` 前置插件：`security_check`（默认容器隔离 + 需审批）
   - `llm_call` 后置插件：`task_reminder`（提醒评估）
   - `router` 插件：`task_evaluation`（评估退出机制）

2. **resource_generator_agent.yaml（上级 agent 层）**
   - 第5步"审查产出"：file_read 读取工作空间产出，验证格式、字段、质量
   - 第6步"合并到项目"：resource_merge(merge) 合并到项目目录
   - 审查不通过可重试最多 3 次
   - 合并失败可 resource_merge(rollback) 回滚

3. **tool_maker 自身**
   - `hard_constraints`：所有产出必须写入工作空间
   - 无需配置隔离/审查插件（由 pipeline 和上级保障）

#### 结论

- **tool_maker 本身不需要配置隔离策略**：它在工作空间中产出代码，不直接执行危险操作
- **tool_maker 本身不需要配置审查插件**：审查由 `resource_generator_agent` 第5步负责
- **新创建的工具**：会通过 `isolation_policy.yaml` 的 `categories:` 自动匹配到隔离策略
- **安全检查**：由 pipeline 的 `security_check` 插件统一处理

---

## 三、具体改造内容

### 3.1 改造1：自动发现机制 (`src/tools/builtin/__init__.py`)

#### 现状分析
当前 `get_all_builtin_tools()` 使用手动列表：
```python
def get_all_builtin_tools() -> list[Any]:
    _tool_modules = [
        (".file_read", "FileReadTool"),
        (".file_write", "FileWriteTool"),
        (".bash", "BashTool"),
        # ... 手动维护！
    ]
```

#### 改造方案
复用 `DynamicToolLoader._discover_tools()` 的扫描结果：

```python
def get_all_builtin_tools() -> list[Any]:
    """获取所有内置工具实例（使用自动发现机制）
    
    导入失败的模块自动跳过并记录警告。
    """
    from tools.loader import get_dynamic_tool_loader, init_dynamic_tool_loader
    from tools.registry import ToolRegistry
    import logging
    
    _logger = logging.getLogger(__name__)
    
    # 获取或创建动态加载器
    loader = get_dynamic_tool_loader()
    if loader is None:
        registry = ToolRegistry()
        loader = init_dynamic_tool_loader(registry)
    
    # 触发自动发现
    if not loader._discovered:
        loader._discover_tools()
    
    tools: list[Any] = []
    
    # 遍历发现的所有工具类
    for tool_name, (module_path, class_name) in loader._tool_classes.items():
        try:
            import importlib
            mod = importlib.import_module(module_path)
            cls = getattr(mod, class_name)
            
            # 实例化工具（处理需要依赖注入的工具）
            if cls.__init__.__code__.co_argcount > 1:
                # 需要参数的类，跳过（由 register_core_tools 处理）
                continue
            
            tools.append(cls())
            _logger.debug(f"[内置工具] 已加载 {tool_name}")
        except Exception as e:
            _logger.debug(f"[内置工具] 跳过 {tool_name}: {e}")
    
    return tools


def get_all_builtin_tools_with_session() -> list[type]:
    """获取需要数据库会话的内置工具类（不实例化）
    
    这些工具需要在运行时注入 session，不能在这里实例化。
    """
    # 需要 session 的工具列表（手动维护，因为这些需要特殊处理）
    return [
        MemoryTool,
        TaskSubmitTool,
        TaskTool,
        TaskEvaluateTool,
    ]
```

#### 注意事项
- 需要保持 `get_all_builtin_tools_with_session()` 手动列表，因为这些工具需要 session 注入
- 对于不需要注入的工具，自动发现后直接实例化

---

### 3.2 改造2：增强 DynamicToolLoader (`src/tools/loader.py`)

#### 现状分析
`DynamicToolLoader._tool_classes` 字典已存在，但未暴露给外部使用。

#### 改造方案
在 `DynamicToolLoader` 中添加方法，返回发现的工具映射：

```python
def get_discovered_tools(self) -> dict[str, tuple[str, str]]:
    """获取所有已发现的工具映射
    
    Returns:
        dict: {工具名: (模块路径, 类名)}
    """
    if not self._discovered:
        self._discover_tools()
    return self._tool_classes.copy()
```

同时修改 `_discover_tools_in_module` 确保 `self._tool_classes` 被正确填充：

```python
def _discover_tools_in_module(self, module_path: str) -> None:
    """在指定模块中发现工具类"""
    # ... 现有代码 ...
    
    # 确保同时设置 _tool_modules 和 _tool_classes
    self._tool_modules[tool_name] = module_path
    self._tool_classes[tool_name] = (module_path, attr_name)  # 改为元组
```

---

### 3.3 改造3：更新 tool_maker.yaml

#### 改造后的完整配置

```yaml
# -*- coding: utf-8 -*-
# 工具工匠 - 负责工具的生成和修改

config_id: tool_maker
name: 工具工匠
display_name: 工具工匠
description: |
  负责工具的生成和修改。
  核心职责：根据需求生成新工具或修改现有工具。

agent_type: specialized
category: generation
level: L3

system_prompt: |
  # 你是工具工匠

  ## 核心职责
  负责工具的生成和修改，包括：
  - 生成新工具（MCP安装、开源适配、自主开发）
  - 修改现有工具（功能增强、问题修复、性能优化、配置调整、版本更新）

  ## 工作空间规则
  - 所有产出必须写入上级传入的工作空间路径

  ## 执行步骤

  ### 生成新工具 (operation_type: create)

  1. **查看工具代码模板**
     - 模板已通过 static_vars 自动注入到上下文中（变量名：工具代码模板）
     - 直接使用模板了解代码结构、接口定义

  2. **编写测试（TDD）**
     - 根据需求编写单元测试，明确期望行为
     - 使用 file_write 将测试文件写到工作空间
     - 测试应覆盖核心功能、边界情况和错误处理

  3. **在工作空间中生成代码**
     - 按上级指定的 strategy 实现工具代码
     - 使用 file_write 将代码写到工作空间（workspace/{tool_id}.py）
     - **无需修改 `__init__.py`**，系统会自动发现

  ### 修改现有工具 (operation_type: modify)

  1. **读取现有代码**
     - 使用 file_read 读取工作空间中的现有工具代码（上级已复制到工作空间）
     - 工具模板（已自动注入上下文）可用于理解规范

  2. **在工作空间中执行修改**
     - 按上级指定的 modification_type 执行修改
     - 只修改上级指定的部分，不改动其他部分
     - 修改后代码仍须符合模板

  ## 修改类型说明

  | 类型 | 说明 |
  |------|------|
  | enhance | 功能增强 - 添加新功能或能力 |
  | fix | 问题修复 - 修复Bug或逻辑问题 |
  | optimize | 性能优化 - 提升性能或效率 |
  | configure | 配置调整 - 修改配置参数 |
  | update | 版本更新 - 适配新版本或更新依赖 |

  ## 代码规范

  1. **命名规范**
     - 工具ID：snake_case（如 `web_search`）
     - 文件名：snake_case.py（如 `web_search.py`）
     - 类名：PascalCase（如 `WebSearchTool`）

  2. **代码风格**
     - 使用中文注释
     - 遵循项目现有风格
     - 包含类型注解

  3. **工具类继承**
     - 所有工具必须继承 `BuiltinTool` 基类
     - 实现 `get_tool_definition()` 静态方法
     - 实现 `execute()` 异步方法

  4. **隔离配置**
     - 创建新工具时，不需要在代码中设置隔离属性
     - 隔离策略由配置文件 `config/isolation/isolation_policy.yaml` 统一管理
     - 如需特殊隔离，在该文件中添加工具配置即可

  你需要做的是：
  1. 为工具设置准确的 category 标签（对应 ToolCategory 枚举）
  2. 确保工具名称能反映其功能（名称会被用于配置匹配）

  ## ToolCategory 枚举值（必须使用大写）
  - FILE: 文件操作
  - SEARCH: 搜索
  - WEB: Web操作
  - MEMORY: 记忆检索
  - TASK: 任务管理
  - SYSTEM: 系统工具
  - EXECUTION: 命令执行
  - ANALYSIS: 分析
  - EVALUATION: 评估
  - AGENT: Agent调用
  - MONITORING: 监控

# 静态变量（会话级不变，可缓存）
static_vars:
  enabled: true
  items:
    - name: "行为约束"
      type: "rules"
    - name: "文档上下文规则"
      type: "path"
      path: "config/rules/document_context_rules.md"
    - name: "可扩展工具索引"
      content: |
        ## 可扩展工具索引
        以下工具未直接加载，可通过 resource_search 搜索后自动加载使用。
        - enhanced_search: 在文件中搜索文本、代码或文件名
        - web_search: 搜索互联网信息
        - yaml_validate: 验证YAML配置文件的格式和内容
        如需使用上述工具，调用 resource_search(resource_type="tool", query="<工具名>", mode="detailed")，工具将自动加载到当前会话，下一轮即可直接调用。
    - path: "config/templates/tool_code_template.md"
      name: "工具代码模板"

# 动态变量（每轮变化，实时生成）
dynamic_vars:
  enabled: true
  items:
    - name: "当前时间"
      type: "timestamp"

tool_ids:
  - file_read
  - file_write
  - bash
  - fetch
  - enhanced_search
  - resource_search
  - memory
  - task_evaluate
  - task_submit
  - task_manage
  - yaml_validate
  - evaluate

hard_constraints:
  - 必须根据operation_type执行对应逻辑
  - 生成的代码必须符合项目规范
  - 所有产出必须写入工作空间
  - 创建工具只需创建 {tool_id}.py，无需修改 __init__.py

soft_constraints:
  - 代码简洁清晰
  - 包含必要的注释
  - 考虑错误处理
  - 保持向后兼容

input_schema:
  type: object
  properties:
    operation_type:
      type: string
      enum: [create, modify]
      description: 操作类型：create(生成新工具) | modify(修改现有工具)
    tool_id:
      type: string
      description: 工具ID（修改时必填，生成时可选）
    requirements:
      type: object
      description: 需求规格
      properties:
        name:
          type: string
          description: 工具名称
        description:
          type: string
          description: 工具描述
        capabilities:
          type: array
          description: 期望能力列表
        category:
          type: string
          description: 工具分类（使用 ToolCategory 枚举值，大写）
    strategy:
      type: string
      enum: [mcp_install, opensource_adapt, custom_develop]
      description: 生成策略（create时必填）
    modification_type:
      type: string
      enum: [enhance, fix, optimize, configure, update]
      description: 修改类型（modify时必填）
    modification_plan:
      type: object
      description: 修改计划（modify时可选，如无则自行分析制定）
    workspace:
      type: string
      description: 工作空间路径（上级传入）
  required:
    - operation_type
    - requirements
    - workspace

output_schema:
  type: object
  properties:
    operation:
      type: string
    tool_id:
      type: string
    tool_name:
      type: string
    files_changed:
      type: array
    verification_result:
      type: object
    operation_log:
      type: array
  required:
    - operation
    - tool_id
    - files_changed
    - verification_result

version: "1.2.0"
is_active: true
status: "active"
max_iterations: 100
max_reminders: 3
timeout_seconds: 900

# 产出物定义
deliverables:
  - name: "tool_code"
    description: "工具代码文件（写入工作空间）"
    output_path: "{tool_id}.py"
    type: "code"
    required: true

# 推荐评估指标
recommended_metrics:
  - metric_id: file_check
    default_params:
      action: "read"
      path: "src/tools/builtin/{tool_id}.py"

plugins:
  disabled: []
  enabled:
    task_reminder:
      max_reminders: 5
      cooldown_seconds: 180

tags:
  - specialized
  - generation
  - modification
  - tool
  - development
  - L3
  - auto-discovery

metadata:
  author: System
  created_at: '2026-03-15'
  updated_at: '2026-04-16'
  capabilities:
    - tool_generation
    - tool_modification
    - mcp_installation
    - code_generation
    - adapter_development
    - self_verification
    - auto_discovery
```

#### 主要变更点

| 变更项 | 旧版 | 新版 |
|--------|------|------|
| tool_ids | `bash_execute` | `bash` |
| 流程步骤 | 7步，需修改 `__init__.py` | 8步，**移除注册步骤** |
| 产出物 | 2个（代码+配置） | 3个（新增 MCP 配置） |
| category 说明 | 小写、缺漏 WEB | 完整枚举列表、大写 |
| 隔离配置说明 | 简略 | 详细 |
| 注释 | "注册到 `__init__.py`" | "无需修改 `__init__.py`" |

---

### 3.4 改造4：更新 tool_code_template.md

#### 补充内容

在文件末尾添加第九节 MCP 工具创建说明（详见原文档）

---

### 3.5 改造5：更新 resource_generator_agent.yaml

#### 现状分析

当前 `resource_generator_agent` 的 `resource_requirement` 没有 `strategy` 字段，无法将生成策略传递给 `tool_maker`。

#### 改造方案

在 `resource_generator_agent.yaml` 的 `resource_requirement` 中添加 `strategy` 字段：

```yaml
resource_requirement:
  type: object
  description: 资源需求描述
  properties:
    name:
      type: string
      description: 资源名称
    type:
      type: string
      enum: [tool, agent]
      description: 资源类型
    description:
      type: string
      description: 资源描述
    capabilities:
      type: array
      description: 期望能力列表
    context:
      type: object
      description: 触发上下文
    strategy:  # ← 新增字段
      type: string
      enum: [mcp_install, opensource_adapt, custom_develop]
      description: 生成策略（tool 类型时必填）
  required:
    - name
    - type
    - description
```

#### 说明

- `strategy` 字段由 `resource_generator_agent` 在提交任务给 `tool_maker` 时传入
- `research_agent` 在调研阶段确定合适的策略
- `tool_maker` 接收已确定的策略，无需再"选择"

---

### 3.6 改造6：MCP 工具创建说明

MCP 工具创建说明在 `tool_code_template.md` 中添加，详见该模板文件第九节。

---

```markdown
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
```

#### 更新第八节的检查清单

将原来的：

```markdown
| 步骤 | 操作 | 文件 |
|------|------|------|
| 1 | 创建工具代码文件 | `src/tools/builtin/{tool_id}.py` |
| 2 | 注册到内置工具模块 | `src/tools/builtin/__init__.py` |
| 3 | 运行自动生成脚本更新配置 | `scripts/tools/collect_tool_info.py` |
| 4 | 创建工具专属配置文件 | `config/tools/{category}/{tool_id}.yaml` |
| 5 | 配置隔离策略 | `config/isolation/isolation_policy.yaml` |
| 6 | 在需要的 Agent 中引用 | Agent 配置的 `tool_ids` |
```

更新为：

```markdown
| 步骤 | 操作 | 文件 | 备注 |
|------|------|------|------|
| 1 | 创建工具代码文件 | `src/tools/builtin/{tool_id}.py` | 继承 BuiltinTool |
| 2 | ~~注册到内置工具模块~~ | ~~`src/tools/builtin/__init__.py`~~ | **已废弃，自动发现** |
| 3 | ~~运行自动生成脚本~~ | ~~`scripts/tools/collect_tool_info.py`~~ | **已废弃，自动发现** |
| 4 | ~~创建工具专属配置文件~~ | ~~`config/tools/{category}/{tool_id}.yaml`~~ | **已废弃** |
| 5 | 配置隔离策略（可选） | `config/isolation/isolation_policy.yaml` | 仅特殊需求时配置 |
| 6 | 在需要的 Agent 中引用 | Agent 配置的 `tool_ids` | 在 Agent 的 tool_ids 中添加 |
```

---

## 四、隔离策略配置说明

### 4.1 新工具的隔离配置流程

tool_maker 生成新工具时，**隔离配置不在工具代码中设置**，而是由外部配置统一管理：

```
新工具的隔离匹配优先级：
1. tools: 精确匹配工具名 → 最高优先级
2. categories: 按工具 category 匹配 → 兜底策略
3. default: 未匹配到时的默认策略 → container
```

**大多数新工具无需额外配置**：只要在 `get_tool_definition()` 中设置了正确的 `category`，就会自动匹配到 `categories:` 下的隔离策略。

**只有以下情况需要在 `isolation_policy.yaml` 的 `tools:` 下添加精确配置**：
- 需要强制容器隔离且禁止降级（`fallback: deny`）
- 需要特殊资源限制（`disk_quota`、`network`）
- 需要覆盖分类级默认策略

### 4.2 隔离级别说明

| 隔离方式 | execution 执行方式 | 说明 |
|----------|-------------------|------|
| `host` | `host_direct` | 宿主机直接执行，无隔离 |
| `container` | `command_in_container` | 容器隔离执行 |

| 降级策略 | 说明 |
|----------|------|
| `allow` | 隔离失败时自动降级到宿主机执行 |
| `deny` | 隔离失败时拒绝执行，不降级 |

### 4.3 工具类别与隔离速查（基于 isolation_policy.yaml 实际配置）

#### 分类级策略（categories: 兜底匹配）

| ToolCategory 值 | isolation | execution | fallback | 说明 |
|-----------------|-----------|-----------|----------|------|
| `execution` | container | command_in_container | **deny** | 命令执行类，禁止降级 |
| `network` | container | command_in_container | allow | 网络类，NAT 网络 |
| `file` | container | command_in_container | allow | 文件操作类 |
| `search` | container | command_in_container | allow | 搜索类 |
| `analysis` | container | command_in_container | allow | 分析类 |
| `system` | **host** | host_direct | allow | 系统工具类，直接执行 |
| `evaluation` | container | command_in_container | allow | 评估类 |
| `task` | **host** | host_direct | allow | 任务管理类，直接执行 |
| `memory` | **host** | host_direct | allow | 记忆类，直接执行 |

#### 工具级精确策略（tools: 优先匹配）

| 工具名 | isolation | execution | fallback | 特殊配置 |
|--------|-----------|-----------|----------|----------|
| `bash_execute` | container | command_in_container | **deny** | disk_quota: 100m, network: restricted |
| `task_submit` | host | host_direct | allow | |
| `task_manage` | host | host_direct | allow | |
| `task_evaluate` | host | host_direct | allow | |
| `memory` | host | host_direct | allow | |
| `state_update` | host | host_direct | allow | |
| `resource_merge` | host | host_direct | allow | |
| `trigger_setup` | host | host_direct | allow | |
| `human_interaction` | host | host_direct | allow | |

### 4.4 tool_maker 生成工具时的隔离配置指引

#### 默认情况（无需配置）

新工具只要在 `get_tool_definition()` 中正确设置 `category`，系统自动匹配隔离策略：

```python
@staticmethod
def get_tool_definition() -> Tool:
    return Tool(
        name="my_search_tool",
        description="搜索工具",
        category=ToolCategory.SEARCH,  # ← 设置正确的 category
        # ...
    )
```

上例中 `SEARCH` 会自动匹配到 `categories.search` → container 隔离。

#### 特殊情况（需手动配置）

如果新工具需要特殊的隔离策略，需要在 `config/isolation/isolation_policy.yaml` 的 `tools:` 下添加：

```yaml
tools:
  my_special_tool:
    isolation: container
    execution: command_in_container
    fallback: deny
    disk_quota: "100m"
    network: restricted
```

**注意**：此配置不在 tool_maker 的工作空间产出范围内（`config/` 目录有写保护），应由 `resource_generator_agent` 在合并阶段或系统管理员手动配置。

### 4.5 完整隔离配置字段参考

```yaml
tools:
  {tool_name}:
    isolation: container|host           # 隔离方式
    execution: command_in_container|host_direct  # 执行方式
    fallback: deny|allow                # 降级策略
    disk_quota: "{size}"                # 磁盘配额（可选，如 "100m"）
    network: restricted|nat|deny        # 网络策略（可选）
```

### 4.6 安全检查配置（dangerous_operations）

`dangerous_operations` 是工具在 `get_tool_definition()` 中声明的危险操作列表。
系统通过 **隔离级别 + 危险操作** 的组合决定审批结果：

```
隔离级别        危险操作？        结果
────────────────────────────────────────
container       任意              → 自动批准（容器已提供隔离）
host            无                → 自动批准
host            有                → 需要审批
```

#### 代码中的定义

```python
@staticmethod
def get_tool_definition() -> Tool:
    return Tool(
        name="my_tool",
        dangerous_operations=[
            "delete_lines:",    # 删除行操作
            "write:/etc/",      # 写入敏感路径
        ],
    )
```

#### 判断规则

| 工具行为 | dangerous_operations |
|----------|---------------------|
| 访问敏感路径 | 添加 `"操作类型:路径前缀"`（如 `"read:/etc/"`） |
| 有破坏性操作 | 添加具体标识（如 `"rm -rf"`、`"delete_lines:"`） |
| 纯计算/查询 | `[]` |

**注意**：
- `dangerous_operations` 只在 HOST 隔离模式下才触发审批，容器模式自动批准
- 全局安全规则（`security_rules.yaml`）对所有工具自动生效，新工具无需修改

---

## 五、改造验证清单

### 5.1 功能验证

- [ ] 新工具文件创建后，系统能自动发现
- [ ] 新工具能被正确注册到注册表
- [ ] 新工具能正常执行
- [ ] 分支合并后工具正常工作

### 5.2 回归验证

- [ ] 现有内置工具不受影响
- [ ] `get_all_builtin_tools()` 返回正确结果
- [ ] `get_all_builtin_tools_with_session()` 返回正确结果
- [ ] 核心系统工具正常加载

### 5.3 文档更新

- [ ] tool_maker.yaml 更新完成
- [ ] tool_code_template.md 更新完成
- [ ] 本文档存档

---

## 六、涉及文件清单

| 文件 | 改造类型 | 说明 |
|------|----------|------|
| `src/tools/builtin/__init__.py` | 核心改造 | 改用自动发现机制 |
| `src/tools/loader.py` | 辅助改造 | 增强 DynamicToolLoader |
| `config/agents/executor/generation/tool_maker.yaml` | 配置更新 | 修正模板内容 |
| `config/templates/tool_code_template.md` | 文档更新 | 补充 MCP 说明、更新流程 |

---

## 七、附录：ToolCategory 完整定义

```python
class ToolCategory(str, Enum):
    """工具功能分类"""
    
    FILE = "file"           # 文件操作：读取、写入、编辑文件
    SEARCH = "search"       # 搜索：在文件、代码、互联网中搜索
    WEB = "web"            # Web操作：HTTP请求、网页抓取
    MEMORY = "memory"       # 记忆检索：访问长期记忆
    TASK = "task"          # 任务管理：提交、管理、评估任务
    SYSTEM = "system"       # 系统工具：验证、格式化、系统操作
    EXECUTION = "execution" # 执行：Shell命令、代码执行
    ANALYSIS = "analysis"   # 分析：代码分析、诊断
    EVALUATION = "evaluation" # 评估：质量评估、验收标准
    AGENT = "agent"        # Agent调用：触发其他Agent
    MONITORING = "monitoring" # 监控：系统状态监控
```

---

*文档生成时间：2026-04-16*
*文档版本：v1.0*
