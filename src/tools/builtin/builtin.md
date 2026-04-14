# 内置工具组件

## 一、需求

### 1.1 组件职责

内置工具组件提供系统内置的工具能力：
- 定义内置工具基类（BuiltinTool）
- 提供增强型 Bash 执行工具
- 提供内置评估器（人工审批、Schema验证）

### 1.2 对外接口

- `BuiltinTool`：内置工具抽象基类
- `BashTool`：增强型 Bash 工具
- `HumanEvaluator`：人工评估器
- `SchemaEvaluator`：Schema 验证评估器

### 1.3 依赖

- `tools.registry`：工具注册表
- `tools.executor`：工具执行器
- `core.logging`：日志模块
- `core.config`：配置模块

---

## 二、逻辑

### 2.1 流程设计

#### 内置工具执行流程

```
工具调用 → BuiltinTool.execute()
              ↓
         参数验证
              ↓
         执行工具逻辑
              ↓
         返回结构化结果
```

#### Bash 工具流程

```
Bash命令 → BashTool
              ↓
         安全检查
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  短命令    长进程    交互式
    ↓         ↓         ↓
  直接执行  ProcessManager  InputHandler
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         LogCompressor（日志压缩）
              ↓
         返回执行结果
```

#### 评估器流程

```
评估请求 → Evaluator
              ↓
    ┌─────────┼─────────┐
    ↓                   ↓
HumanEvaluator    SchemaEvaluator
    ↓                   ↓
创建审批请求      验证数据格式
    ↓                   ↓
等待人工决策      返回验证结果
```

### 2.2 数据流向

```
ToolExecutor → BuiltinTool
                   ↓
    ┌──────────────┼──────────────┐
    ↓              ↓              ↓
BashTool    HumanEvaluator  SchemaEvaluator
    ↓              ↓              ↓
ProcessManager  审批服务      Schema验证
    ↓              ↓              ↓
    └──────────────┼──────────────┘
                   ↓
              执行结果
```

### 2.3 配置设计

| 配置项 | 类型 | 说明 |
|--------|------|------|
| bash_timeout | int | Bash 命令超时时间 |
| max_output_size | int | 最大输出大小 |
| allowed_commands | List[str] | 允许的命令白名单 |

### 2.4 错误处理

- 命令执行失败：返回错误信息与退出码
- 超时：终止进程并返回超时错误
- 权限不足：返回权限错误

---

## 三、结构

### 3.1 子组件清单

| 子组件 | 职责 |
|--------|------|
| BuiltinTool | 内置工具抽象基类 |
| BashTool | 增强型 Bash 执行工具 |
| HumanEvaluator | 人工评估器 |
| SchemaEvaluator | Schema 验证评估器 |

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `base.py` | 内置工具基类 |
| `bash/tool.py` | Bash 工具主文件 |
| `bash/types.py` | Bash 工具类型定义 |
| `bash/process_manager.py` | 进程管理器 |
| `bash/input_handler.py` | 输入处理器 |
| `bash/log_compressor.py` | 日志压缩器 |
| `evaluators/__init__.py` | 评估器导出 |
| `evaluators/human_evaluator.py` | 人工评估器 |
| `evaluators/schema_evaluator.py` | Schema 评估器 |
| `evaluators/api_evaluator.py` | API 评估器 |
| `evaluators/bash_evaluator.py` | Bash 评估器 |
| `evaluators/code_evaluator.py` | 代码评估器 |
| `evaluators/file_evaluator.py` | 文件评估器 |
| `evaluators/resource_evaluator.py` | 资源评估器 |
| `evaluators/test_evaluator.py` | 测试评估器 |

### 3.3 测试策略

- 单元测试：各工具方法独立测试
- 集成测试：工具与执行器协作测试
- 覆盖率要求：核心逻辑 ≥85%

---

## 四、实现

### 4.1 base.py

```
BuiltinTool(ABC):
  get_tool_definition() -> Tool: 获取工具定义（静态抽象方法）
  execute(inputs: dict[str, Any]) -> ToolResult: 执行工具（异步抽象方法）
  to_runnable() -> ToolRunnable: 转换为 Runnable
  to_mcp_format() -> dict: 转换为 MCP 格式
  to_llm_format() -> dict: 转换为 LLM 格式
```

### 4.2 evaluators/human_evaluator.py

```
HumanEvaluator(BuiltinTool):
  execute(inputs: dict[str, Any]) -> ToolResult: 执行人工评估
  create_approval_request(context: dict) -> str: 创建审批请求
```

### 4.3 evaluators/schema_evaluator.py

```
SchemaEvaluator(BuiltinTool):
  execute(inputs: dict[str, Any]) -> ToolResult: 执行 Schema 验证
  validate_json(data: dict, schema: dict) -> ValidationResult: JSON 验证
  validate_yaml(data: dict, schema: dict) -> ValidationResult: YAML 验证
```
