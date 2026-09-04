# CI/CD 流水线指南

> 灵汐系统（Agent OS）测试与持续集成完整手册
> 适用对象：人类开发者、AI Agent、新成员

> **现状说明（2026-08）**：lint/type 车道由
> `scripts/run_gates.py` 统一持有（如 `python scripts/run_gates.py --filter sdk-lint,sdk-mypy`），
> 全量格式化为 `ruff format .` / `ruff check .`——本文速查命令与流水线图已按此对齐。
> 第 4 章 API 示例对准内核 :9100 的现役 `/api/v1/*` 路由，路径与端口以内核实际路由为准。

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
| 只跑 Lint | `ruff check . --config pyproject.toml` |
| 只跑格式检查 | `ruff format --check . --config pyproject.toml` |
| 只跑类型检查 | `python scripts/run_gates.py --filter sdk-mypy`（mypy 车道经 run_gates 统一持有） |
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

机械门禁的单一事实源是 `scripts/run_gates.py`（27 个门禁：CI 跑穷尽集，本地 `--mode fast` 跑廉价检查）；GitHub Actions 只是它的宿主之一。

```
┌─────────────────────────────────────────────────────────────────────────┐
│  触发条件（.github/workflows/ci.yml）                                   │
│   push / PR 到 main 与 dev/* 分支；workflow_dispatch 手动                │
│   concurrency: ci-${{ github.ref }}，同分支新提交自动取消旧流水线        │
└─────────────────────────┬───────────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────────┐
│  Rust 族（cargo + sccache）                                             │
│   rust-lint（fmt + clippy） / rust-build / rust-test                    │
│   rust-coverage（lcov + 基线检查） / rust-deny（依赖许可与漏洞门禁）     │
├─────────────────────────────────────────────────────────────────────────┤
│  Python 族（uv.lock 冻结安装）                                          │
│   python-lint（ruff + run_gates sdk-lint/sdk-mypy 等车道）              │
│   python-test（SDK pytest：run_gates --filter sdk-test）                │
│   python-coverage（run_gates --filter plugins-coverage-pairing,         │
│     channel-copy-guard,plugins-coverage,channel-per-file-coverage,      │
│     plugins-diff-coverage,plugins-mypy-baseline：                       │
│     插桩 + 失败数基线锁 + mypy 基线(0) + 改动行 diff 100%；             │
│     整体覆盖率基线 2026-09-01 起挂起观察 --skip，插桩照跑）             │
│   python-heavy-suites（重型套件，失败数基线锁）                          │
├─────────────────────────────────────────────────────────────────────────┤
│  前端 / 桌面                                                            │
│   frontend-endpoints-sync（端点生成物一致性） / frontend-test（vitest） │
│   frontend-e2e（Playwright）/ electron-compile（桌面壳编译门禁）         │
├─────────────────────────────────────────────────────────────────────────┤
│  专项门禁                                                               │
│   timing（时序不变量 @pytest.mark.timing）/ pre-commit / tdd-gate       │
│   / traceability-gate（追溯标记）                                       │
├─────────────────────────────────────────────────────────────────────────┤
│  all-checks-passed（汇总门：以上全部通过才绿）                           │
└─────────────────────────────────────────────────────────────────────────┘

独立 workflow（.github/workflows/e2e.yml）：
  check-key → e2e（编译 Linux 内核 + 插件 venv/cdylib + tests/e2e_02，
  真实 WebSocket/LLM 链路；schedule 定时 + push 触发，API key 就绪才跑）
```

### 2.2 配置文件位置

| 文件 | 用途 |
|------|------|
| `.github/workflows/ci.yml` | 主 CI（18 个 job，见 §2.1） |
| `.github/workflows/e2e.yml` | e2e workflow（check-key + 真实 LLM e2e） |
| `scripts/run_gates.py` | 机械门禁单一事实源（27 门禁；`--mode fast/kernel/plugins/frontend/all` 或 `--filter <id>`） |
| `pyproject.toml` | pytest / ruff / mypy 配置 |
| `tests/conftest.py` | 测试框架增强（日志、报告、Bug 定位） |
| `.github/mypy-baseline.txt` | mypy 错误基线（现值 0，只减不增） |
| `.github/pytest-failure-baseline.txt` | 红测失败数基线锁（现值 plugins-coverage 0 / heavy 0，只减不增） |

