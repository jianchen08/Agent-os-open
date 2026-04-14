# Bash 工具组件

## 一、需求

### 1.1 组件职责

Bash 工具组件提供增强型的命令行执行能力：
- 安全的 Bash 命令执行
- 长时间运行进程管理
- 交互式输入处理
- 日志压缩与摘要

### 1.2 对外接口

- `BashTool`：Bash 工具主类
- `ProcessManager`：进程管理器
- `InputHandler`：输入处理器
- `LogCompressor`：日志压缩器

### 1.3 依赖

- `tools.builtin.base`：内置工具基类
- `core.logging`：日志模块
- `core.config`：配置模块

---

## 二、逻辑

### 2.1 流程设计

#### 命令执行流程

```
命令请求 → BashTool.execute()
              ↓
         安全检查与隔离
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  短命令    长进程    交互式
    ↓         ↓         ↓
  同步执行  异步管理  输入处理
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         日志压缩
              ↓
         返回结果
```

#### 进程管理流程

```
启动进程 → ProcessManager
              ↓
         创建隔离环境
              ↓
         异步执行
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  监控输出  检查状态  超时处理
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         进程结束
              ↓
         清理资源
```

#### 输入处理流程

```
交互请求 → InputHandler
              ↓
         解析输入类型
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  确认型    选择型    文本型
    ↓         ↓         ↓
  等待确认  提供选项  接收输入
              ↓
         返回用户响应
```

#### 日志压缩流程

```
原始日志 → LogCompressor
              ↓
         分析日志内容
              ↓
    ┌─────────┼─────────┐
    ↓         ↓         ↓
  错误提取  关键信息  统计摘要
    ↓         ↓         ↓
    └─────────┼─────────┘
              ↓
         生成压缩摘要
```

### 2.2 数据流向

```
BashTool
    ↓
安全检查 → ProcessManager → 子进程
                ↓
           InputHandler（交互）
                ↓
           LogCompressor
                ↓
           ToolResult
```

### 2.3 数据模型

#### BashAction

| 值 | 说明 |
|----|------|
| EXECUTE | 执行命令 |
| CONTINUE | 继续运行 |
| TERMINATE | 终止进程 |

#### OutputType

| 值 | 说明 |
|----|------|
| STDOUT | 标准输出 |
| STDERR | 标准错误 |
| MIXED | 混合输出 |

#### ProcessInfo

| 字段 | 类型 | 说明 |
|------|------|------|
| pid | int | 进程ID |
| command | str | 执行命令 |
| status | str | 进程状态 |
| start_time | datetime | 启动时间 |

### 2.4 配置设计

| 配置项 | 类型 | 说明 |
|--------|------|------|
| default_timeout | int | 默认超时时间（秒） |
| max_output_lines | int | 最大输出行数 |
| isolation_enabled | bool | 是否启用隔离 |
| allowed_paths | List[str] | 允许访问的路径 |

### 2.5 错误处理

- 命令不存在：返回错误与建议
- 权限不足：返回权限错误
- 超时：终止进程并返回超时信息
- 资源耗尽：清理并返回资源错误

---

## 三、结构

### 3.1 子组件清单

| 子组件 | 职责 |
|--------|------|
| BashTool | Bash 工具主类 |
| ProcessManager | 进程生命周期管理 |
| InputHandler | 交互式输入处理 |
| LogCompressor | 日志压缩与摘要 |

### 3.2 文件清单

| 文件 | 职责 |
|------|------|
| `tool.py` | Bash 工具主类 |
| `types.py` | 类型定义（枚举、数据类） |
| `process_manager.py` | 进程管理器 |
| `input_handler.py` | 输入处理器 |
| `log_compressor.py` | 日志压缩器 |

### 3.3 测试策略

- 单元测试：各组件方法独立测试
- 集成测试：完整命令执行流程测试
- 覆盖率要求：核心逻辑 ≥85%

---

## 四、实现

### 4.1 tool.py

```
BashTool(BuiltinTool):
  execute(params: dict) -> ToolResult: 执行 Bash 命令
  run_command(command: str, timeout: int) -> CommandResult: 运行命令
  check_safety(command: str) -> SafetyCheckResult: 安全检查
```

### 4.2 types.py

```
BashAction(Enum):
  EXECUTE: 执行命令
  CONTINUE: 继续运行
  TERMINATE: 终止进程

OutputType(Enum):
  STDOUT: 标准输出
  STDERR: 标准错误
  MIXED: 混合输出

ProcessInfo(DataClass):
  pid: int
  command: str
  status: str
  start_time: datetime
```

### 4.3 process_manager.py

```
ProcessManager:
  start(command: str, isolation: bool) -> ProcessInfo: 启动进程
  monitor(pid: int) -> ProcessStatus: 监控进程状态
  terminate(pid: int, force: bool) -> bool: 终止进程
  get_output(pid: int) -> str: 获取进程输出
  cleanup(pid: int) -> None: 清理进程资源
```

### 4.4 input_handler.py

```
InputHandler:
  handle_confirmation(prompt: str) -> bool: 处理确认型输入
  handle_choice(prompt: str, options: List[str]) -> str: 处理选择型输入
  handle_text(prompt: str) -> str: 处理文本型输入
```

### 4.5 log_compressor.py

```
LogCompressor:
  compress(log: str, max_lines: int) -> str: 压缩日志
  extract_errors(log: str) -> List[str]: 提取错误信息
  summarize(log: str) -> str: 生成摘要
```
