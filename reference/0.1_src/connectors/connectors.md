# 连接器模块（connectors）

> 提供 Agent 与外部 IDE / 编辑器的双向通信能力。

## 需求

- 支持 Agent 与外部 IDE（VSCode、JetBrains 等）的双向通信
- 支持策略模式：多种连接器可注册、按能力匹配、降级处理
- 健康检查与指数退避重连
- 配置热更新（通过 ConfigCenter 订阅）

## 逻辑

### 策略模式

- **BaseConnector**（Strategy）：连接器抽象基类，定义 connect/disconnect/get_context/execute_action/health_check 接口
- **ConnectorRegistry**（Context）：注册表，管理连接器的注册、查询、按能力匹配、优先级排序
- **DegradationManager**（Fallback）：降级管理器，无可用连接器时提供本地降级操作

### 健康检查

- `health_check()`：检测连接状态，ERROR 状态时自动尝试重连
- `_reconnect_with_backoff()`：指数退避重连，可配置最大重试次数和基础延迟

### 配置订阅

- **ConfigSubscriberMixin**：混入类，提供 `subscribe_config`/`unsubscribe_config` 方法，将连接器注册为 ConfigCenter 的配置变更监听者

## 结构

### 文件清单

| 文件 | 职责 |
|------|------|
| `__init__.py` | 模块入口，暴露公共接口 |
| `base.py` | BaseConnector 抽象基类 |
| `registry.py` | ConnectorRegistry 注册表 |
| `degradation.py` | DegradationManager 降级管理器 |
| `config_mixin.py` | ConfigSubscriberMixin 配置订阅混入 |
| `types.py` | 数据类型定义（ConnectorState, ConnectorInfo, ActionResult 等） |
| `adapter_config.py` | 适配器配置加载与状态摘要 |
| `vscode/` | VSCode 连接器实现 |
| `creative/` | 创意工具连接器 |

### 公共接口

见 `__init__.py` 的 `__all__` 列表。

### 测试

| 文件 | 覆盖范围 |
|------|----------|
| `tests/connectors/test_connector_registry.py` | 注册表 CRUD、优先级排序、能力匹配 |
| `tests/connectors/test_connector_types.py` | 数据类型校验 |
| `tests/connectors/test_degradation.py` | 降级策略 |
| `tests/connectors/test_ide_tools.py` | IDE 工具函数 |
| `tests/connectors/test_vscode_connector.py` | VSCode 连接器生命周期 |
| `tests/connectors/test_strategy_health_config.py` | 策略模式 + 健康检查 + ConfigMixin 综合测试 |