### 2.3 CI 环境变量

ci.yml 的 workflow 级全局 env（各 job 可按需追加）：

| 变量 | 说明 |
|------|------|
| `CARGO_TERM_COLOR` | cargo 输出着色 |
| `RUST_BACKTRACE` | Rust panic 栈回溯 |
| `RUSTC_WRAPPER` / `SCCACHE_DIR` | sccache 编译缓存（best-effort） |
| `AGENTOS_GATE_VERBOSE` | run_gates 门禁详细输出 |

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
├── channels/                # 通道层测试
├── connectors/              # 连接器测试
├── tools/                   # 工具测试
├── monitoring/              # 监控模块测试
├── multimodal/              # 多模态测试
├── suites/                  # 测试套件（按功能组织）
├── plugins/                 # 插件车道镜像测试（CI 必跑，分层 marker 强制）
├── gates/                   # 门禁自检测试
├── e2e_02/                  # 0.2 端到端测试（真实内核 + WebSocket/LLM，e2e workflow 跑）
├── manual/                  # 手动脚本预留目录（需手动环境，当前为空，不入默认收集）
└── test_*.py                # 各功能模块测试文件（60+ 文件）
```

### 3.2 pytest 常用命令

#### 运行全部测试

```bash
python -m pytest tests/ -q
```

#### 运行特定模块的测试

```bash
# 任务域相关测试
python -m pytest tests/test_task_submit_params.py tests/test_delete_task_cascade_pipeline.py -v

# LLM 轨道相关测试
python -m pytest tests/test_llm_core_providers.py tests/test_llm_adapter_call_streaming.py -v

# 安全守卫相关测试
python -m pytest tests/test_security_check_isolation.py tests/test_security_check_permission_modes.py -v
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
# pyproject.toml 中注册了 12 个标记（unit/integration/e2e/timing 等）
# 用 -m 按标记筛选

# 只运行单元测试
python -m pytest tests/ -m "unit" -v

# 只运行集成测试
python -m pytest tests/ -m "integration" -v
```

> **注意**：项目启用了 `--strict-markers`，未在 `pyproject.toml` 注册的标记会直接报错。

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

CI 不再单独拼 pytest 命令——Python 测试车道统一经 `scripts/run_gates.py` 持有（门禁 id 与命令见该文件）：

| CI job | 门禁/命令 | 覆盖内容 |
|--------|----------|---------|
| `python-test` | `run_gates.py --filter sdk-test` | SDK pytest |
| `python-coverage` | `run_gates.py --filter plugins-coverage-pairing,channel-copy-guard,plugins-coverage,channel-per-file-coverage,plugins-diff-coverage,plugins-mypy-baseline` | 插件测试全量插桩 + 失败数基线锁 + mypy 基线 + 改动行 diff 100% |
| `python-heavy-suites` | 重型套件车道（失败数基线锁） | 慢速/重环境套件 |
| `timing` | `@pytest.mark.timing` 用例 | 时序不变量（事件顺序/间隔/超时边界） |

- **失败数基线锁**：失败数 > `.github/pytest-failure-baseline.txt` 基线 → CI 红（拦截增长）；≤ 基线 → 绿（允许 pre-existing 持平，修好后收紧基线并 commit 留归因）。当前基线：plugins-coverage 0 / heavy 0。
- **mypy 基线**：`.github/mypy-baseline.txt` 现值 0，只减不增。
- **整体覆盖率基线**：2026-09-01 用户裁定暂时挂起观察（基线检查脚本以 `--skip` 挂起，插桩与 coverage.xml 产出照跑，diff/车道门禁仍消费）；恢复 = 去掉 `scripts/run_gates.py` 对应门禁命令中的 `--skip` 参数。
- e2e 与真实 LLM 链路不在 ci.yml，在独立的 `.github/workflows/e2e.yml`（`check-key` 前置 → `e2e` job 跑 `tests/e2e_02`，编译 Linux 内核 + 插件 venv/cdylib 后真机验证；schedule 定时 + push 触发）。

> 完整 job 定义与触发条件以 `.github/workflows/ci.yml` / `e2e.yml` 为准。

### 3.4 pytest 配置（pyproject.toml）

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"          # 自动识别 async 测试函数
testpaths = ["tests"]          # 默认测试路径
addopts = ["--strict-markers"] # 未注册的 marker 直接报错
markers = [
    "unit: 单元测试",
    "integration: 集成测试",
    "e2e: 端到端测试",
    "timing: 关键时序不变量（事件顺序/间隔/超时边界），独立 stage 阻塞门禁",
    "core: 核心单元测试",
    "m6: M6插件测试",
    "stage: Stage闭环测试",
    "cli: CLI测试",
    "memory: 内存经验测试",
    "task: Task E2E测试",
    "agent: Agent测试",
    "llm: LLM相关测试",
]
```

