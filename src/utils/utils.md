# 工具模块

## 一、需求

### 1.1 模块职责

提供通用工具函数和辅助类：
- 后台任务管理：统一管理后台任务的生命周期
- 数据转换：对象与字典、JSON 之间的转换
- 调试工具：开发友好的调试输出
- 装饰器：常用的装饰器功能
- ID 编码：Base36 编解码和嵌套 ID 生成
- 消息 ID 生成：基于数据库的唯一 ID 生成
- 性能监控：函数执行时间监控

### 1.2 对外接口

```python
# 后台任务管理
class BackgroundTaskManager:
    async def start() -> None
    async def stop() -> None
    async def shutdown(timeout) -> None
    async def start_task(name, coro, on_complete, on_error) -> Task
    async def cancel_task(name, wait, timeout) -> bool
    def get_task(name) -> Task
    def is_task_running(name) -> bool
    def list_tasks() -> list[dict]

def get_global_task_manager() -> BackgroundTaskManager

# 数据转换
class DataConverter:
    @staticmethod
    def to_dict(obj, exclude_none, exclude_private) -> dict
    @staticmethod
    def to_json(obj, **kwargs) -> str
    @staticmethod
    def from_json(json_str) -> Any
    @staticmethod
    def merge_dicts(*dicts, deep) -> dict
    @staticmethod
    def filter_dict(data, include_keys, exclude_keys, exclude_none) -> dict
    @staticmethod
    def flatten_dict(data, separator, prefix) -> dict
    @staticmethod
    def unflatten_dict(data, separator) -> dict

def to_dict(obj, **kwargs) -> dict
def to_json(obj, **kwargs) -> str
def from_json(json_str) -> Any
def merge_dicts(*dicts, **kwargs) -> dict

# 调试工具
def dev_print(*args, **kwargs) -> None
def dev_pprint(obj, indent) -> None
class DevDebug: ...
def debug_enter(func_name) -> None
def debug_exit(func_name, result) -> None
def debug_var(name, value, max_length) -> None
def check_dev_mode() -> bool
def set_dev_mode(enabled) -> None

# 装饰器
def handle_exceptions(default_return, log_error, error_message, reraise)
def retry_on_failure(max_retries, delay, backoff_factor, exceptions)
def log_execution_time(log_level)
def validate_params(**validators)

# ID 编码
def encode_base36(num, width) -> str
def decode_base36(s) -> int
def generate_project_id(project_index) -> str
def generate_task_id(project_id, task_index, parent_task_id) -> str  # 同步版本（id_encoder.py）
def generate_nested_id(parent_id, sequence, prefix) -> str
def parse_nested_id(nested_id) -> dict
def parse_task_id(task_id) -> dict
def exec_id_to_task_id(exec_id) -> str
def task_id_to_exec_id(task_id) -> str

# 执行记录 ID 生成（统一接口）
async def generate_execution_record_id(db, session_id, parent_record_id) -> str
def get_sequence_from_id(message_id) -> int
async def generate_task_id(db, parent_task_id, thread_id) -> str

# 序列号管理
class SequenceManager:
    async def initialize_from_db(db_session) -> None
    async def get_next_sequence(parent_id) -> int
    async def reset(parent_id) -> None
    def get_current_sequence(parent_id) -> int

def get_sequence_manager() -> SequenceManager
async def get_next_sequence(parent_id) -> int

# 性能监控
def monitor_performance(func, name, threshold, log_args) -> Callable
def count_queries(func) -> Callable
def batch_operation(batch_size) -> Callable
```

### 1.3 依赖

- `sqlalchemy`：数据库操作（消息 ID 生成）
- `asyncio`：异步编程（后台任务管理）
- `jinja2`：模板渲染（触发器模块使用）

---

## 二、逻辑

### 2.1 后台任务管理

```
┌─────────────────────────────────────────────────────────────┐
│                    后台任务管理器                            │
├─────────────────────────────────────────────────────────────┤
│  start_task() -> 创建任务 -> 保存引用 -> 添加回调           │
│                                                              │
│  任务完成 -> 调用回调 -> 延迟清理引用                        │
│                                                              │
│  shutdown() -> 取消所有任务 -> 等待完成 -> 清理引用          │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 ID 编码

#### Base36 编码
- 字符集：0-9, a-z（共 36 个字符）
- 用途：生成紧凑的数字编码

#### 嵌套 ID 格式
```
项目 ID: p-{encoded_index}
任务 ID: p-{project}-t-{task_index}
子任务 ID: p-{project}-t-{parent}-{child}
执行记录 ID: exec-{seq1}-{seq2}-{seq3}
```

### 2.3 消息 ID 生成

```
1. 从数据库查询当前会话的最大序列号
2. 递增序列号
3. 检查 ID 是否已存在
4. 如果存在，继续递增重试
5. 返回唯一 ID
```

### 2.4 数据转换

| 方法 | 说明 |
|---|---|
| to_dict | 对象转字典，支持排除 None 值和私有属性 |
| to_json | 对象转 JSON 字符串 |
| from_json | JSON 字符串转对象 |
| merge_dicts | 合并多个字典，支持深度合并 |
| filter_dict | 过滤字典，支持包含/排除键列表 |
| flatten_dict | 扁平化嵌套字典 |
| unflatten_dict | 反扁平化字典 |

### 2.5 装饰器

| 装饰器 | 说明 |
|---|---|
| handle_exceptions | 异常处理，支持默认返回值和重新抛出 |
| retry_on_failure | 重试机制，支持指数退避 |
| log_execution_time | 记录执行时间 |
| validate_params | 参数验证 |

### 2.6 性能监控

| 装饰器 | 说明 |
|---|---|
| monitor_performance | 监控函数执行时间，超过阈值记录警告 |
| count_queries | 统计数据库查询次数 |
| batch_operation | 自动分批处理大量数据 |

---

## 三、结构

### 3.1 组件清单

| 组件 | 职责 |
|---|---|
| BackgroundTaskManager | 后台任务管理器 |
| TaskInfo | 任务信息数据类 |
| DataConverter | 数据转换器 |
| DevDebug | 开发调试上下文管理器 |
| SequenceManager | 序列号管理器 |

### 3.2 文件清单

| 文件 | 说明 |
|---|---|
| `background_tasks.py` | 后台任务管理器 |
| `converters.py` | 数据转换工具 |
| `debug.py` | 调试工具 |
| `decorators.py` | 通用装饰器 |
| `id_encoder.py` | ID 编码工具 |
| `message_id_helper.py` | 消息 ID 生成辅助 |
| `sequence_manager.py` | 序列号管理器 |
| `performance_decorators.py` | 性能监控装饰器 |
| `enum_utils.py` | 枚举安全提取工具 |

### 3.3 测试策略

- 单元测试：测试各工具函数的正确性
- 边界测试：测试 ID 编码的边界情况
- 并发测试：测试后台任务管理器的并发安全性
- 性能测试：测试序列号管理器的性能
