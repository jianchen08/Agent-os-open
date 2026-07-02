"""测试公共配置。

职责：
1. 将 src 目录添加到 Python 路径
2. 注册统一日志系统
3. 通过 pytest hook 自动收集失败测试的日志和 bug 定位信息
4. 生成结构化测试报告
"""

import logging
import os
import sys

import pytest

# 将 src 目录添加到 Python 路径，确保测试能正确导入项目模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

# 注意："suites" 已从 collect_ignore 移除，使 pytest tests/channels/ tests/suites/ 能正确收集集成测试。
# 以下根级测试文件因需要特殊外部服务（向量库/Redis/真实 LLM 等），仍然排除：
collect_ignore = [
    "test_cross_domain_discovery.py",
    "test_directory_generator.py",
    "test_memory_metrics.py",
    "test_pgvector_store.py",
    "test_task_submit_event_chain.py",
    "test_yaml_error_chain.py",
]

# ── 报告输出目录 ──────────────────────────────────────────
REPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")


# ── pytest hook: 会话级初始化 ──────────────────────────────


def pytest_sessionstart(session: pytest.Session) -> None:
    """测试会话开始时初始化日志系统和报告生成器。"""
    # 使用标准 logging 配置（setup_logging 会破坏 pytest capture 临时文件，仅在非测试环境使用）
    logging.basicConfig(level=logging.WARNING)

    # 初始化报告生成器（存到 session config）
    from tests.test_utils.report_generator import ReportGenerator
    session.config._report_generator = ReportGenerator()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """测试会话结束时生成报告。"""
    generator: object | None = getattr(session.config, "_report_generator", None)
    if generator is None:
        return

    from tests.test_utils.report_generator import ReportGenerator
    assert isinstance(generator, ReportGenerator)

    os.makedirs(REPORT_DIR, exist_ok=True)

    # 生成控制台摘要
    console_summary = generator.to_console()
    try:
        print(console_summary)
    except UnicodeEncodeError:
        # Windows GBK 编码不支持 emoji，回退到纯 ASCII
        print(console_summary.encode('ascii', 'replace').decode('ascii'))

    # 生成 JSON 报告
    json_path = os.path.join(REPORT_DIR, "test_report.json")
    generator.to_json(json_path)
    try:
        print(f"\n[JSON] 报告已生成: {json_path}")
    except UnicodeEncodeError:
        print(f"\n[JSON] Report generated: {json_path}")

    # 生成 HTML 报告
    html_path = os.path.join(REPORT_DIR, "test_report.html")
    generator.to_html(html_path)
    try:
        print(f"[HTML] 报告已生成: {html_path}")
    except UnicodeEncodeError:
        print(f"[HTML] Report generated: {html_path}")


# ── pytest hook: 每个测试用例执行 ──────────────────────────


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo) -> None:
    """每个测试阶段的 hook，用于收集结果和失败定位。"""
    outcome = yield
    report: pytest.TestReport = outcome.get_result()

    # 只在 call 阶段处理（不含 setup/teardown）
    if report.when != "call":
        return

    generator = getattr(item.config, "_report_generator", None)
    if generator is None:
        return

    from tests.test_utils.report_generator import ReportGenerator
    assert isinstance(generator, ReportGenerator)

    # 映射 pytest 结果到报告 outcome
    outcome_str = "passed"
    error_message = ""
    tb_text = ""
    exc_info = None

    if report.failed:
        outcome_str = "failed"
        if hasattr(report, "longreprtext"):
            error_message = report.longreprtext[:500]
        if call.excinfo:
            tb_text = str(call.excinfo.getrepr())
            exc_info = call.excinfo._excinfo

    elif report.skipped:
        outcome_str = "skipped"

    duration_ms = (call.stop - call.start) * 1000 if call.stop and call.start else 0.0

    generator.add_case(
        node_id=item.nodeid,
        name=item.name,
        outcome=outcome_str,
        duration_ms=duration_ms,
        file_path=str(item.fspath) if hasattr(item, "fspath") else "",
        line_number=item.location[1] if item.location else 0,
        error_message=error_message,
        traceback=tb_text,
        exc_info=exc_info,
    )

    # 失败时打印 bug 定位信息
    if report.failed and call.excinfo and call.excinfo._excinfo:
        from tests.test_utils.bug_locator import locate_bug

        bug_result = locate_bug(call.excinfo._excinfo)
        try:
            print(bug_result.summary())
        except UnicodeEncodeError:
            # Windows GBK 编码不支持 emoji，回退到纯 ASCII
            print(bug_result.summary().encode('ascii', 'replace').decode('ascii'))


