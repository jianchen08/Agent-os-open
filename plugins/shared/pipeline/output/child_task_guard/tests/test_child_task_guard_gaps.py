# @feature: FP-0.2.〇 任务执行驱动 | @ci: python-coverage
"""child_task_guard 剩余分支补测（缺行清零批）——service_access 不可导入降级。

锁定行为契约（对应 plugin.py 227-229）：

活跃子任务查询的第三级回退链是
``ctx.get_service("task_service")`` → ``tasks.service_access.get_task_service()``。
当 ``tasks.service_access`` 所在模块**不可导入**（任务域插件被移除/部署残缺）
时按「服务不可用」处理：记 warning 并返回 None，由调用方按无活跃子任务放行——
不得裸抛中断管道步（本方法「不可用时返回 None」契约）。

与既有 ``test_service_access_returns_none``（模块在但工厂返 None）构成对照：
两条路径的观察差异 = 是否留下 warning 日志。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types as _types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_PIPELINE_ROOT = Path(__file__).resolve().parents[4]  # plugins/shared

for _d in (_PLUGIN_DIR, _PIPELINE_ROOT):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_plugin() -> Any:
    """按唯一模块名加载 plugin.py（平铺布局防裸名互劫持）。"""
    mod_name = "child_task_guard_gaps_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _CtxWithoutService:
    """最小 PluginContext 替身：任何服务查询都抛 KeyError（迫使走导入回退）。"""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}

    def get_service(self, name: str) -> Any:
        raise KeyError(name)


class TestServiceAccessImportFailure:
    """tasks.service_access 不可导入：warning + None 降级，不裸抛。"""

    def test_import_error_degrades_to_none_with_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setitem(sys.modules, "tasks.service_access", None)
        guard = _load_plugin().ChildTaskGuard(config={})
        with caplog.at_level(logging.WARNING):
            assert guard._get_task_service(_CtxWithoutService()) is None
        assert any(
            "tasks.service_access 不可用" in r.getMessage() for r in caplog.records
        ), "降级必须留可见日志（部署残缺需可诊断）"

    def test_module_present_factory_none_is_silent(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """对照：模块可导入但工厂返 None → 同样降级，但不留 warning（非异常路径）。"""
        monkeypatch.setitem(
            sys.modules,
            "tasks.service_access",
            _types.SimpleNamespace(get_task_service=lambda: None),
        )
        guard = _load_plugin().ChildTaskGuard(config={})
        with caplog.at_level(logging.WARNING):
            assert guard._get_task_service(_CtxWithoutService()) is None
        assert not [
            r for r in caplog.records if "tasks.service_access 不可用" in r.getMessage()
        ]

    def test_ctx_service_wins_over_broken_module(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ctx 已注入服务时根本不做导入回退（导入失败不影响主路径）。"""
        monkeypatch.setitem(sys.modules, "tasks.service_access", None)
        sentinel = object()

        class _CtxWithService(_CtxWithoutService):
            def get_service(self, name: str) -> Any:
                if name == "task_service":
                    return sentinel
                raise KeyError(name)

        guard = _load_plugin().ChildTaskGuard(config={})
        assert guard._get_task_service(_CtxWithService()) is sentinel
