# channels 模块文档

## 需求

提供管道与外部系统之间的输入/输出适配层，将外部请求转换为管道 state，将管道结果转换为外部响应格式。

## 逻辑

### 适配器架构

```
外部系统 → IInputAdapter.receive() → initial_state → PipelineEngine.run() → final_state → IOutputAdapter.send()
```

- `IInputAdapter`：接收外部请求，返回初始 state 字典
- `IOutputAdapter`：输出管道结果，支持一次性输出（`send`）和流式输出（`send_stream`）

### CLI 通道

CLI 通道是 M1 阶段唯一实现的通道，提供交互式命令行演示：

```
CLIInputAdapter → PipelineEngine → CLIOutputAdapter
        ↑                                  ↓
     stdin读取                        rich Console 输出
```

**CLI Demo 插件**：

| 插件 | 类型 | priority | 功能 |
|------|------|----------|------|
| `DemoLLMCore` | Core | 50 | Echo 回显用户输入 |
| `DemoStopPlugin` | Output | 1 | 检测 should_stop，返回 END 信号 |
| `DemoDefaultRoute` | Output | 99 | 默认返回 END 信号（单轮对话） |

**CLI 路由配置**：

- 输入路由：`should_stop == True` → end（优先级 1）；`True` → core（优先级 10）
- 输出路由：`end` + `should_stop == True`（优先级 1）；`end` + `True`（优先级 99，兜底）

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `input_adapter.py` | `IInputAdapter` | 输入适配器基类（ABC） |
| `output_adapter.py` | `IOutputAdapter` | 输出适配器基类（ABC）+ `send_stream` |
| `__init__.py` | 重导出 `IInputAdapter`, `IOutputAdapter` | 模块入口 |
| `cli/input_adapter.py` | `CLIInputAdapter` | CLI 输入适配器（stdin + quit/exit 检测） |
| `cli/output_adapter.py` | `CLIOutputAdapter` | CLI 输出适配器（rich Console 彩色输出） |
| `cli/cli_main.py` | `DemoLLMCore`, `DemoStopPlugin`, `DemoDefaultRoute`, `CLIApplication`, `main` | CLI 入口 + Demo 插件 |
| `cli/__init__.py` | 重导出 `CLIInputAdapter`, `CLIOutputAdapter` | CLI 子模块入口 |

### 类继承关系

```
IInputAdapter (ABC)
└── CLIInputAdapter

IOutputAdapter (ABC)
└── CLIOutputAdapter

IPlugin (ABC)
├── ICorePlugin ← DemoLLMCore
└── IOutputPlugin ← DemoStopPlugin, DemoDefaultRoute
```

### 运行方式

```bash
$env:PYTHONPATH="src"
python -m channels.cli.cli_main
```
