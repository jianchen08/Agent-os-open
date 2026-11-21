# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""「判定键展开」三处实现的一致性契约锁（审查 SF#1）。

同一概念——「`call_tool` 条目按目标工具名 + 目标参数判定」——在三个插件里各有一份
实现，因为播种副本对共享根裸模块不可达（跨插件不可共享代码是硬约束）：

| 插件 | 实现 | 消费面 |
|---|---|---|
| `security_check` | `_effective_tool_call(tc)` | 基础底线 / 位置闸 / 逐工具授权 / 审批卡面 / 拒绝指纹 |
| `level_guard` | `_effective_tool_name(tc)` | 任务控制类层级闸 |
| `duplicate_check` | `_effective_fail_target(tool, res)` | 连败归属（**输入是 tool_results 条目**，不是调用条目） |

不能抽公共模块，故用**一致性用例**兜住漂移：同一组调用形态喂三处实现，断言展开出的
目标名相同；且「白名单式展开」（只认 `call_tool` 一个名字）在三处一致。

本文件是**跨插件契约测试**（测试面，不改生产模块边界）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SHARED = Path(__file__).resolve().parents[1]  # plugins/shared/
for _p in (str(_SHARED), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_FORWARD = "call_tool"


def _load(plugin_dir: str, mod_name: str) -> Any:
    path = _SHARED / plugin_dir / "plugin.py"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_SEC = _load("security_check", "sc_expand_consistency")
_LVL = _load("level_guard", "lg_expand_consistency")
_DUP = _load("duplicate_check", "dc_expand_consistency")


def _call(target: str, arguments: Any = None, *, name: str = _FORWARD) -> dict[str, Any]:
    return {"id": "c1", "name": name, "args": {"tool": target, "arguments": arguments or {}}}


def _expand_sec(tc: dict[str, Any]) -> str:
    return _SEC._effective_tool_call(tc)[0]


def _expand_lvl(tc: dict[str, Any]) -> str:
    return _LVL._effective_tool_name(tc)


def _expand_dup(tc: dict[str, Any]) -> str:
    """duplicate_check 的输入是 tool_results 条目（壳名 + metadata.target_tool）。"""
    entry = {"tool_name": tc["name"], "success": False, "metadata": {"target_tool": tc["args"]["tool"]}}
    return _DUP._effective_fail_target(entry["tool_name"], entry)[0]


class TestExpansionConsistency:
    """三处展开对同一调用必须得出同一目标名（漂移即红）。"""

    @pytest.mark.parametrize(
        "call",
        [
            _call("bash", {"command": "ls"}),
            _call("file_read", {"path": "docs/a.md"}),
            _call("task_submit", {"description": "x"}),
            _call("mcp__vendor__fetch", {"url": "https://example.invalid"}),
        ],
    )
    def test_same_target_across_three_plugins(self, call: dict[str, Any]) -> None:
        target = call["args"]["tool"]
        assert _expand_sec(call) == target
        assert _expand_lvl(call) == target
        assert _expand_dup(call) == target

    @pytest.mark.parametrize(
        "call",
        [
            _call("bash", {"command": "ls"}, name="some_other_tool"),
            _call("bash", {"command": "ls"}, name="file_read"),
        ],
    )
    def test_whitelist_expansion_only_for_forward_shell(self, call: dict[str, Any]) -> None:
        """非转发壳工具伪造 tool 字段：三处都不展开（白名单式，A13 同口径）。"""
        assert _expand_sec(call) == call["name"]
        assert _expand_lvl(call) == call["name"]

    def test_missing_target_falls_back_to_shell_name_everywhere(self) -> None:
        """目标名缺失：三处一致回落壳名（不明白放行成任意目标）。"""
        bare: dict[str, Any] = {"id": "c1", "name": _FORWARD, "args": {}}
        assert _expand_sec(bare) == _FORWARD
        assert _expand_lvl(bare) == _FORWARD
        entry = {"tool_name": _FORWARD, "success": False, "metadata": {}}
        assert _DUP._effective_fail_target(_FORWARD, entry)[0] == _FORWARD

    def test_forward_shell_name_constant_identical(self) -> None:
        """三处的转发壳常量必须同值（改名时一处漏改即红）。"""
        assert _SEC.DYNAMIC_FORWARD_TOOL == _FORWARD
        assert _LVL.DYNAMIC_FORWARD_TOOL == _FORWARD
        assert _DUP.DYNAMIC_FORWARD_TOOL == _FORWARD