> **注意**：完整 markers 列表以 `pyproject.toml` 为准（共 12 个）。`asyncio_mode = "auto"` 意味着所有 `async def test_*()` 函数自动被 pytest-asyncio 处理，无需 `@pytest.mark.asyncio` 装饰器。`--strict-markers` 要求所有 `@pytest.mark.xxx` 必须在上方 markers 列表中注册，否则报错。

### 3.5 conftest.py 中的全局配置

`tests/conftest.py` 在测试运行时自动执行以下操作：

| 阶段 | Hook 函数 | 行为 |
|------|-----------|------|
| 会话开始 | `pytest_sessionstart` | `logging.basicConfig(level=WARNING)`（不用 `setup_logging`——会破坏 pytest capture 的临时文件）；创建 `ReportGenerator` 实例 |
| 每个测试 | `pytest_runtest_makereport` | 收集测试结果到 `ReportGenerator`；失败时调用 `BugLocator` 自动定位 |
| 会话结束 | `pytest_sessionfinish` | 生成控制台摘要、JSON 报告、HTML 报告 |

#### 排除的测试目录

`tests/manual/` 预留给需手动环境（kernel + LLM key）的脚本（无断言或命中真实外部 API），不进默认收集，避免污染 `pytest tests/`（当前为空目录）；`tests/channels/`、`tests/suites/` 等集成测试正常收集。

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

本节列出前端与内核通信的关键 API 示例，全部对准内核 `:9100` 的现役 REST 面（`/api/v1/*`，
路由清单以 `kernel/crates/api` 为准）；实时消息走 WebSocket，契约见
[streaming-protocol.md](streaming-protocol.md)。

### 4.1 认证 API

#### 用户登录 — 测试认证系统

```bash
# 测试目标：JWT Token 签发（内核 user-admin：kernel/crates/user-admin）
curl -X POST http://localhost:9100/api/v1/auth/login   -H "Content-Type: application/json"   -d '{"username": "admin", "password": "your-password"}'

# 预期响应：携带 access_token（后续请求 Header 带 Authorization: Bearer <token>）
```

#### 用户注册 — 测试用户创建

```bash
# 测试目标：新用户注册
curl -X POST http://localhost:9100/api/v1/auth/register   -H "Content-Type: application/json"   -d '{"username": "newuser", "password": "secure-password"}'

# 预期响应：返回新建用户信息（id / username）
```

### 4.2 会话 API

#### 获取会话列表

```bash
# 测试目标：获取当前用户会话列表
curl "http://localhost:9100/api/v1/sessions"   -H "Authorization: Bearer <token>"

# 预期响应：会话列表（thread 坐标；前端据此渲染侧边栏）
```

#### 创建会话并绑定 Agent

```bash
# 测试目标：创建会话（agent_id 写入执行上下文 initial_state）
curl -X POST http://localhost:9100/api/v1/sessions   -H "Authorization: Bearer <token>"   -H "Content-Type: application/json"   -d '{"title": "测试对话", "agent_id": "agentos"}'
```

#### 切换会话 Agent

```bash
# 测试目标：切换执行上下文键（下一轮管道整体切人设/工具/约束）
curl -X PATCH http://localhost:9100/api/v1/sessions/{id}/agent   -H "Authorization: Bearer <token>"   -H "Content-Type: application/json"   -d '{"agent_id": "code_writer_agent"}'
```

