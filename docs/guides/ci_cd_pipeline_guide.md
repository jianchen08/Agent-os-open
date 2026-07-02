# CI/CD 流水线指南

> 灵汐系统（Agent OS）测试与持续集成完整手册
> 适用对象：人类开发者、AI Agent、新成员

---

## 目录

1. [快速开始](#1-快速开始)
2. [CI/CD 流水线全览](#2-cicd-流水线全览)
3. [后端测试方法](#3-后端测试方法)
4. [前端测试消息（API 请求示例）](#4-前端测试消息api-请求示例)
5. [日志系统](#5-日志系统)
6. [测试日志拦截与收集](#6-测试日志拦截与收集)
7. [Bug 定位](#7-bug-定位)
8. [测试报告](#8-测试报告)
9. [如何添加新测试](#9-如何添加新测试)
10. [故障排查](#10-故障排查)

---

## 1. 快速开始

### 一句话概括

代码提交 → GitHub Actions 自动执行 Lint → 类型检查 → 测试 → 日志收集 → 报告生成 → Bug 定位。

### 30 秒速查

| 我想… | 命令 |
|--------|------|
| 运行全部测试 | `python -m pytest tests/ -q` |
| 运行单个测试文件 | `python -m pytest tests/test_bug_fixes.py -v` |
| 运行带日志收集的测试 | `python -m pytest tests/ --tb=long -q` |
| 只跑 Lint | `ruff check src/ --config pyproject.toml` |
| 只跑格式检查 | `ruff format --check src/ --config pyproject.toml` |
| 只跑类型检查 | `mypy src/ --config-file pyproject.toml` |
| 查看测试报告 | 打开 `reports/test_report.html` 或读取 `reports/test_report.json` |
| 查看 Lint 报告 | 读取 `reports/ruff_results.json` |

### 环境准备

```bash
# 安装项目及开发依赖
pip install -e ".[dev]"

# 如上面的命令失败，手动安装核心依赖
pip install pytest pytest-asyncio pyyaml rich aiohttp pydantic jsonschema litellm

# 安装代码质量工具
pip install ruff mypy
```

---

## 2. CI/CD 流水线全览

### 2.1 流程图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         触发条件                                        │
│   push 到 main/develop 分支                                            │
│   Pull Request 到 main/develop 分支                                    │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  阶段 1: Lint & Format（lint job）                                     │
│  ┌──────────────────────┐    ┌───────────────────────────┐              │
│  │ Ruff Lint            │───▶│ reports/ruff_results.json  │              │
│  │ ruff check src/      │    └───────────────────────────┘              │
│  └──────────────────────┘                                               │
│  ┌──────────────────────┐                                               │
│  │ Ruff Format Check    │                                               │
│  │ ruff format --check  │                                               │
│  └──────────────────────┘                                               │
│  产出物: lint-results artifact                                          │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  阶段 2: Type Check（typecheck job）                                   │
│  ┌──────────────────────┐                                               │
│  │ mypy src/            │                                               │
│  └──────────────────────┘                                               │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  阶段 3: Tests（test job，依赖 lint 通过）                             │
│  ┌──────────────────────────────────────────────────────┐               │
│  │ pytest tests/                                        │               │
│  │   ├── conftest.py 初始化统一日志系统                  │               │
│  │   ├── 每个 test 自动收集结果到 ReportGenerator       │               │
│  │   ├── 失败 test 触发 BugLocator 定位                 │               │
│  │   └── 会话结束生成 JSON + HTML 报告                   │               │
│  └──────────────────────────────────────────────────────┘               │
│  环境变量: LOG_LEVEL=WARNING, LOG_OUTPUT=console                        │
│  产出物:                                                                │
│    - reports/test_report.json（结构化 JSON）                            │
│    - reports/test_report.html（可视化 HTML）                            │
└─────────────────────────┬───────────────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  阶段 4: CI Summary（summary job）                                     │
│  ┌──────────────────────────────────────────────────────┐               │
│  │ 下载所有 artifact                                    │               │
│  │ 汇总 Lint / TypeCheck / Test 结果                    │               │
│  │ 解析 test_report.json 中的 Bug 定位信息              │               │
│  │ 输出最终摘要到 GitHub Actions 日志                    │               │
│  └──────────────────────────────────────────────────────┘               │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.2 配置文件位置

| 文件 | 用途 |
|------|------|
| `.github/workflows/ci.yml` | CI/CD 流水线定义 |
| `pyproject.toml` | pytest / ruff / mypy 配置 |
| `tests/conftest.py` | 测试框架增强（日志、报告、Bug 定位） |

### 2.3 CI 环境变量

| 变量 | CI 中的值 | 说明 |
|------|----------|------|
| `PYTHON_VERSION` | `3.10` | Python 版本 |
| `REPORT_DIR` | `reports` | 报告输出目录 |
| `LOG_LEVEL` | `WARNING` | 测试期间日志级别 |
| `LOG_OUTPUT` | `console` | 日志输出目标 |

### 2.4 并发控制

```yaml
concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
```

同一分支的新提交会自动取消正在运行的旧流水线。

---

## 3. 后端测试方法

### 3.1 测试目录组织

```
tests/
├── conftest.py              # 全局测试配置（日志初始化 + 报告生成 + Bug 定位）
├── test_utils/              # 测试工具库
│   ├── bug_locator.py       # Bug 自动定位器
│   ├── log_collector.py     # 日志收集器
│   └── report_generator.py  # 结构化报告生成器
├── unit/                    # 单元测试（独立模块，无外部依赖）
├── integration/             # 集成测试（模块间交互）
├── channels/                # 通道层测试（钉钉/飞书/QQ/企微等）
├── connectors/              # 连接器测试
├── tools/                   # 工具测试
├── monitoring/              # 监控模块测试
├── electron/                # Electron 相关测试
├── suites/                  # 测试套件（按功能组织）
├── manual/                  # 手动测试脚本
└── test_*.py                # 各功能模块测试文件（80+ 文件）
```

### 3.2 pytest 常用命令

#### 运行全部测试

```bash
python -m pytest tests/ -q
```

#### 运行特定模块的测试

```bash
# 任务相关测试
python -m pytest tests/test_task2_bugfixes.py tests/test_task_manage_refactor.py -v

# 管道相关测试
python -m pytest tests/test_pipeline_integration.py tests/test_direct_pipeline_routing.py -v

# WebSocket 相关测试
python -m pytest tests/test_websocket_api_imports.py tests/test_websocket_sender_queue.py -v
```

#### 按关键词筛选

```bash
# 只运行包含 "submit" 的测试
python -m pytest tests/ -k "submit" -v

# 只运行包含 "memory" 的测试
python -m pytest tests/ -k "memory" -v
```

#### 按标记筛选

```bash
# pyproject.toml 中定义的标记
# markers = ["integration: LLM integration tests (require --run-integration)"]

# 运行集成测试（需要显式 opt-in）
python -m pytest tests/ --run-integration -v
```

#### 详细输出模式

```bash
# 完整 traceback（推荐用于调试）
python -m pytest tests/test_bug_fixes.py --tb=long -v

# 短 traceback
python -m pytest tests/ --tb=short -q

# 只显示失败的 traceback
python -m pytest tests/ --tb=short --no-header -q
```

### 3.3 CI 中的测试运行方式

CI 流水线中运行测试时排除了以下目录（这些目录的测试需要特殊环境或手动触发）：

```bash
python -m pytest tests/ \
  --tb=long \
  --no-header \
  -q \
  --ignore=tests/suites \
  --ignore=tests/integration \
  --ignore=tests/e2e \
  --ignore=tests/manual \
  --ignore=tests/electron \
  --ignore=tests/monitoring \
  --ignore=tests/tools \
  --ignore=tests/unit \
  --ignore=tests/channels \
  --ignore=tests/connectors \
  --ignore=tests/test_utils
```

### 3.4 pytest 配置（pyproject.toml）

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"          # 自动识别 async 测试函数
testpaths = ["src/tests"]      # 默认测试路径
markers = [
    "integration: LLM integration tests (require --run-integration)",
]
```

> **注意**：`asyncio_mode = "auto"` 意味着所有 `async def test_*()` 函数自动被 pytest-asyncio 处理，无需 `@pytest.mark.asyncio` 装饰器。

### 3.5 conftest.py 中的全局配置

`tests/conftest.py` 在测试运行时自动执行以下操作：

| 阶段 | Hook 函数 | 行为 |
|------|-----------|------|
| 会话开始 | `pytest_sessionstart` | 初始化统一日志系统（WARNING 级别 + 控制台输出）；创建 `ReportGenerator` 实例 |
| 每个测试 | `pytest_runtest_makereport` | 收集测试结果到 `ReportGenerator`；失败时调用 `BugLocator` 自动定位 |
| 会话结束 | `pytest_sessionfinish` | 生成控制台摘要、JSON 报告、HTML 报告 |

#### 排除的测试文件

`conftest.py` 中的 `collect_ignore` 列表会在默认 `pytest` 运行时排除：

```python
collect_ignore = [
    "suites",
    "test_cross_domain_discovery.py",
    "test_directory_generator.py",
    "test_memory_metrics.py",
    "test_pgvector_store.py",
    "test_state_evolution_levels.py",
    "test_task_submit_event_chain.py",
    "test_yaml_error_chain.py",
]
```

### 3.6 可用的 pytest Fixture

`conftest.py` 提供两个全局 fixture：

#### `log_collector` — 日志收集器

```python
def test_example(log_collector):
    log_collector.start(min_level=logging.DEBUG)
    
    # ... 被测逻辑 ...
    
    result = log_collector.get_result()
    assert result.error_count == 0, result.format_errors()
    assert result.warning_count < 5
    log_collector.stop()
```

#### `log_context` — 日志上下文

```python
def test_with_context(log_context):
    log_context.bind(request_id="test-req-123")
    
    # ... 被测逻辑（日志中会携带 rid=test-req-123）...
```

---

## 4. 前端测试消息（API 请求示例）

本节列出前后端通信的关键 API 请求示例。每个示例说明：请求什么、测试什么系统/功能、预期响应。

### 4.1 认证 API

#### 用户登录 — 测试认证系统

```bash
# 测试目标：JWT Token 签发
# 关联后端：src/auth/token.py, src/channels/api/routes_auth.py
curl -X POST http://localhost:8988/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your-password"}'

# 预期响应：
# {
#   "access_token": "eyJhbGciOiJIUzI1NiIs...",
#   "token_type": "bearer",
#   "expires_in": 3600
# }
```

#### 用户注册 — 测试用户创建

```bash
# 测试目标：新用户注册与密码加密
# 关联后端：src/auth/service.py
curl -X POST http://localhost:8988/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username": "newuser", "password": "secure-password", "role": "user"}'

# 预期响应：
# {"id": "user_xxx", "username": "newuser", "role": "user"}
```

> 后续所有请求都需要在 Header 中携带 `Authorization: Bearer <token>`。

### 4.2 会话/线程 API

#### 获取会话列表 — 测试会话管理系统

```bash
# 测试目标：分页获取用户会话列表
# 关联后端：src/channels/api/routes_threads.py
curl http://localhost:8988/api/v1/threads?page=1&page_size=20 \
  -H "Authorization: Bearer <token>"

# 预期响应：
# {
#   "items": [...],
#   "total": 42,
#   "page": 1,
#   "page_size": 20
# }
```

#### 创建新会话 — 测试会话创建与 Agent 绑定

```bash
# 测试目标：创建会话并绑定主 Agent
# 关联后端：src/infrastructure/session/
curl -X POST http://localhost:8988/api/v1/threads \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"title": "测试对话", "agent_id": "lingxi"}'

# 预期响应：
# {"id": "thread_xxx", "title": "测试对话", "agent_id": "lingxi", "created_at": "..."}
```

### 4.3 任务 API

#### 提交任务 — 测试任务创建与状态机初始化

```bash
# 测试目标：创建任务、验证初始状态为 pending
# 关联后端：src/tasks/service.py, src/tasks/state_machine.py
curl -X POST http://localhost:8988/api/v1/tasks \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "编写单元测试",
    "description": "为 auth 模块编写单元测试",
    "agent_name": "coding_agent",
    "priority": 5
  }'

# 预期响应：
# {
#   "id": "abc123def456",
#   "title": "编写单元测试",
#   "status": "pending",
#   "priority": 5,
#   "agent_name": "coding_agent",
#   "created_at": "2026-06-08T12:00:00Z"
# }
```

#### 查询任务状态 — 测试状态追踪

```bash
# 测试目标：验证任务状态流转（pending → running → completed）
# 关联后端：src/tasks/state_machine.py（7 种状态）
curl http://localhost:8988/api/v1/tasks/abc123def456 \
  -H "Authorization: Bearer <token>"

# 预期响应：
# {"id": "abc123def456", "status": "running", ...}
```

#### 评估任务 — 测试评估引擎

```bash
# 测试目标：验证评估指标检查
# 关联后端：src/evaluation/engine.py, src/evaluation/executor.py
curl -X POST http://localhost:8988/api/v1/tasks/abc123def456/evaluate \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "metric_ids": ["file_check", "format_valid"],
    "context": {"output_path": "src/auth/test_new.py"}
  }'

# 预期响应：
# {
#   "task_id": "abc123def456",
#   "results": [
#     {"metric_id": "file_check", "passed": true, "score": 1.0},
#     {"metric_id": "format_valid", "passed": true, "score": 1.0}
#   ]
# }
```

### 4.4 配置管理 API

#### 读取 LLM 配置 — 测试配置读取

```bash
# 测试目标：验证 YAML 配置读取与 API Key 脱敏
# 关联后端：src/channels/api/routes_config.py
curl http://localhost:8988/api/v1/config/llm \
  -H "Authorization: Bearer <token>"

# 预期响应：API Key 字段会被脱敏为 "sk-************xyz" 格式
```

#### 更新并发配置 — 测试配置写入

```bash
# 测试目标：验证配置修改实时写入 YAML 文件
# 关联后端：src/channels/api/routes_config.py → config/system/concurrency_config.yaml
curl -X PUT http://localhost:8988/api/v1/config/concurrency \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"max_concurrent_tasks": 10, "max_concurrent_llm_calls": 5}'

# 预期响应：
# {"success": true, "message": "配置已更新"}
```

### 4.5 Agent API

#### 获取 Agent 列表 — 测试 Agent 注册系统

```bash
# 测试目标：验证 Agent 注册与层级分类
# 关联后端：src/agents/registry.py
curl http://localhost:8988/api/v1/agents \
  -H "Authorization: Bearer <token>"

# 预期响应：返回所有已注册的 Agent（L1/L2/L3 层级）
# [{"config_id": "lingxi", "level": "L1", ...}, ...]
```

### 4.6 工具 API

#### 获取工具列表 — 测试工具注册系统

```bash
# 测试目标：验证内置工具和外部工具注册
# 关联后端：src/channels/api/routes_tools.py
curl http://localhost:8988/api/v1/tools \
  -H "Authorization: Bearer <token>"

# 预期响应：返回所有已注册的工具列表
```

### 4.7 记忆 API

#### 存储记忆 — 测试记忆存储系统

```bash
# 测试目标：验证语义记忆存储
# 关联后端：src/memory/service.py, src/channels/api/routes_memory.py
curl -X POST http://localhost:8988/api/v1/memory \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "semantic",
    "content": "灵汐系统使用 FastAPI 框架",
    "tags": ["architecture", "backend"]
  }'
```

### 4.8 WebSocket 消息

#### 建立聊天连接 — 测试实时消息系统

```javascript
// 前端 WebSocket 连接（fetch API 方式示意）
// 测试目标：验证 WebSocket v3 协议的消息推送
// 关联后端：src/websocket/handler.py, src/pipeline/stream_bridge.py

const ws = new WebSocket('ws://localhost:8988/ws/chat');

// 连接建立后发送消息
ws.onopen = () => {
  ws.send(JSON.stringify({
    type: 'chat_message',
    data: {
      thread_id: 'thread_xxx',
      content: '你好，灵汐'
    }
  }));
};

// 接收流式响应
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  // msg.type 可能的值：
  //   "execution_start"  — 执行开始
  //   "execution_done"   — 执行完成
  //   "interaction_request" — 交互请求（审批/输入）
  //   "interaction_cancelled" — 交互取消
  //   "session_update"   — 会话变更通知
  console.log(msg.type, msg.data);
};
```

#### WebSocket 消息类型一览

| 消息 type | 方向 | 测试什么功能 | 关联后端 |
|-----------|------|-------------|----------|
| `chat_message` | C→S | 主对话 | `pipeline/engine.py` |
| `execution_start` | S→C | 执行开始事件 | `api/websocket/service.py` |
| `execution_done` | S→C | 执行完成事件 | `api/websocket/service.py` |
| `interaction_request` | S→C | 审批交互 | `human_interaction/service.py` |
| `interaction_cancelled` | S→C | 交互取消 | `human_interaction/service.py` |
| `session_update` | S→C | 会话列表刷新 | `channels/api/routes_threads.py` |

---

## 5. 日志系统

### 5.1 统一日志架构

灵汐系统使用 `src/core/logging/` 模块提供统一日志功能。**现有代码中的 `logging.getLogger(__name__)` 无需修改即可自动受益**。

| 文件 | 职责 |
|------|------|
| `src/core/logging/__init__.py` | 公共入口：`setup_logging()` 和 `get_logger()` |
| `src/core/logging/config.py` | `LoggingConfig` 数据类（环境变量驱动） |
| `src/core/logging/formatters.py` | `StructuredFormatter`（人类可读）和 `JsonFormatter`（JSON） |
| `src/core/logging/context.py` | `LogContext` — 请求级追踪字段（线程安全 + asyncio 安全） |

### 5.2 两种输出格式

#### 人类可读格式（StructuredFormatter，默认）

```
2026-06-08 12:00:00 | INFO     | src.pipeline.engine | rid=abc tid=t-001 | 管道启动
2026-06-08 12:00:01 | WARNING  | src.llm.adapter     | rid=abc tid=-     | LLM 调用超时 | duration_ms=5000 model=gpt-4
```

格式模板：
```
%(asctime)s | %(levelname)-8s | %(name)s | %(context)s | %(message)s
```

- `%(context)s` 由 `LogContext` 自动注入，格式为 `rid=xxx tid=xxx sid=xxx`
- 额外字段（如 `duration_ms`、`model`）自动追加到末尾

#### JSON 格式（JsonFormatter，设置 `LOG_JSON=1` 启用）

```json
{
  "timestamp": "2026-06-08T12:00:00.123Z",
  "level": "INFO",
  "logger": "src.pipeline.engine",
  "message": "管道启动",
  "request_id": "abc",
  "task_id": "t-001",
  "session_id": "-",
  "module": "engine",
  "function": "run",
  "line": 42
}
```

### 5.3 JSON 日志字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `timestamp` | string | ISO 8601 UTC 时间戳（如 `2026-06-08T12:00:00.123Z`） |
| `level` | string | 日志级别：DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `logger` | string | Logger 名称（通常为模块路径，如 `src.pipeline.engine`） |
| `message` | string | 日志消息内容 |
| `request_id` | string | 请求追踪 ID（由 `LogContext.bind()` 设置，默认 `-`） |
| `task_id` | string | 任务追踪 ID（默认 `-`） |
| `session_id` | string | 会话追踪 ID（默认 `-`） |
| `module` | string | 模块名 |
| `function` | string | 函数名 |
| `line` | int | 源码行号 |
| `exception` | object | 异常信息（仅 ERROR 及以上级别） |
| `exception.type` | string | 异常类名 |
| `exception.message` | string | 异常消息 |
| `exception.traceback` | array[string] | 完整 traceback |

### 5.4 日志配置方式

#### 方式一：环境变量（推荐用于 CI/CD）

| 环境变量 | 可选值 | 默认值 | 说明 |
|----------|--------|--------|------|
| `LOG_LEVEL` | DEBUG / INFO / WARNING / ERROR / CRITICAL | INFO | 全局日志级别 |
| `LOG_FORMAT` | 格式字符串 | 见 `LoggingConfig` | 自定义格式 |
| `LOG_JSON` | 1 / true | 未设置 | 启用 JSON 输出 |
| `LOG_OUTPUT` | console / file / both | console | 输出目标 |
| `LOG_FILE` | 文件路径 | `logs/app.log` | 日志文件路径 |
| `LOG_FILE_MAX_BYTES` | 整数 | 52428800 (50MB) | 单文件最大字节数 |
| `LOG_FILE_BACKUPS` | 整数 | 5 | 轮转保留文件数 |
| `LOG_THIRD_PARTY_LEVEL` | 同 LOG_LEVEL | WARNING | 第三方库日志级别 |

#### 方式二：代码配置

```python
from src.core.logging import setup_logging
from src.core.logging.config import LoggingConfig

config = LoggingConfig(level=logging.DEBUG, output="both", json_output=True)
setup_logging(config, reset=True)
```

### 5.5 日志上下文追踪

通过 `LogContext`（基于 `contextvars`，线程安全 + asyncio 安全）在日志中注入追踪字段：

```python
from src.core.logging.context import LogContext

# 绑定追踪字段
LogContext.bind(request_id="abc123", task_id="t-001")

# 后续所有日志自动携带 rid=abc123 tid=t-001
# ...

# 临时绑定（退出自动恢复）
with LogContext.scoped(session_id="sess-42"):
    # 日志携带 rid=abc123 tid=t-001 sid=sess-42
    pass
# 自动恢复到 rid=abc123 tid=t-001 sid=-

# 清除全部
LogContext.unbind()
```

### 5.6 第三方库日志降级

以下第三方库的日志级别自动设为 `WARNING`，减少噪音：

- `urllib3`、`httpx`、`httpcore`
- `asyncio`、`aiohttp.access`
- `liteLLM`、`litellm`

可通过 `LOG_THIRD_PARTY_LEVEL` 环境变量调整。

### 5.7 如何查看日志

| 场景 | 方法 |
|------|------|
| 本地开发（控制台） | 日志直接输出到 stdout（默认） |
| 本地开发（文件） | 设置 `LOG_OUTPUT=file` 或 `LOG_OUTPUT=both`，查看 `logs/app.log` |
| CI/CD | GitHub Actions 日志中直接查看 stdout 输出 |
| JSON 日志聚合 | 设置 `LOG_JSON=1 LOG_OUTPUT=file`，用 `jq` 或 ELK 分析 |
| 实时过滤 | `python -m pytest tests/ 2>&1 | grep "ERROR"` |

---

## 6. 测试日志拦截与收集

### 6.1 工作原理

测试框架通过 `tests/conftest.py` 自动集成日志系统：

```
pytest 启动
  │
  ├─ pytest_sessionstart()
  │    └─ setup_logging(level=WARNING, output="console")
  │    └─ 创建 ReportGenerator 实例
  │
  ├─ 每个测试执行
  │    └─ pytest_runtest_makereport() hook
  │         ├─ 收集结果到 ReportGenerator
  │         └─ 失败时 → BugLocator 定位
  │
  └─ pytest_sessionfinish()
       ├─ 输出控制台摘要
       ├─ 生成 reports/test_report.json
       └─ 生成 reports/test_report.html
```

### 6.2 收集的日志数据

#### 会话级（自动）

| 数据 | 来源 | 存放位置 |
|------|------|----------|
| 每个测试用例的通过/失败/跳过状态 | `pytest_runtest_makereport` | `reports/test_report.json` |
| 测试执行耗时（毫秒） | `call.stop - call.start` | `reports/test_report.json` |
| 错误消息（前 500 字符） | `report.longreprtext` | `reports/test_report.json` |
| 完整 traceback | `call.excinfo.getrepr()` | `reports/test_report.json` |
| Bug 定位信息 | `bug_locator.locate_bug()` | `reports/test_report.json` |
| 环境信息 | `_collect_env_info()` | `reports/test_report.json` |

#### 测试级（使用 `log_collector` fixture）

```python
def test_with_log_capture(log_collector):
    # 开始收集（可指定最低级别）
    log_collector.start(min_level=logging.DEBUG)
    
    # ... 执行被测逻辑 ...
    
    result = log_collector.get_result()
    
    # 收集到的数据：
    # result.entries: list[LogEntry]  — 所有日志条目
    # result.error_count: int         — ERROR + CRITICAL 数量
    # result.warning_count: int       — WARNING 数量
    # result.errors(): list[LogEntry] — 仅 ERROR 及以上
    # result.warnings(): list[LogEntry] — 仅 WARNING
    # result.for_logger("src.pipeline") — 按 logger 前缀过滤
    
    assert result.error_count == 0, result.format_errors()
```

#### 每条日志条目（LogEntry）包含

| 字段 | 类型 | 说明 |
|------|------|------|
| `timestamp` | string | 日志时间戳 |
| `level` | string | 日志级别 |
| `logger_name` | string | Logger 名称 |
| `message` | string | 日志消息 |
| `context` | dict | LogContext 快照（request_id/task_id/session_id） |
| `extra` | dict | 用户自定义的 extra 字段 |

### 6.3 配置日志级别

| 场景 | 日志级别 | 配置方法 |
|------|----------|----------|
| CI/CD（默认） | WARNING | 环境变量 `LOG_LEVEL=WARNING`（ci.yml 中已配置） |
| 本地调试 | DEBUG | `LOG_LEVEL=DEBUG python -m pytest tests/` |
| 日志收集器 | 自定义 | `log_collector.start(min_level=logging.DEBUG)` |
| 仅关注错误 | ERROR | `LOG_LEVEL=ERROR python -m pytest tests/` |

### 6.4 日志收集器 API 参考

```python
from tests.test_utils.log_collector import LogCollector, LogCaptureResult

collector = LogCollector()

# 开始收集
collector.start(min_level=logging.DEBUG)  # 默认 WARNING

# 检查状态
collector.active  # True

# 获取结果
result: LogCaptureResult = collector.get_result()
result.error_count    # ERROR + CRITICAL 条数
result.warning_count  # WARNING 条数
result.entries        # 全部 LogEntry 列表
result.errors()       # 仅 ERROR 及以上
result.warnings()     # 仅 WARNING
result.for_logger("src.pipeline")  # 按 logger 前缀过滤
result.format_errors()  # 格式化为可读字符串

# 停止收集
collector.stop()
```

---

## 7. Bug 定位

### 7.1 工作原理

当测试失败时，`BugLocator`（`tests/test_utils/bug_locator.py`）自动分析异常的 traceback，提取以下信息：

```
异常发生
  │
  ├─ 提取 traceback 中的所有帧
  │    └─ 每帧包含：文件路径 + 行号 + 函数名 + 代码片段
  │
  ├─ 判断帧类型
  │    ├─ is_project_code（src/ 目录下）= True → 高概率 bug 位置
  │    └─ is_test_code（tests/ 目录下）= True → 测试代码位置
  │
  ├─ 定位断言失败位置
  │    └─ traceback 最内层帧（最后一个）
  │
  └─ 生成 Bug 候选列表
       └─ 项目源码（src/）但非测试代码的帧，按调用深度倒序排列
```

### 7.2 自动触发的 Bug 定位

在 `conftest.py` 的 `pytest_runtest_makereport` hook 中，当测试失败时自动调用：

```python
# conftest.py 中的关键代码（自动执行，无需手动操作）
if report.failed and call.excinfo and call.excinfo._excinfo:
    from tests.test_utils.bug_locator import locate_bug
    bug_result = locate_bug(call.excinfo._excinfo)
    print(bug_result.summary())  # 输出到控制台
```

### 7.3 Bug 定位报告解读

测试失败时，控制台会输出类似如下的报告：

```
============================================================
🐛 Bug 定位报告
============================================================

📍 断言失败位置: tests/test_bug_fixes.py:45
   函数: test_auth_token_expiry

  42 | token = create_token(user_id="test", expires_in=-1)
  43 | result = validate_token(token)
>>>44 | assert result.is_valid is False
  45 |     # ^^^ 这里断言失败

🎯 高概率 Bug 位置（项目源码，非测试代码）:

  [1] src/auth/token.py:78 in validate_token
  76 |     if payload is None:
  77 |         return TokenResult(is_valid=False)
>>>78 |     if payload.get("exp") > time.time():  # ← 可能是比较逻辑有误
  79 |         return TokenResult(is_valid=True, user_id=payload["sub"])
  80 |     return TokenResult(is_valid=False)

📁 涉及的源码文件:
  - src/auth/token.py
  - src/auth/service.py
============================================================
```

#### 报告各部分含义

| 部分 | 含义 | 如何使用 |
|------|------|----------|
| 📍 断言失败位置 | 测试代码中断言失败的确切行号 | 理解测试预期是什么 |
| 🎯 高概率 Bug 位置 | **最可能包含 bug 的源码文件和行号** | **优先检查这里** |
| 📁 涉及的源码文件 | 调用链涉及的所有项目源码文件 | 完整的排查范围 |

### 7.4 JSON 报告中的 Bug 定位信息

在 `reports/test_report.json` 中，失败用例包含 `bug_location` 字段：

```json
{
  "node_id": "tests/test_bug_fixes.py::test_auth_token_expiry",
  "name": "test_auth_token_expiry",
  "outcome": "failed",
  "error_message": "AssertionError: assert True is False",
  "bug_location": {
    "assertion": "tests/test_bug_fixes.py:44",
    "candidates": [
      {
        "file": "src/auth/token.py",
        "line": 78,
        "function": "validate_token",
        "code": "if payload.get(\"exp\") > time.time():"
      }
    ],
    "source_files": ["src/auth/token.py", "src/auth/service.py"]
  }
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `bug_location.assertion` | string\|null | 断言失败的文件:行号 |
| `bug_location.candidates` | array | Bug 候选位置列表（按概率从高到低） |
| `candidates[].file` | string | 源码文件路径 |
| `candidates[].line` | int | 行号 |
| `candidates[].function` | string | 所在函数名 |
| `candidates[].code` | string | 该行代码内容 |
| `bug_location.source_files` | array[string] | 涉及的所有项目源码文件 |

### 7.5 CI Summary 中的 Bug 定位输出

在 GitHub Actions 的 CI Summary 阶段，会解析 JSON 报告并输出：

```
━━━ 测试报告摘要 ━━━
  总计: 85  通过: 80  失败: 3  跳过: 2
  通过率: 94.1%
🎯 Bug候选: src/auth/token.py:78 in validate_token
🎯 Bug候选: src/tasks/service.py:156 in _create_task
```

### 7.6 手动使用 Bug 定位器

```python
from tests.test_utils.bug_locator import locate_bug

try:
    # ... 被测代码 ...
    pass
except Exception:
    import sys
    result = locate_bug(sys.exc_info())
    print(result.summary())
    # 或访问结构化数据：
    # result.assertion_location  — 断言位置
    # result.bug_candidates      — Bug 候选列表
    # result.source_files        — 涉及的源文件
```

---

## 8. 测试报告

### 8.1 报告生成流程

```
pytest 执行
  │
  ├─ 每个测试用例 → ReportGenerator.add_case()
  │    ├─ 记录 node_id / name / outcome / duration_ms
  │    ├─ 失败时 → BugLocator 定位 → 附加 bug_location
  │    └─ 如有 log_result → 附加 captured_logs
  │
  └─ 会话结束 → 生成三种输出
       ├─ 控制台摘要（to_console）
       ├─ reports/test_report.json（to_json）
       └─ reports/test_report.html（to_html）
```

### 8.2 控制台摘要

测试运行结束后自动输出：

```
============================================================
📊 测试报告摘要
============================================================
  总计: 85  通过: 80  失败: 3  错误: 0  跳过: 2
  通过率: 94.1%
  总耗时: 12345ms

❌ 失败/错误用例:
  - tests/test_bug_fixes.py::test_auth_token_expiry
    错误: AssertionError: assert True is False
    🎯 Bug候选: src/auth/token.py:78 in validate_token

⏱️ 最慢的 5 个测试:
  2345ms  tests/test_integration.py::test_full_pipeline
  1234ms  tests/test_pipeline_integration.py::test_sub_pipeline
  ...
============================================================
```

### 8.3 JSON 报告结构

`reports/test_report.json` 的完整结构：

```json
{
  "timestamp": "2026-06-08T12:00:00+00:00",
  "summary": {
    "total": 85,
    "passed": 80,
    "failed": 3,
    "errors": 0,
    "skipped": 2,
    "pass_rate": 0.941,
    "duration_ms": 12345.6
  },
  "test_cases": [
    {
      "node_id": "tests/test_foo.py::test_bar",
      "name": "test_bar",
      "outcome": "passed",
      "duration_ms": 12.3,
      "file_path": "/path/to/tests/test_foo.py",
      "line_number": 10,
      "error_message": "",
      "traceback": "",
      "captured_logs": ""
    }
  ],
  "environment": {
    "python": "3.10.x",
    "platform": "linux",
    "cwd": "/path/to/project",
    "user": "runner"
  }
}
```

### 8.4 HTML 报告

`reports/test_report.html` 提供可视化界面：

- **通过率进度条**：绿色（通过）和红色（失败）的比例条
- **统计卡片**：总计、通过、失败、错误、跳过、通过率、总耗时
- **用例详情表格**：每条用例的状态、名称、耗时、Bug 定位信息、错误消息
- **环境信息表格**：Python 版本、平台、工作目录、用户

### 8.5 Lint 报告

`reports/ruff_results.json` 包含 Ruff Lint 的检查结果（JSON 格式），可通过 `jq` 分析：

```bash
# 统计各类违规数量
cat reports/ruff_results.json | jq '[.[] | .code] | group_by(.) | map({code: .[0], count: length}) | sort_by(-.count)'
```

---

## 9. 如何添加新测试

### 9.1 最简单的测试

在 `tests/` 目录下创建 `test_<功能>.py` 文件：

```python
"""新功能的测试。"""

import pytest


def test_basic_case():
    """测试基本场景。"""
    result = some_function(input_data="test")
    assert result.status == "expected_status"


@pytest.mark.asyncio
async def test_async_case():
    """测试异步场景（pytest-asyncio 自动处理，无需装饰器）。"""
    result = await some_async_function()
    assert result is not None
```

### 9.2 使用日志收集器的测试

```python
"""带日志收集的测试。"""

import logging

import pytest


def test_with_logs(log_collector):
    """测试功能并验证没有异常日志。"""
    log_collector.start(min_level=logging.WARNING)
    
    # 执行被测逻辑
    result = some_function()
    
    # 验证结果
    assert result.success
    
    # 验证没有错误日志
    logs = log_collector.get_result()
    assert logs.error_count == 0, logs.format_errors()
    log_collector.stop()
```

### 9.3 使用日志上下文的测试

```python
"""带追踪上下文的测试。"""


def test_with_context(log_context):
    """测试时绑定追踪字段。"""
    log_context.bind(request_id="test-123", task_id="task-456")
    
    # 被测逻辑中的日志会携带 rid=test-123 tid=task-456
    result = pipeline_function()
    assert result.ok
```

### 9.4 测试命名规范

| 规范 | 示例 |
|------|------|
| 文件名 | `test_<模块名>.py` |
| 函数名 | `test_<功能>_<场景>` |
| 描述性命名 | `test_task_submit_with_invalid_status` |
| 避免 | `test_1`、`test_stuff` |

### 9.5 测试文件放置位置

| 测试类型 | 放置位置 | 示例 |
|----------|----------|------|
| 模块单元测试 | `tests/test_<模块>.py` | `test_bug_fixes.py` |
| 集成测试 | `tests/integration/` | `integration/test_pipeline.py` |
| E2E 测试 | `tests/e2e/` | `e2e/test_full_flow.py` |
| 工具测试 | `tests/tools/` | `tools/test_bash.py` |
| 通道测试 | `tests/channels/` | `channels/test_feishu.py` |

### 9.6 注意事项

1. **异步测试**：`pyproject.toml` 中 `asyncio_mode = "auto"`，`async def test_*()` 自动被识别
2. **Bug 定位**：测试失败时自动触发，无需手动调用
3. **报告**：所有测试结果自动收集到 `reports/`，无需额外配置
4. **日志级别**：CI 环境默认 WARNING，如需 DEBUG 日志设置 `LOG_LEVEL=DEBUG`

---

## 10. 故障排查

### 10.1 常见问题

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| `ModuleNotFoundError: No module named 'src.xxx'` | `src` 未在 Python 路径中 | `conftest.py` 自动添加；手动运行时从项目根目录执行 |
| `ImportError: cannot import name 'setup_logging'` | 未安装项目依赖 | `pip install -e ".[dev]"` |
| 测试报告未生成 | `reports/` 目录不存在 | CI 中有 `mkdir -p reports`；本地手动创建 |
| 日志刷屏 | 日志级别过低 | 设置 `LOG_LEVEL=WARNING` 或 `log_collector.start(min_level=logging.WARNING)` |
| `pytest-asyncio` 警告 | 未配置 asyncio_mode | 已在 `pyproject.toml` 中配置 `asyncio_mode = "auto"` |
| CI 中测试被跳过 | 目录被 `--ignore` 排除 | 检查 `ci.yml` 中的 `--ignore` 列表 |

### 10.2 调试失败测试的步骤

```
1. 查看 CI 日志中的失败信息
   └─ GitHub Actions → 失败的 job → 展开 "Run Tests" step

2. 查看控制台中的 🐛 Bug 定位报告
   └─ 直接定位到最可能的 bug 位置

3. 下载 CI artifact
   └─ test-reports/test_report.json — 结构化失败详情
   └─ test-reports/test_report.html — 可视化报告

4. 本地复现
   └─ python -m pytest tests/test_xxx.py::test_yyy --tb=long -v

5. 启用 DEBUG 日志
   └─ LOG_LEVEL=DEBUG python -m pytest tests/test_xxx.py --tb=long -v
```

### 10.3 报告文件位置

| 文件 | 位置 | 保留策略 |
|------|------|----------|
| Lint 结果 | `reports/ruff_results.json` | CI artifact，保留 7 天 |
| 测试报告（JSON） | `reports/test_report.json` | CI artifact，保留 14 天 |
| 测试报告（HTML） | `reports/test_report.html` | CI artifact，保留 14 天 |
| 应用日志 | `logs/app.log` | 本地，轮转（50MB × 5 个） |

---

## 附录 A：CI/CD 流水线配置完整参考

> 来源：`.github/workflows/ci.yml`

| 阶段 | Job 名称 | 依赖 | 产出物 |
|------|----------|------|--------|
| Lint & Format | `lint` | 无 | `lint-results/ruff_results.json` |
| Type Check | `typecheck` | 无 | 控制台输出 |
| Tests | `test` | `lint` | `test-reports/test_report.json` + `.html` |
| CI Summary | `summary` | `lint`, `typecheck`, `test` | 控制台摘要 |

## 附录 B：代码质量工具配置参考

> 来源：`pyproject.toml`

| 工具 | 配置项 | 值 |
|------|--------|-----|
| ruff | `line-length` | 120 |
| ruff | `target-version` | py310 |
| ruff | `lint.select` | E, W, F, I, B, C4, UP, N, SIM, PT, RET, ARG, PTH, ERA, PL |
| mypy | `python_version` | 3.10 |
| mypy | `ignore_missing_imports` | true |
| mypy | `check_untyped_defs` | true |
| pytest | `asyncio_mode` | auto |

## 附录 C：相关文件索引

| 文件 | 用途 |
|------|------|
| `.github/workflows/ci.yml` | CI/CD 流水线定义 |
| `pyproject.toml` | pytest / ruff / mypy 配置 |
| `tests/conftest.py` | 测试框架增强 |
| `tests/test_utils/bug_locator.py` | Bug 自动定位器 |
| `tests/test_utils/log_collector.py` | 测试日志收集器 |
| `tests/test_utils/report_generator.py` | 结构化报告生成器 |
| `src/core/logging/__init__.py` | 统一日志系统入口 |
| `src/core/logging/config.py` | 日志配置 |
| `src/core/logging/formatters.py` | 日志格式化器（JSON + 结构化文本） |
| `src/core/logging/context.py` | 日志上下文追踪 |
| `docs/feature_audit_report.md` | 功能审计报告 |