# ── fixture: 日志收集器 ────────────────────────────────────


@pytest.fixture
def log_collector():
    """提供日志收集器 fixture。

    用法::

        def test_something(log_collector):
            log_collector.start(min_level=logging.DEBUG)
            # ... 测试逻辑 ...
            result = log_collector.get_result()
            assert result.error_count == 0
            log_collector.stop()
    """
    from tests.test_utils.log_collector import LogCollector

    collector = LogCollector()
    yield collector
    collector.stop()


@pytest.fixture
def log_context():
    """提供日志上下文 fixture，自动清理。

    用法::

        def test_with_context(log_context):
            log_context.bind(request_id="test-req-123")
            # ... 测试逻辑 ...
    """
    from src.core.logging.context import LogContext

    with LogContext.scoped():
        yield LogContext


# ── 公共 Mock Fixtures（分层 conftest 体系） ──────────────────


@pytest.fixture
def mock_redis():
    """提供内存字典模拟的 Redis 客户端。

    用于替代真实 Redis 连接，所有键值操作在内存中完成。
    """
    from unittest.mock import AsyncMock

    data: dict[str, str] = {}

    class _FakeRedis:
        def __init__(self) -> None:
            self._data = data

        async def get(self, key: str) -> str | None:
            return self._data.get(key)

        async def set(self, key: str, value: str, ex: int | None = None) -> None:
            self._data[key] = value

        async def delete(self, key: str) -> int:
            return int(self._data.pop(key, None) is not None)

        async def exists(self, key: str) -> bool:
            return key in self._data

        async def keys(self, pattern: str = "*") -> list[str]:
            import fnmatch

            return [k for k in self._data if fnmatch.fnmatch(k, pattern)]

        async def flushdb(self) -> None:
            self._data.clear()

    return _FakeRedis()


@pytest.fixture
def mock_db():
    """提供内存字典模拟的数据库。

    用于替代真实数据库连接，适合简单 CRUD 测试。
    """
    tables: dict[str, list[dict]] = {}

    class _FakeDB:
        def __init__(self) -> None:
            self._tables = tables

        def insert(self, table: str, record: dict) -> str:
            import uuid

            tables.setdefault(table, [])
            record["_id"] = uuid.uuid4().hex[:12]
            tables[table].append(record)
            return record["_id"]

        def find(self, table: str, query: dict | None = None) -> list[dict]:
            rows = tables.get(table, [])
            if query is None:
                return rows
            return [
                r for r in rows if all(r.get(k) == v for k, v in query.items())
            ]

        def find_one(self, table: str, query: dict) -> dict | None:
            results = self.find(table, query)
            return results[0] if results else None

        def delete(self, table: str, query: dict) -> int:
            rows = tables.get(table, [])
            before = len(rows)
            tables[table] = [
                r for r in rows if not all(r.get(k) == v for k, v in query.items())
            ]
            return before - len(tables[table])

        def clear(self) -> None:
            tables.clear()

    return _FakeDB()


@pytest.fixture
def mock_llm():
    """提供 Mock LLM 客户端，返回预设响应。

    用法::

        def test_pipeline(mock_llm):
            mock_llm.set_response("这是AI的回复")
            # 调用使用 mock_llm 的代码
    """
    from unittest.mock import AsyncMock

    class _MockLLM:
        def __init__(self) -> None:
            self._responses: list[str] = []
            self._call_count = 0
            self.call_history: list[dict] = []

        def set_response(self, text: str) -> None:
            self._responses.append(text)

        async def complete(self, messages: list[dict], **kwargs: object) -> str:
            self._call_count += 1
            self.call_history.append({"messages": messages, "kwargs": kwargs})
            if self._responses:
                return self._responses.pop(0)
            return "mock response"

        @property
        def call_count(self) -> int:
            return self._call_count

    return _MockLLM()


@pytest.fixture
def temp_workspace(tmp_path):
    """提供临时工作空间目录，用于文件操作测试。

    基于 pytest 内置 tmp_path，自动创建标准子目录结构。
    """
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "config").mkdir()
    (ws / "output").mkdir()
    return ws
