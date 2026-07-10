# error_policy 模块文档

## 需求

框架级错误策略处理。根据 `ErrorPolicy` 枚举生成对应的 `PluginResult`，为 PipelineEngine 的插件执行提供统一的错误处理入口。

四种策略：
- **ABORT**：记录错误，停止管道后续执行
- **SKIP**：记录警告，继续执行后续插件
- **FALLBACK**：使用降级状态，继续执行
- **RETRY**：由调用方实现重试循环，重试耗尽后等同于 ABORT

## 逻辑

### 错误策略映射

| 策略 | apply_error_policy 行为 | 返回的 PluginResult |
|------|------------------------|-------------------|
| ABORT | 记录错误，停止管道 | `skip_remaining=True, error=error` |
| SKIP | 记录警告，继续执行 | `error=error` |
| FALLBACK | 使用降级状态 | `state_updates=fallback_state, error=error` |
| RETRY | 重试耗尽后等同于 ABORT | `skip_remaining=True, error=error` |

### 重试逻辑说明

RETRY 的实际重试循环由调用方实现（需要重新调用 `execute`），此函数只负责：
1. 在重试耗尽后生成结果（等同于 ABORT）
2. 调用方应在重试循环耗尽后，以 RETRY 策略调用此函数

## 结构

### 文件清单

| 文件 | 核心符号 | 说明 |
|------|---------|------|
| `error_policy.py` | `apply_error_policy` | 根据错误策略生成 PluginResult |

### 依赖

- `pipeline.types.ErrorPolicy` — 错误策略枚举定义
- `pipeline.plugin.PluginResult` — 插件执行结果类
- 依赖方向：infrastructure → pipeline（正确，基础设施依赖管道核心类型）
