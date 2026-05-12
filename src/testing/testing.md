# testing

## 需求
### 职责
提供测试工具类和测试环境管理，支持端到端测试、集成测试的辅助功能。

### 对外接口
- 输入：测试配置、测试数据需求
- 输出：模拟数据、测试环境、测试监控

### 依赖
- 依赖模块：config（配置）、db（数据库模型）、core（常量）

## 逻辑
### 流程设计
1. 测试前：设置测试环境（数据库、服务）
2. 测试中：创建模拟数据、监控测试事件
3. 测试后：清理测试数据、停止服务

### 数据流向
```
测试启动 → TestEnvironmentManager.setup() → 测试执行 → TestDataManager → 清理
```

### API设计
#### 模块API
| 类/函数 | 职责 |
|------|------|
| `MockEmbeddingService` | 模拟嵌入服务 |
| `MockDatabaseSession` | 模拟数据库会话 |
| `TestEnvironmentManager` | 测试环境管理器 |
| `TestDataManager` | 测试数据管理器 |
| `RealTimeTestMonitor` | 实时测试监控器 |
| `create_mock_user_data()` | 创建模拟用户数据 |
| `create_mock_notification_data()` | 创建模拟通知数据 |
| `create_mock_task_data()` | 创建模拟任务数据 |

### 配置设计
#### 模块配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| test_db_url | 测试数据库URL | postgresql://localhost/test_agent_db |
| backend_port | 后端服务端口 | 8888 |
| frontend_port | 前端服务端口 | 3000 |

### 错误处理
- 服务启动超时：抛出 TimeoutError
- 数据库连接失败：记录错误日志
- 环境清理失败：记录错误日志

## 结构
### 文件清单（代码文件 - 具体接口）
#### utils.py
职责：测试工具类
暴露接口：
- `MockEmbeddingService`：模拟嵌入服务类
  - `async get_embedding(text: str) -> list[float]`：获取嵌入向量
  - `async get_embeddings(texts: list[str]) -> list[list[float]]`：批量获取嵌入向量
- `MockDatabaseSession`：模拟数据库会话类
  - `async add(obj)`：添加对象
  - `async commit()`：提交事务
  - `async rollback()`：回滚事务
  - `async close()`：关闭会话
  - `async flush()`：刷新会话
  - `async refresh(obj)`：刷新对象
  - `is_committed: bool`：是否已提交（属性）
  - `is_rolled_back: bool`：是否已回滚（属性）
- `TestEnvironmentManager`：测试环境管理器
  - `async setup_test_database()`：设置测试数据库
  - `async start_backend_service()`：启动后端服务
  - `async start_frontend_service()`：启动前端服务
  - `async wait_for_services_ready(timeout: int = 60)`：等待服务就绪
  - `async cleanup()`：清理测试环境
- `TestDataManager`：测试数据管理器
  - `create_test_user() -> dict[str, str]`：创建测试用户
  - `create_test_task() -> dict[str, Any]`：创建测试任务
  - `async verify_task_in_database(task_id: str, user_id: str) -> dict[str, Any]`：验证任务
  - `cleanup_test_data()`：清理测试数据
- `RealTimeTestMonitor`：实时测试监控器
  - `log_event(event_type: str, message: str, data: dict | None = None)`：记录测试事件
  - `get_test_timeline() -> list[dict]`：获取测试时间线
  - `generate_timeline_report() -> str`：生成时间线报告
- `create_mock_user_data(user_id: str = "test_user") -> dict[str, Any]`：创建模拟用户数据
- `create_mock_notification_data(notification_id: str = "test_notification", user_id: str = "test_user", title: str = "测试通知", message: str = "这是一个测试通知") -> dict[str, Any]`：创建模拟通知数据
- `create_mock_task_data(task_id: str = "test_task", status: str = "pending", task_type: str = "test") -> dict[str, Any]`：创建模拟任务数据

#### setup.py
职责：测试环境设置
暴露接口：
- `setup_test_environment() -> Settings`：设置测试环境，返回配置对象
- `setup_test_database() -> AsyncGenerator`：设置测试数据库，返回会话管理器
- `create_test_client() -> TestClient`：创建同步测试客户端
- `create_async_test_client() -> AsyncClient`：创建异步测试客户端
- `TestEnvironment`：测试环境管理器类
  - `client`：获取同步测试客户端（属性）
  - `async_client`：获取异步测试客户端（属性）
  - `async get_db_session()`：获取数据库会话
  - `async cleanup()`：清理资源
- `get_test_settings() -> Settings`：获取测试配置
- `get_test_urls() -> dict`：获取测试URL字典
- `print_test_info(test_name: str)`：打印测试信息
- `print_test_result(test_name: str, success: bool, message: str = "")`：打印测试结果

### 测试策略
#### 模块测试
- 单元测试：工具函数、模拟类
- 集成测试：环境管理、数据管理
- Mock策略：外部服务 Mock

## 实现
→ 见代码文件
