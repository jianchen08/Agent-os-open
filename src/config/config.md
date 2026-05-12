# 配置模块

## 需求

### 职责
提供统一的配置管理，支持环境变量覆盖、配置文件加载、热更新和 LLM 模型配置管理。

### 对外接口
- 输入：配置键、配置文件路径
- 输出：配置值、配置对象

### 依赖
- 依赖模块：`src.core.exceptions`（异常）、`src.db.models`（数据库模型）
- 外部依赖：Pydantic、YAML、SQLAlchemy

## 逻辑

### 流程设计
```
启动 → 加载 .env 文件 → 加载 YAML 配置 → 环境变量覆盖 → 配置验证 → 提供配置访问
                              ↓
                        同步到数据库（Agent、Workflow、Tool）
```

### 数据流向
1. 配置加载：.env → YAML → 环境变量 → Settings 对象
2. 配置同步：YAML 文件 → 数据库表
3. 热更新：文件变更 → 数据库同步

### 数据模型
#### Settings 配置模型
| 字段 | 类型 | 说明 |
|------|------|------|
| api_host | str | API 主机地址 |
| api_port | int | API 端口 |
| database_url | str | 数据库连接 URL |
| redis_url | str | Redis 连接 URL |
| jwt_secret_key | str | JWT 密钥 |
| openai_api_key | str | None | OpenAI API 密钥 |

#### LLM 配置模型
| 字段 | 类型 | 说明 |
|------|------|------|
| provider | str | 提供商名称 |
| model_name | str | 模型名称 |
| display_name | str | 显示名称 |
| context_window | int | 上下文窗口大小 |

### API设计
#### 模块API
| 接口 | 职责 |
|------|------|
| `get_settings() -> Settings` | 获取配置实例 |
| `get_llm_config() -> LLMConfigManager` | 获取 LLM 配置管理器 |
| `get_model_context_window(model_alias: str) -> int` | 获取模型上下文窗口 |
| `sync_configs(session: AsyncSession) -> dict` | 同步配置到数据库 |

### 配置设计
#### 环境变量
| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| API_HOST | API 主机 | localhost |
| API_PORT | API 端口 | 8888 |
| DATABASE_URL | 数据库 URL | postgresql+asyncpg://... |
| REDIS_URL | Redis URL | redis://localhost:6379/0 |
| OPENAI_API_KEY | OpenAI 密钥 | None |

#### 配置文件
- `.env`：环境变量配置
- `config/models/llm.yaml`：LLM 模型配置
- `config/agents/*.yaml`：Agent 配置
- `config/workflows/*.yaml`：工作流配置

### 错误处理
#### 模块错误码
| 错误码 | 说明 |
|--------|------|
| CONFIG_NOT_FOUND | 配置文件不存在 |
| CONFIG_VALIDATION_ERROR | 配置验证失败 |
| MODEL_NOT_FOUND | 模型别名不存在 |
| PROVIDER_NOT_FOUND | 提供商不存在 |
| ENV_VAR_NOT_FOUND | 环境变量未设置 |

### 安全设计
- 敏感配置（API 密钥）通过环境变量注入
- JWT 密钥生产环境必须修改
- 配置文件不提交敏感信息到版本控制

## 结构

### 组件清单（文件夹 - 抽象说明）
无子组件，为扁平结构。

### 文件清单（代码文件 - 具体接口）

#### __init__.py
职责：模块入口，导出公共接口
暴露接口：
- `ConfigLoader`：配置加载器类
- `sync_configs(session: AsyncSession, config_dir: str) -> dict[str, list[str]]`：同步配置到数据库
- `Settings`：项目配置类
- `get_settings() -> Settings`：获取配置实例
- `get_llm_config() -> LLMConfigManager`：获取 LLM 配置管理器
- `get_model_context_window(model_alias: str) -> int`：获取模型上下文窗口
- `reset_llm_config() -> None`：重置 LLM 配置

#### settings.py
职责：统一配置管理
暴露接口：
- `Settings`：项目配置 Pydantic 模型
- `get_settings() -> Settings`：获取配置实例
- `reset_settings() -> None`：重置配置
- `get_api_base_url() -> str`：获取 API 基础 URL
- `get_frontend_base_url() -> str`：获取前端基础 URL
- `get_ws_base_url() -> str`：获取 WebSocket 基础 URL

#### loader.py
职责：配置文件加载器
暴露接口：
- `ConfigLoader.__init__(config_dir: str | Path, env_file: str | Path | None)`：初始化加载器
- `ConfigLoader.load(filename: str) -> dict[str, Any]`：加载单个配置文件
- `ConfigLoader.load_all() -> dict[str, Any]`：加载所有配置文件
- `ConfigLoader.load_agents(session: AsyncSession, agents_dir: str, include_builtin: bool) -> list[str]`：加载 Agent 配置
- `ConfigLoader.load_workflows(session: AsyncSession, workflows_dir: str) -> list[str]`：加载工作流配置
- `ConfigLoader.load_tools(session: AsyncSession, tools_dir: str) -> list[str]`：加载工具配置
- `ConfigLoader.sync_all(session: AsyncSession) -> dict[str, list[str]]`：同步所有配置

