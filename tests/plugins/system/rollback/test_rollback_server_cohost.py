# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""rollback server.py 合宿 exec 期绑定行为测试。

合宿静息态下裸名 ``models``/``manager`` 槽位可能是先装载成员（review/
workspace 与 rollback 同持 models.py）的同名模块——server 已改为 exec 期
绑定（宿主 loader 遮蔽保护窗口内解析），运行期槽位被 decoy 占据时
on_load/handler 仍用本插件实现，绑定不随槽位漂移。
decoy 常驻/不常驻两组输入。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = (
    Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "rollback"
)
_BARE_NAMES = ("models", "manager", "reversers")


def _load_server() -> Any:
    """模拟宿主 loader 遮蔽保护窗口：exec 前逐出裸名槽位、自身目录置顶。"""
    mod_name = "rollback_server_cohost_test"
    sys.modules.pop(mod_name, None)
    for bare in _BARE_NAMES:
        sys.modules.pop(bare, None)
    if str(_PLUGIN_DIR) not in sys.path:
        sys.path.insert(0, str(_PLUGIN_DIR))
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _plant_decoys() -> None:
    """静息态槽位被异成员同名模块占据（无本插件符号，命中即断）。"""
    for name in ("models", "manager"):
        decoy = types.ModuleType(name)
        sys.modules[name] = decoy


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestCohostExecBinding:
    @pytest.mark.parametrize("decoy_resident", [True, False])
    def test_on_load_and_handler_use_frozen_local_bindings(self, decoy_resident: bool) -> None:
        saved = {n: sys.modules.get(n) for n in _BARE_NAMES}
        try:
            server = _load_server()
            # exec 期绑定必为本插件模块（遮蔽保护窗口内解析，符号齐全）
            assert server.OperationType.__module__ == "models"
            assert hasattr(server.OperationType, "CREATE")
            assert hasattr(server.RollbackManager, "record_operation")

            if decoy_resident:
                _plant_decoys()

            # on_load 经冻结绑定构型 manager（decoy manager 无 RollbackManager，
            # 旧懒加载实现此处即 AttributeError）
            _run(server._on_load({}))
            assert server._manager is not None
            assert isinstance(server._manager, server.RollbackManager)

            # handler 经冻结绑定解析 OperationType（decoy models 无该符号，
            # 旧懒加载实现此处即 ImportError）
            out = _run(
                server.rollback_record_operation(
                    task_id="t1",
                    tool_name="file_write",
                    operation_type="create",
                    target="/tmp/x",
                    params={"content": "hi"},
                )
            )
            operation_id = out.get("operation_id")
            assert isinstance(operation_id, str)
            assert operation_id

            manager = server._ensure_manager()
            assert isinstance(manager, server.RollbackManager)
        finally:
            for n, m in saved.items():
                sys.modules.pop(n, None)
                if m is not None:
                    sys.modules[n] = m
