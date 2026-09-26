"""测试公共配置。

职责：
1. 注册统一日志系统
2. 通过 pytest hook 自动收集失败测试的日志和 bug 定位信息
3. 生成结构化测试报告
"""

import logging
import os
import sys
from pathlib import Path

import pytest

from tests import _stdlib_guard

# ── 报告输出目录 ──────────────────────────────────────────
REPORT_DIR = os.path.join(os.path.dirname(__file__), "..", "reports")


# 0.2 插件平铺 import 共享的裸模块名（plugin.py / models.py / tool.py 等）。
# 多个插件同名，pytest 收集时先导入的会缓存进 sys.modules，导致后收集的测试
# ``from plugin import X`` 命中错误模块。在收集完成后统一逐出，让各测试的
# 模块级导入按自身 sys.path 重新解析。
_BARE_MODULE_NAMES = {
    "plugin",
    "models",
    "tool",
    "types",
    "adapter",
    "stream_client",
    "server",
    "decorators",
    "exceptions",
    "registry",
    "manager",
    "decider",
    "approval",
    "sensitive_paths",
    "workspace",
    "gateway",
    "storage",
    "service",
    "http_api",
    "interfaces",
    "connector_types",
    "base",
    "config_mixin",
    "degradation",
    "policy",
}

# 名单内与 stdlib 同名的裸名：被插件同名模块劫持时重装 stdlib 本体而非逐出。
_STDLIB_NAMES = {"types"}


def _is_stdlib_module(mod: object) -> bool:
    """是否 stdlib 模块（名单内 "types" 与 stdlib 同名）。

    stdlib 同名模块不逐出：逐出后 ``from types import`` 会按 sys.path 重新
    解析，车道内其他测试残留的插件目录（含 pipeline/types.py）会劫持该名。
    """
    return _stdlib_guard.is_stdlib_module(mod)


@pytest.hookimpl(trylast=True)
def pytest_collect_file(file_path: Path, parent: pytest.Collector) -> pytest.Collector | None:
    """每个测试文件收集**前**逐出共享裸模块缓存并把本文件声明的源目录置前。

    0.2 架构下各插件目录内是平铺 import（from plugin import ...），
    不同插件的同名模块（plugin/models/policy/storage 等）会互相覆盖
    sys.modules 缓存——后收集的测试 ``from plugin import X`` 会命中
    先导入的其它插件模块。本钩子在文件收集（含模块导入）前调用：

    1. 逐出共享裸模块缓存，让模块级导入按 sys.path 重新解析；
    2. 把该文件最近 conftest 声明的 ``_PLUGIN_SOURCE_DIRS`` 推到 sys.path
       最前（``_PLUGIN_CONFLICT_DIRS`` 摘除）——各 conftest 的模块级
       insert 在会话启动期按参数顺序全部驻留，仅逐出不控解析顺序，
       收集期导入会命中"最后加载者"的同名模块（如 ``import server``
       命中 monitoring 的 server.py）。置前后本文件的模块级导入确定性
       解析到自己的插件目录。

    置前/摘除跨文件驻留与运行期 pytest_runtest_setup 同款（该钩子同样
    晋升后不恢复）；同目录文件共享同一 conftest 声明，重复置前幂等，
    跨目录时后收集者的置前覆盖前者，每个文件的收集期导入都解析到自己
    声明的源目录。

    与 stdlib 同名的裸名（"types"）被劫持时不逐出（逐出 = 下一个
    ``from types import`` 按 sys.path 重解析再炸一次），改为重装 stdlib
    本体——后续导入永远命中 stdlib。
    """
    if file_path.suffix == ".py":
        for name in _BARE_MODULE_NAMES:
            mod = sys.modules.get(name)
            if mod is not None and _stdlib_guard.is_stdlib_module(mod):
                continue
            if name in _STDLIB_NAMES:
                _stdlib_guard.ensure_stdlib_module(name)
            else:
                sys.modules.pop(name, None)
        from tests.plugins._bare_module_evict import (
            demote_conflict_dirs,
            find_conftest_declared_dirs,
            promote_source_dirs,
        )

        test_dir = str(file_path.parent)
        source_dirs = find_conftest_declared_dirs(parent.config, test_dir, "_PLUGIN_SOURCE_DIRS")
        if source_dirs:
            demote_conflict_dirs(
                find_conftest_declared_dirs(parent.config, test_dir, "_PLUGIN_CONFLICT_DIRS")
            )
            promote_source_dirs(source_dirs)
    return None


# ── pytest hook: 会话级初始化 ──────────────────────────────


def pytest_sessionstart(session: pytest.Session) -> None:
    """测试会话开始时初始化日志系统和报告生成器。"""
    # 使用标准 logging 配置（setup_logging 会破坏 pytest capture 临时文件，仅在非测试环境使用）
    logging.basicConfig(level=logging.WARNING)

    # 初始化报告生成器（存到 session config）
    from tests.test_utils.report_generator import ReportGenerator
    session.config._report_generator = ReportGenerator()


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """测试会话结束时生成报告。

    环境开关：AGENTOS_SKIP_TEST_REPORT=1 时跳过全部报告写盘与控制台摘要
    （避免并发 pytest 会话互相覆盖 reports/test_report.{json,html} 的副作用）。
    默认（未设置/为 0）行为与历史完全一致。
    """
    if os.environ.get("AGENTOS_SKIP_TEST_REPORT", "") == "1":
        return

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