### 4.3 任务与管道

任务没有独立 REST 创建口——**任务创建/派发经 `task_submit` 工具**（LLM 工具调用与
前端面板表单同一条工具通道，见 ARCHITECTURE「任务系统与评估闸门」）；评估由
`task_evaluate` 承担。运行态用 pipelines 只读面查询：

```bash
# 管道（执行）列表
curl "http://localhost:9100/api/v1/pipelines" -H "Authorization: Bearer <token>"

# 管道 state 投影（task 状态的真值所在；返回当前租户全部管道，无单管道过滤参数）
curl "http://localhost:9100/api/v1/pipelines/state"   -H "Authorization: Bearer <token>"
```

### 4.4 配置读写

配置按插件 `config_files` 声明读写（未声明收空配置）；LLM 配置归 `llm_service`
插件（无全局 /config/llm 端点）：

```bash
# 读某插件声明的配置文件（file_id 见该插件 manifest 的 config_files）
curl "http://localhost:9100/api/v1/plugins/llm_service/config/llm"   -H "Authorization: Bearer <token>"

# 写回（mtime 缓存热更新）
curl -X PUT "http://localhost:9100/api/v1/plugins/llm_service/config/llm"   -H "Authorization: Bearer <token>" -H "Content-Type: application/json" -d '{...}'
```

### 4.5 工具面

```bash
# 已注册工具清单（可见面 = 启用档案 ∩ 能力注册 ∩ Agent tool_ids 白名单）
curl "http://localhost:9100/api/v1/tools" -H "Authorization: Bearer <token>"
```

Agent 无独立 REST 面：Agent 是 `config/agents/` 下的 YAML，由 context_build 插件
按 agent_id 展开为执行上下文（见 [agent-configuration.md](agent-configuration.md) §3）。

### 4.6 记忆

记忆无 REST 面：由 `hindsight_memory` 插件承载服务（`hindsight.retain` / `recall` /
`import_document`），LLM 经 `memory` 工具读写——测试走工具调用链路，不走 HTTP。

### 4.7 WebSocket 消息

```javascript
// 前端 WebSocket 连接（:9100/ws/chat，JWT 经 query 传入；重连可带 last_sequence 回放断线事件）
const ws = new WebSocket('ws://localhost:9100/ws/chat?token=<access_token>');

// 入站：用户消息（thread_id 定位会话；client_message_id 作乐观消息认领键）
ws.onopen = () => {
  ws.send(JSON.stringify({
    type: 'user_input',
    thread_id: 'thread_xxx',
    content: '你好，灵汐',
    client_message_id: '<uuid>'
  }));
};

// 出站：流式事件序列（契约真值源 config/kernel_capabilities/streaming.json）
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  // connection_confirmation → stream_start
  // → block_start / text_delta / reasoning_delta / tool_call_delta / block_end（LLM 块协议）
  // → tool_start / tool_result → usage → finish
  // → new_message（权威确认）→ stream_end（轮级收尾）→ pipeline_round_finished（run 级终结）
  // 另有：approval_request（审批）、task_status_update、heartbeat_ack 等
  console.log(msg.type);
};
```

---

## 5. 日志系统

### 5.1 统一日志架构

灵汐系统使用 `agentos_plugin_sdk.logging` 模块（`plugins/sdk/src/agentos_plugin_sdk/logging/`）提供统一日志功能。**现有代码中的 `logging.getLogger(__name__)` 无需修改即可自动受益**。

| 文件 | 职责 |
|------|------|
| `agentos_plugin_sdk/logging/__init__.py` | 公共入口：`setup_logging()` 和 `get_logger()` |
| `agentos_plugin_sdk/logging/config.py` | `LoggingConfig` 数据类（环境变量驱动） |
| `agentos_plugin_sdk/logging/formatters.py` | `StructuredFormatter`（人类可读）和 `JsonFormatter`（JSON） |
| `agentos_plugin_sdk/logging/filters.py` | 日志过滤器 |
| `agentos_plugin_sdk/logging/context.py` | `LogContext` — 请求级追踪字段（线程安全 + asyncio 安全） |

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
from agentos_plugin_sdk.logging import setup_logging
from agentos_plugin_sdk.logging.config import LoggingConfig

