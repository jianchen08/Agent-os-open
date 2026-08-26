# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation providers/base.py 抽象基类测试（providers 包覆盖补）。

覆盖：
1. IsolationProvider 抽象性：缺 get_level 无法实例化；全部实现后实例化成功；
2. 接口契约：get_level/is_available/create_environment/destroy_environment/
   execute_in_environment/get_environment_status 均为抽象方法；
3. health_check 默认实现 = is_available（可用/不可用两组输入）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from plugins.shared.system.isolation.providers.base import IsolationProvider

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/


def _run(coro: Any) -> Any:
    """共享测试进程中其他测试可能关闭主 loop，须自建独立 loop。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _load_mod() -> Any:
    """动态加载 providers/base.py（唯一模块名，防与其它测试的裸名模块冲突）。"""
    mod_name = "isolation_providers_base_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "providers" / "base.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()

if not TYPE_CHECKING:
    # 运行期：从动态加载的模块取真实类（mypy 走上方静态导入）。
    IsolationProvider = _MOD.IsolationProvider


class _ConcreteProvider(IsolationProvider):
    """实现全部抽象方法的测试替身（内部依赖，真实实现）。"""

    def __init__(self, available: tuple[bool, str | None] = (True, None)) -> None:
        self._available = available

    def get_level(self) -> Any:
        return "host"

    async def is_available(self) -> tuple[bool, str | None]:
        return self._available

    async def create_environment(self, context: Any) -> Any:
        return context

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        return True

    async def execute_in_environment(self, env_id: str, operation: dict[str, Any]) -> Any:
        return operation

    async def get_environment_status(self, env_id: str) -> Any:
        return "ready"


class TestAbstractContract:
    def test_cannot_instantiate_without_all_abstract_methods(self) -> None:
        """缺任一抽象方法即不可实例化（fail-closed 契约）。"""
        with pytest.raises(TypeError):
            IsolationProvider()  # type: ignore[abstract]

    def test_abstract_method_names(self) -> None:
        """六个接口方法必须全部抽象，保证提供者可替换性。"""
        expected = {
            "get_level",
            "is_available",
            "create_environment",
            "destroy_environment",
            "execute_in_environment",
            "get_environment_status",
        }
        assert expected <= IsolationProvider.__abstractmethods__

    def test_concrete_subclass_instantiable(self) -> None:
        provider = _ConcreteProvider()
        assert provider.get_level() == "host"


class TestHealthCheck:
    @pytest.mark.parametrize(
        ("available", "expected"),
        [
            ((True, None), (True, None)),
            ((False, "daemon down"), (False, "daemon down")),
        ],
    )
    def test_health_check_delegates_to_is_available(
        self, available: tuple[bool, str | None], expected: tuple[bool, str | None]
    ) -> None:
        """health_check 默认实现即转发 is_available（可用/不可用两组输入）。"""
        provider = _ConcreteProvider(available)
        assert _run(provider.health_check()) == expected
