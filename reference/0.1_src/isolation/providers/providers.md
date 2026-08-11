# 隔离提供者组件

## 需求
### 职责
提供代码执行的隔离环境，支持多种隔离级别（宿主机、容器、沙箱），确保执行安全性和可扩展性。

### 对外接口
- 输入：隔离上下文（IsolationContext）、操作定义
- 输出：执行结果（ExecutionResult）、隔离环境（IsolationEnvironment）

### 依赖
- 依赖模块：src.isolation.types（隔离类型定义）

## 逻辑
### 流程设计
1. 检查提供者可用性（is_available）
2. 创建隔离环境（create_environment）
3. 在环境中执行操作（execute_in_environment）
4. 销毁环境（destroy_environment）

### 数据流向
```
IsolationContext → create_environment → IsolationEnvironment
IsolationEnvironment + Operation → execute_in_environment → ExecutionResult
```

### 错误处理
- RuntimeError：提供者不可用时抛出
- ExecutionResult.error：执行失败时返回错误信息

### 安全设计
- 宿主机模式：无隔离，直接执行
- 容器模式：Docker 容器隔离
- 沙箱模式：进程级隔离，资源限制

## 结构
### 子组件清单（文件夹 - 抽象说明）
| 子组件 | 职责 | 对外接口 | 文档 |
|------|------|----------|------|
| base | 隔离提供者抽象基类 | 输入：隔离上下文 → 输出：隔离环境 | - |
| host_provider | 宿主机隔离提供者 | 输入：操作定义 → 输出：执行结果 | - |
| e2b_provider | E2B沙箱提供者 | 输入：Python代码 → 输出：执行结果 | - |
| cua_provider | Docker容器提供者 | 输入：操作定义 → 输出：执行结果 | - |

### 文件清单（代码文件 - 具体接口）
#### base.py
职责：隔离提供者抽象基类
暴露接口：
- `IsolationProvider`：隔离提供者抽象基类
  - `get_level() -> IsolationLevel`：获取支持的隔离级别
  - `async is_available() -> tuple[bool, str | None]`：检查提供者是否可用
  - `async create_environment(context: IsolationContext) -> IsolationEnvironment`：创建隔离环境
  - `async destroy_environment(env_id: str) -> None`：销毁隔离环境
  - `async execute_in_environment(env_id: str, operation: dict[str, Any]) -> ExecutionResult`：在环境中执行操作
  - `async get_environment_status(env_id: str) -> EnvironmentStatus`：获取环境状态
  - `async health_check() -> tuple[bool, str | None]`：健康检查

#### host_provider.py
职责：宿主机隔离提供者（无隔离）
暴露接口：
- `HostProvider`：宿主机提供者类
  - `get_level() -> IsolationLevel`：返回 HOST 级别
  - 继承 IsolationProvider 所有方法

#### e2b_provider.py
职责：E2B MicroVM 沙箱提供者
暴露接口：
- `E2BProvider`：E2B 沙箱提供者类
  - `__init__(template: str = "base-python", timeout: int = 30, memory_limit: str = "512m")`：初始化
  - `get_level() -> IsolationLevel`：返回 CONTAINER 级别
  - 继承 IsolationProvider 所有方法

#### cua_provider.py
职责：Docker 容器隔离提供者
暴露接口：
- `CuaProvider`：Docker 容器提供者类
  - `__init__(image: str = "python:3.11-slim", memory_limit: str = "2g", cpu_limit: str = "2")`：初始化
  - `get_level() -> IsolationLevel`：返回 CONTAINER 级别
  - 继承 IsolationProvider 所有方法

### 测试策略
#### 组件测试
- 单元测试：各提供者的核心方法
- 集成测试：环境创建、执行、销毁流程
- Mock策略：Docker SDK Mock

## 实现
→ 见代码文件