#### llm_config.py
职责：LLM 配置管理器
暴露接口：
- `LLMConfigManager.__init__(config: dict[str, Any] | None)`：初始化管理器
- `LLMConfigManager.get_model(alias: str) -> ModelConfig`：获取模型配置
- `LLMConfigManager.get_default(purpose: str) -> ModelConfig`：获取默认模型
- `LLMConfigManager.get_provider(name: str) -> ProviderConfig`：获取提供商配置
- `LLMConfigManager.get_embedding(name: str) -> EmbeddingConfig`：获取嵌入模型配置
- `LLMConfigManager.list_models() -> list[str]`：列出所有模型
- `LLMConfigManager.list_providers() -> list[str]`：列出所有提供商
- `LLMConfigManager.has_model(alias: str) -> bool`：检查模型是否存在
- `LLMConfigManager.has_provider(name: str) -> bool`：检查提供商是否存在
- `LLMConfigManager.add_model(alias: str, provider: str, model_name: str, display_name: str, ...) -> ModelConfig`：添加模型配置
- `LLMConfigManager.remove_model(alias: str) -> None`：删除模型配置
- `LLMConfigManager.save_to_file() -> None`：保存配置到文件
- `LLMConfigManager.load(key: str) -> dict[str, Any]`：统一加载接口
- `LLMConfigManager.save(key: str, config: dict[str, Any]) -> None`：统一保存接口
- `LLMConfigManager.get_all_keys() -> list[str]`：获取所有可用的配置键
- `LLMConfigManager.has_key(key: str) -> bool`：检查配置键是否存在
- `LLMConfigManager.get_metadata() -> dict[str, Any]`：获取配置管理器的元数据

#### schemas.py
职责：配置数据模型定义
暴露接口：
- `ModelConfig`：模型配置 Pydantic 模型
- `ProviderConfig`：提供商配置 Pydantic 模型
- `EmbeddingConfig`：嵌入模型配置 Pydantic 模型
- `LLMDefaults`：LLM 默认配置 Pydantic 模型
- `EndpointConfig`：API 端点配置 Pydantic 模型
- `RateLimitConfig`：限流配置 Pydantic 模型
- `CORSConfig`：CORS 配置 Pydantic 模型
- `AppConfig`：应用配置 Pydantic 模型
- `ServerConfig`：服务器配置 Pydantic 模型
- `DatabaseConfig`：数据库配置 Pydantic 模型
- `CacheConfig`：缓存配置 Pydantic 模型
- `AuthConfig`：认证配置 Pydantic 模型
- `MemoryConfig`：记忆模块配置 Pydantic 模型
- `LoggingConfig`：日志配置 Pydantic 模型

#### hot_reload.py
职责：配置热更新服务
暴露接口：
- `ConfigFileHandler`：配置文件变化处理器类
- `ConfigHotReloader.__init__(config_dir: str, debounce_seconds: float)`：初始化热更新服务
- `ConfigHotReloader.set_session_factory(session_factory: Callable) -> None`：设置数据库会话工厂
- `ConfigHotReloader.add_callback(callback: Callable) -> None`：添加配置变化回调
- `ConfigHotReloader.start() -> None`：启动服务
- `ConfigHotReloader.stop() -> None`：停止服务
- `ConfigHotReloader.is_running() -> bool`：检查服务状态
- `get_hot_reloader() -> ConfigHotReloader`：获取热更新服务单例
- `init_hot_reloader(config_dir: str, session_factory: Callable | None) -> ConfigHotReloader`：初始化热更新服务

#### exceptions.py
职责：配置模块异常定义
暴露接口：
- `ConfigException`：配置异常基类
- `ConfigNotFoundError`：配置文件不存在异常
- `ConfigValidationError`：配置验证失败异常
- `ModelNotFoundError`：模型不存在异常
- `ProviderNotFoundError`：提供商不存在异常
- `EndpointNotFoundError`：端点不存在异常
- `EnvVarNotFoundError`：环境变量未设置异常

#### api_config.py
职责：API 配置管理
暴露接口：
- API 相关配置常量和工具函数

#### logging.py
职责：日志配置管理
暴露接口：
- 日志配置相关函数和常量

#### registry.py
职责：配置注册表
暴露接口：
- 配置项注册和管理功能

#### validator.py
职责：配置验证器
暴露接口：
- 配置验证相关函数

#### interfaces.py
职责：配置模块接口定义
暴露接口：
- 配置管理器接口定义

#### resource_index.py
职责：资源索引管理
暴露接口：
- 资源索引相关功能

#### system_config.py
职责：系统配置管理
暴露接口：
- 系统级配置管理功能

### 测试策略
#### 模块测试
- 单元测试：配置加载、环境变量替换、配置验证
- 集成测试：配置同步到数据库、热更新
- Mock 策略：Mock 数据库会话

## 实现
→ 见代码文件
