# plugins 模块文档

## 需求

Agent OS 的插件体系分为两大类：

### 1. 管道插件（`src/plugins/shared/`）

管道执行过程中的处理节点，按生命周期阶段分为三子类：

1. **Input 插件**（22 个）：准备管道输入数据，如上下文构建、知识注入、参数注入、提示词构建等
2. **Output 插件**（21 个）：处理管道输出，如停止检查、错误分析、任务评估、委派等待策略等
3. **Core 插件**（3 个）：执行核心逻辑，LLM 调用和工具执行

所有插件遵循 `IInputPlugin` / `ICorePlugin` / `IOutputPlugin` 接口。

### 2. Sidecar 插件（根级 `plugins/shared/`）

独立运行的系统服务，通过 MCP 协议提供工具能力：

- **system 分类**：memory、approval、evaluation、review
- **tools 分类**：builtin_tools、channel_ws、triggers

## 目录结构

```
src/plugins/                     # 管道插件（Python 包，pip install -e .）
├── shared/                      # 全局共享插件（所有租户共享）
│   ├── input/                   # 22 个 Input 插件
│   ├── output/                  # 21 个 Output 插件
│   └── core/                    # 3 个 Core 插件
├── tenants/                     # 租户级管道插件
│   └── default/                 # 默认租户（预留扩展）
│       ├── input/
│       ├── output/
│       └── core/
├── input/__init__.py            # 兼容性 shim → shared/input
├── output/__init__.py           # 兼容性 shim → shared/output
├── core/__init__.py             # 兼容性 shim → shared/core
└── hot_reload.py                # 热重载管理器

plugins/                         # Sidecar 插件 + 统一扫描器
├── shared/                      # 全局共享 Sidecar 插件
│   ├── system/                  # 系统服务类
│   │   ├── memory/
│   │   ├── approval/
│   │   ├── evaluation/
│   │   └── review/
│   ├── tools/                   # 工具类
│   │   ├── builtin_tools/
│   │   ├── channel_ws/
│   │   └── triggers/
│   └── pipeline/                # 管道插件副本（参考）
│       ├── input/
│       ├── output/
│       └── core/
├── tenants/                     # 租户级 Sidecar 插件
│   └── default/
│       ├── system/
│       ├── tools/
│       └── pipeline/
├── plugin_scanner.py            # 统一插件扫描器
├── sdk/                         # 插件开发 SDK
└── test_system_plugins.py       # 系统插件测试
```

## 加载机制

### 管道插件发现

`pipeline/config.py._discover_plugin_class(name)` 按以下优先级搜索：

1. **新路径**：`plugins.shared.input.{name}.plugin` / `plugins.shared.output.{name}.plugin`
2. **旧路径回退**：`plugins.input.{name}.plugin` / `plugins.output.{name}.plugin`（通过 shim 重定向到新位置）

### YAML class 路径迁移

`_resolve_plugin_class()` 中的 `_prefix_migrations` 自动将旧前缀映射到新前缀：
- `plugins.input.` → `plugins.shared.input.`
- `plugins.output.` → `plugins.shared.output.`
- `plugins.core.` → `plugins.shared.core.`

### 统一扫描器

`plugins/plugin_scanner.py` 提供：
- `scan_pipeline_plugins(tenant_id)` — 扫描管道插件（共享 + 租户覆盖）
- `scan_sidecar_plugins(tenant_id)` — 扫描 Sidecar 插件
- `resolve_pipeline_plugin_module(name, category, tenant_id)` — 解析模块路径

### 兼容性保障

三层兼容：
1. **Import 层**：`src/plugins/input/__init__.py` 等 shim 将旧路径重定向到新位置
2. **配置层**：`_migrated_paths` + `_prefix_migrations` 自动转换 YAML 中的旧路径
3. **发现层**：`_discover_plugin_class` 先搜新路径，回退旧路径

## 逻辑

### State 命名空间约定
- Input 插件写 `context.*`、`knowledge.*`、`prompt.*`、`tool.*`、`security.*`、`reasoning.*` 命名空间
- Output 插件写 `router.*`、`track.*`、`memory.*`、`evaluation.*`、`error_analysis` 命名空间

### 委派等待策略（M11a）
- **FireAndForgetPlugin**：不等待，适合不关心子管道结果的场景
- **EventCallbackPlugin**：事件驱动挂起，适合异步事件恢复场景

## 结构

### 子文件夹

| 子文件夹 | 文档 | 说明 |
|---------|------|------|
| `shared/core/` | [CORE.md](shared/core/CORE.md) | LLM 调用 + 工具执行核心插件 |
| `shared/input/` | 无独立文档（22 个插件） | Input 插件目录 |
| `shared/output/` | [OUTPUT.md](shared/output/OUTPUT.md) | Output 插件目录（含 M11a 委派策略） |