config = LoggingConfig(level=logging.DEBUG, output="both", json_output=True)
setup_logging(config, reset=True)
```

### 5.5 日志上下文追踪

通过 `LogContext`（基于 `contextvars`，线程安全 + asyncio 安全）在日志中注入追踪字段：

```python
from agentos_plugin_sdk.logging.context import LogContext

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
  │    └─ logging.basicConfig(level=WARNING)
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

> 注：`src/` 判定为 0.1 布局遗留（`_PROJECT_SRC = Path("src")`），0.2 仓库布局（`plugins/`/`kernel/`）下该分支不命中——失败定位以断言位置与 traceback 帧为准。

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
    "python": "3.11.x",
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


def test_basic_case():
    """测试基本场景。"""
    result = some_function(input_data="test")
    assert result.status == "expected_status"


async def test_async_case():
    """测试异步场景。

    asyncio_mode = "auto"，async def test_*() 自动被 pytest-asyncio 识别，
    无需 @pytest.mark.asyncio 装饰器。
    """
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

## 附录 A：CI/CD 流水线主要 Job 概览

> 来源：`.github/workflows/ci.yml`（18 个 job，四族 + 专项门禁 + 汇总）。完整 job 定义以该文件为准；e2e 在独立的 `.github/workflows/e2e.yml`。

| 族 | Job 名称 | 要点 |
|----|----------|------|
| Rust | `rust-lint` / `rust-build` / `rust-test` / `rust-coverage` / `rust-deny` | fmt+clippy / 编译 / 测试 / lcov 基线 / 依赖许可门禁 |
| Python | `python-lint` / `python-test` / `python-coverage` / `python-heavy-suites` | run_gates 车道（sdk-lint/sdk-mypy/sdk-test/plugins-coverage 族），基线锁见 §3.3 |
| 前端 | `frontend-endpoints-sync` / `frontend-test` / `frontend-e2e` | 端点生成物一致性 / vitest / Playwright |
| 桌面 | `electron-compile` | 桌面壳编译门禁 |
| 专项 | `timing` / `pre-commit` / `tdd-gate` / `traceability-gate` | 时序不变量 / 钩子 / TDD 合规 / 追溯标记 |
| 汇总 | `all-checks-passed` | 以上全部通过才绿 |

## 附录 B：代码质量工具配置参考

> 来源：`pyproject.toml`

| 工具 | 配置项 | 值 |
|------|--------|-----|
| ruff | `line-length` | 120 |
| ruff | `target-version` | py311 |
| ruff | `lint.select` | E, W, F, I, B, C4, UP, N, SIM, PT, RET, ARG, PTH, ERA, PL |
| mypy | `python_version` | 3.11 |
| mypy | `ignore_missing_imports` | true |
| mypy | `check_untyped_defs` | true |
| pytest | `asyncio_mode` | auto |

## 附录 C：相关文件索引

| 文件 | 用途 |
|------|------|
| `.github/workflows/ci.yml` | 主 CI 流水线定义（18 job） |
| `.github/workflows/e2e.yml` | e2e workflow（真实 LLM） |
| `scripts/run_gates.py` | 机械门禁单一事实源 |
| `pyproject.toml` | pytest / ruff / mypy 配置 |
| `tests/conftest.py` | 测试框架增强 |
| `tests/test_utils/bug_locator.py` | Bug 自动定位器 |
| `tests/test_utils/log_collector.py` | 测试日志收集器 |
| `tests/test_utils/report_generator.py` | 结构化报告生成器 |
| `plugins/sdk/src/agentos_plugin_sdk/logging/__init__.py` | 统一日志系统入口 |
| `plugins/sdk/src/agentos_plugin_sdk/logging/config.py` | 日志配置 |
| `plugins/sdk/src/agentos_plugin_sdk/logging/formatters.py` | 日志格式化器（JSON + 结构化文本） |
| `plugins/sdk/src/agentos_plugin_sdk/logging/context.py` | 日志上下文追踪 |
