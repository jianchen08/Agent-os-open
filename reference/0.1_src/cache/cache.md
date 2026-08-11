# 缓存模块

## 需求

### 职责
提供多层缓存策略，支持内存缓存（L1）和 Redis 分布式缓存（L2），为系统提供高性能的数据访问能力。

### 对外接口
- 输入：缓存键、缓存值、TTL（过期时间）
- 输出：缓存值或 None

### 依赖
- 依赖模块：`src.memory.storage`（内存存储）、`src.config.settings`（配置）
- 外部依赖：Redis（可选）

## 逻辑

### 流程设计
```
请求缓存 → 检查 L1 缓存 → 命中返回
           ↓ 未命中
         检查 L2 缓存 → 命中返回并回填 L1
           ↓ 未命中
         返回 None
```

### 数据流向
1. 读取：L1 → L2 → 数据源（调用方负责）
2. 写入：同时写入 L1 和 L2
3. 删除：同时删除 L1 和 L2

### 数据模型
无独立数据模型，使用内存存储和 Redis 存储。

### API设计
#### 模块API
| 接口 | 职责 |
|------|------|
| `get_global_cache()` | 获取全局缓存实例 |
| `cached(key, factory_func, ttl)` | 缓存装饰器函数 |
| `get_redis_manager()` | 获取 Redis 管理器单例 |
| `get_redis_client()` | 获取 Redis 客户端 |

### 配置设计
#### 模块配置
| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| l1_ttl | L1 缓存 TTL（秒） | 300 |
| l2_ttl | L2 缓存 TTL（秒） | 3600 |
| enable_redis | 是否启用 Redis | True |
| redis_url | Redis 连接 URL | 从 Settings 获取 |

#### 环境变量
| 变量名 | 说明 |
|--------|------|
| ENABLE_REDIS_CACHE | 是否启用 Redis 缓存 |

### 错误处理
- Redis 连接失败：降级使用 L1 缓存
- 缓存读写失败：记录日志，不影响主流程

### 安全设计
- 使用 SHA256 替代 MD5 生成缓存键
- pickle 反序列化时记录安全警告
- 敏感数据不应缓存

## 结构

### 组件清单（文件夹 - 抽象说明）
无子组件，为扁平结构。

### 文件清单（代码文件 - 具体接口）

#### __init__.py
职责：模块入口，导出公共接口
暴露接口：
- `RedisManager`：Redis 缓存管理器类
- `get_redis_manager() -> RedisManager`：获取 Redis 管理器单例
- `get_redis_client() -> Redis`：获取 Redis 客户端
- `MultiLevelCache`：多层缓存管理器类
- `get_global_cache() -> MultiLevelCache`：获取全局缓存实例
- `cached(key: str, factory_func, ttl: int | None) -> Any`：缓存装饰器函数
- `cached_function(ttl: int | None, key_prefix: str, key_generator: Callable | None) -> Callable`：函数缓存装饰器
- `cache_result(ttl: int, key_prefix: str) -> Callable`：结果缓存装饰器
- `invalidate_cache(pattern: str) -> Callable`：缓存失效装饰器

#### redis_manager.py
职责：Redis 缓存管理器
暴露接口：
- `RedisManager.__init__(redis_url: str | None)`：初始化管理器
- `RedisManager.connect() -> None`：连接 Redis（async）
- `RedisManager.disconnect() -> None`：断开连接（async）
- `RedisManager.get(key: str, use_json: bool) -> Any | None`：获取缓存值（async）
- `RedisManager.set(key: str, value: Any, ttl: int | timedelta | None, use_json: bool) -> bool`：设置缓存值（async）
- `RedisManager.delete(key: str) -> bool`：删除缓存（async）
- `RedisManager.exists(key: str) -> bool`：检查键是否存在（async）
- `RedisManager.expire(key: str, ttl: int | timedelta) -> bool`：设置过期时间（async）
- `RedisManager.clear_pattern(pattern: str) -> int`：清除匹配模式的缓存（async）
- `RedisManager.get_info() -> dict`：获取 Redis 信息（async）
- `get_redis_manager() -> RedisManager`：获取 Redis 管理器单例
- `get_redis_client() -> Redis`：获取 Redis 客户端（async）

#### multi_level_cache.py
职责：多层缓存管理器
暴露接口：
- `MultiLevelCache.__init__(l1_ttl: int, l2_ttl: int, enable_redis: bool)`：初始化多层缓存
- `MultiLevelCache.get(key: str) -> Any | None`：获取缓存值（L1 -> L2）（async）
- `MultiLevelCache.set(key: str, value: Any, ttl: int | timedelta | None) -> bool`：设置缓存值（async）
- `MultiLevelCache.delete(key: str) -> bool`：删除缓存（async）
- `MultiLevelCache.clear_pattern(pattern: str) -> int`：清除匹配模式的缓存（async）
- `MultiLevelCache.get_stats() -> dict`：获取缓存统计信息
- `get_global_cache() -> MultiLevelCache`：获取全局缓存实例
- `cached(key: str, factory_func, ttl: int | None, cache_instance: MultiLevelCache | None) -> Any`：缓存装饰器函数（async）

#### decorators.py
职责：缓存装饰器
暴露接口：
- `cache_key_generator(*args, **kwargs) -> str`：生成缓存键
- `cached_function(ttl: int | None, key_prefix: str, key_generator: Callable | None) -> Callable`：函数缓存装饰器
- `cache_result(ttl: int, key_prefix: str) -> Callable`：结果缓存装饰器
- `invalidate_cache(pattern: str) -> Callable`：缓存失效装饰器

### 测试策略
#### 模块测试
- 单元测试：缓存读写、TTL 过期、缓存失效
- 集成测试：Redis 连接、多层缓存协同
- Mock 策略：测试环境禁用 Redis

## 实现
→ 见代码文件
