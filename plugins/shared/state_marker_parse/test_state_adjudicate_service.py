# @feature: FP-0.2.〇 世界内嵌谱系右移批次二 自由操作 Adjudicate 面 | @vision: V1 可进化 | @ci: python-coverage
"""state.adjudicate 服务测试（Validator 结算纯委托，世界内嵌批次二）。

- 委托语义：与直调 state_contract.validate_and_apply 同结果（accepted/rejected/
  audit 三账，clamp/reject 同判）——单源不二做的服务面钉；
- schema 外键 rejected（fail-closed 键域闸——自由操作 effects 键域同闸）；
- schema=None 兼容态逐字透传（无声明卡现状行为零变化）；
- 零副作用：入参防突变（纯函数契约，落账/审计归调用方编排）。

mock 零：纯函数服务，模块装载面真实（server.py 经生产同形 bootstrap）。
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_HERE = Path(__file__).resolve().parent
_SHARED = _HERE.parent
for _p in (str(_SHARED), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_SERVER_PATH = _HERE / "server.py"


def _load_server() -> Any:
    """装载 server.py（生产同形：插件目录 sys.path[0]，bare ``plugin`` 解析到本
    目录 plugin.py；测试护栏防跨插件同名 bare 模块互覆）。"""
    saved = sys.modules.get("plugin")
    sys.modules.pop("plugin", None)
    mod_name = "state_marker_parse_server_test"
    spec = importlib.util.spec_from_file_location(mod_name, _SERVER_PATH)
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop("plugin", None)
        if saved is not None:
            sys.modules["plugin"] = saved
    return mod


def test_adjudicate_delegates_relative_op_and_clamp() -> None:
    """相对 op + clamp：与 validate_and_apply 直调同结果（三账逐键同判）。"""
    mod = _load_server()
    out = asyncio.run(mod.state_adjudicate(
        changes={"player.气血": "+30"},
        current={"player.气血": 90},
        schema={"player": {"气血": {"type": "int", "min": 0, "max": 100}}},
    ))

    assert out["accepted"] == {"player.气血": 100}, "120 钳到声明 max"
    assert out["rejected"] == []
    assert len(out["audit"]) == 1
    assert out["audit"][0]["reason"] == "clamped"


def test_adjudicate_rejects_schema_foreign_keys_failclosed() -> None:
    """schema 外键 rejected（自由操作 effects 键域 fail-closed 同闸）。"""
    mod = _load_server()
    out = asyncio.run(mod.state_adjudicate(
        changes={"npc.阿比.好感度": "+50"},
        current={},
        schema={"player": {"气血": {"type": "int", "min": 0, "max": 100}}},
    ))

    assert out["accepted"] == {}
    assert len(out["rejected"]) == 1
    assert out["rejected"][0]["reason"] == "unknown_key"
    assert out["rejected"][0]["layer"] == "hard"


def test_adjudicate_compat_mode_passthrough() -> None:
    """schema=None 兼容态逐字透传（无声明卡现状行为零变化）。"""
    mod = _load_server()
    out = asyncio.run(mod.state_adjudicate(changes={"任意键": "任意值"}, current={}))

    assert out["accepted"] == {"任意键": "任意值"}
    assert out["rejected"] == []


def test_adjudicate_inputs_not_mutated() -> None:
    """入参防突变（纯函数契约）。"""
    mod = _load_server()
    changes = {"player.气血": "+10"}
    current = {"player.气血": 50}
    schema = {"player": {"气血": {"type": "int", "min": 0, "max": 100}}}
    asyncio.run(mod.state_adjudicate(changes=changes, current=current, schema=schema))

    assert changes == {"player.气血": "+10"}
    assert current == {"player.气血": 50}
