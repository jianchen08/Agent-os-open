# 依赖注入组件

## 需求
### 职责
提供完整的依赖注入（DI）容器实现，支持服务生命周期管理、依赖解析和自动注入。

### 对外接口
- 输入：服务注册配置、服务名称
- 输出：服务实例、容器实例

### 依赖
- 外部依赖：无
- 内部依赖：src.core.exceptions.di（DI异常定义）

## 逻辑
### 流程设计
1. **服务注册**：通过 register 系列方法将服务描述符注册到容器
2. **服务解析**：通过 get 方法根据服务名称获取实例，支持自动依赖注入
3. **生命周期管理**：单例缓存、作用域管理、实例销毁

### 数据流向
```
服务注册 -> ServiceDescriptor 存储 -> get() 请求 -> 生命周期判断 -> 实例创建/缓存获取 -> 返回实例
```

### 数据模型
#### ServiceLifetime（服务生命周期枚举）
| 值 | 说明 |
|---|---|
| SINGLETON | 单例，容器生命周期内只创建一次 |
| TRANSIENT | 瞬态，每次请求创建新实例 |
| SCOPED | 作用域，同一作用域内共享实例 |

#### ServiceDescriptor（服务描述符）
| 字段 | 类型 | 说明 |
|---|---|---|
| service_type | type | 服务类型 |
| lifetime | ServiceLifetime | 生命周期 |
| factory | Callable | 工厂函数 |
| instance | Any | 已存在的实例 |
| is_initialized | bool | 是否已初始化 |

### 错误处理
| 异常类型 | 触发场景 |
|---|---|
| ServiceNotFoundError | 服务未注册 |
| ServiceAlreadyRegisteredError | 服务已存在 |
| CircularDependencyError | 循环依赖检测 |
| InvalidServiceFactoryError | 无效的服务工厂 |
| ServiceValidationError | 服务验证失败 |

## 结构
### 子组件清单
无

### 文件清单（代码文件 - 具体接口）
#### container.py
职责：依赖注入容器核心实现
暴露接口：
- `Container`：依赖注入容器类
  - `register(service_name: str, service_type: type, lifetime: ServiceLifetime, factory: Callable | None, instance: Any | None) -> Container`：注册服务
  - `register_instance(service_name: str, instance: Any) -> Container`：注册实例
  - `register_singleton(service_name: str, service_type: type, factory: Callable | None) -> Container`：注册单例
  - `register_transient(service_name: str, service_type: type, factory: Callable | None) -> Container`：注册瞬态
  - `register_scoped(service_name: str, service_type: type, factory: Callable | None) -> Container`：注册作用域
  - `get(service_name: str) -> Any`：获取服务实例
  - `has(service_name: str) -> bool`：检查服务是否注册
  - `create_scope() -> AsyncContextManager`：创建作用域
  - `dispose() -> None`：销毁容器
  - `list_services() -> dict[str, str]`：列出所有服务
- `ServiceDescriptor`：服务描述符类

#### decorators.py
职责：依赖注入装饰器
暴露接口：
- `inject(**dependencies: str) -> Callable`：依赖注入装饰器
- `singleton(service_name: str | None) -> Callable`：单例服务装饰器
- `transient(service_name: str | None) -> Callable`：瞬态服务装饰器
- `scoped(service_name: str | None) -> Callable`：作用域服务装饰器
- `inject_method(**dependencies: str) -> Callable`：方法依赖注入装饰器

#### lifetime.py
职责：服务生命周期枚举
暴露接口：
- `ServiceLifetime`：服务生命周期枚举类

#### global_container.py
职责：全局容器管理
暴露接口：
- `get_global_container() -> Container`：获取全局容器
- `set_global_container(container: Container) -> None`：设置全局容器
- `reset_global_container() -> None`：重置全局容器
- `dispose_global_container() -> None`：销毁全局容器

#### service_initialization.py
职责：服务初始化注册
暴露接口：
- `register_all_services(container: Container) -> None`：注册所有应用服务
- `create_fastapi_dependency(service_name: str) -> Depends`：创建 FastAPI 依赖

#### exceptions.py
职责：DI 异常定义
暴露接口：
- `DIException`：DI 异常基类
- `ServiceNotFoundError`：服务未找到异常
- `ServiceAlreadyRegisteredError`：服务已注册异常
- `CircularDependencyError`：循环依赖异常
- `InvalidServiceFactoryError`：无效工厂异常
- `ServiceValidationError`：服务验证异常

### 测试策略
#### 组件测试
- 单元测试：容器注册、解析、生命周期管理
- 集成测试：装饰器功能、全局容器管理
- 覆盖率要求：核心逻辑 >= 90%

## 实现
-> 见代码文件：src/core/di/
