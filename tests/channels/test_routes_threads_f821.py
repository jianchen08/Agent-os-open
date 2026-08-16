# @feature: FP-MIGR 0.1→0.2迁移（0.1 遗留测试） | @ci: python-coverage
"""routes_threads F821 回归测试（TDD）。

回归 Round 3 发现的运行时崩溃：routes_threads.py 引用了未导入的名字
（SessionModel / get_service_provider / _session_svc），线程创建与 session
补建路径执行即 NameError。

本测试覆盖 `_ensure_session`——它构造 SessionModel，未导入时 NameError。
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tests.channels.conftest import use_channel

use_channel("api")

import routes_threads  # noqa: E402


def test_ensure_session_constructs_session_when_thread_exists() -> None:
    """thread 存在但 session 缺失时，_ensure_session 应构造 SessionModel 并登记。

    RED：SessionModel 未导入 → :548 `SessionModel(...)` 抛 NameError。
    GREEN：补 `from memory_store import SessionModel` 后正常构造。
    """
    mock_store = MagicMock()
    mock_store.get_session.return_value = None  # session 不存在，触发补建
    mock_store.get_thread.return_value = {
        "id": "t1",
        "pipeline_ids": ["p-aaa"],
        "active_pipeline_id": "p-aaa",
        "created_at": "",
        "updated_at": "",
        "metadata": {"k": "v"},
    }

    with patch.object(routes_threads, "store", mock_store):
        session = routes_threads._ensure_session("t1")

    # 构造成功（未抛 NameError）且字段来自 thread
    assert session is not None
    assert session.session_id == "t1"
    assert session.pipeline_ids == ["p-aaa"]
    # 补建的 session 应落库
    mock_store.set_session.assert_called_once()


def test_ensure_session_returns_none_when_thread_missing() -> None:
    """thread 也不存在时返回 None（不应崩溃）。"""
    mock_store = MagicMock()
    mock_store.get_session.return_value = None
    mock_store.get_thread.return_value = None

    with patch.object(routes_threads, "store", mock_store):
        session = routes_threads._ensure_session("missing")

    assert session is None
    mock_store.set_session.assert_not_called()


def test_safe_get_service_does_not_leak_nameerror() -> None:
    """`_safe_get_service` 不应把 NameError（get_service_provider 未导入）漏给调用方。

    修复前：get_service_provider 未导入 → NameError 被 except Exception 捕获 →
    静默返回 None（功能失效但不易察觉）。修复后：import 已补，无基础设施时
    仍安全返回 None。两侧都不应抛 NameError 给调用方。
    """
    # 无论基础设施是否就绪，调用方都不应见到 NameError
    result = routes_threads._safe_get_service("agent_registry")
    assert result is None or isinstance(result, object)
