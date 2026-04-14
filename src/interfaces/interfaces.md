# interfaces 模块文档

## 需求

提供稳定的公共 API 层，隔离外部消费者与管道内部实现。外部代码应通过 `interfaces` 模块引用插件接口，避免直接依赖 `pipeline.plugin` 内部模块。

## 逻辑

interfaces 模块是纯重导出层，不包含任何自有逻辑。所有符号从 `pipeline.plugin` 和 `pipeline.types` 重新导出。

重导出关系：

| interfaces 文件 | 来源 | 导出符号 |
|----------------|------|---------|
| `input_plugin.py` | `pipeline.plugin` | `IInputPlugin`, `PluginContext`, `PluginResult` |
| `core_plugin.py` | `pipeline.plugin` | `ICorePlugin`, `PluginContext` |
| `output_plugin.py` | `pipeline.plugin` + `pipeline.types` | `IOutputPlugin`, `OutputResult`, `PluginContext`, `RouteSignal` |
| `__init__.py` | 以上三个文件 + `pipeline.types` | 全部 8 个符号 |

## 结构

### 文件清单

| 文件 | 行数 | 说明 |
|------|------|------|
| `__init__.py` | 27 | 统一导出入口，__all__ 包含 8 个符号 |
| `input_plugin.py` | 10 | 输入插件接口重导出 |
| `core_plugin.py` | 10 | 核心插件接口重导出 |
| `output_plugin.py` | 11 | 输出插件接口重导出 |

### 完整 __all__

```python
["ICorePlugin", "IInputPlugin", "IOutputPlugin", "OutputResult",
 "PluginContext", "PluginResult", "ErrorPolicy", "RouteSignal"]
```

### 使用方式

```python
# 推荐：通过 interfaces 引用
from interfaces import ICorePlugin, PluginContext

# 不推荐：直接引用内部实现
from pipeline.plugin import ICorePlugin  # 避免这样做
```
