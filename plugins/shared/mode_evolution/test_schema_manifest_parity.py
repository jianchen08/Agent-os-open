# @feature: FP-0.2.四 工具声明契约 | @ci: python-coverage
"""mode_evolution 声明↔实现 schema 一致性回归锁。

G2 注册闸逐字比对 manifest ``plugin.json`` 的 ``tools[].input_schema`` 与
sidecar 运行时上报的 schema（``compare_tools``：声明了 input_schema 即严格
比较，键序无关）；漂移即记入 ``rejected_tools`` 并置失败桩。2026-11-12 装机
实证：``eval_summarize`` 的 ``results.items`` 子结构只写在 ``server.py`` 内联
schema、``model`` 描述两侧不同步，该工具被剔出工具面（契约账本
``g2_consistency=sanitized``，``rejected_tools=["eval_summarize"]``）。

本测试锁死单一真值：``server.py`` 经 ``@plugin.tool`` 注册进
``plugin._tools`` 的 schema（即 sidecar ``tools/list`` 上报的原件）必须与
manifest 逐字相等。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
_DECLARED_TOOL_NAMES = ("eval_query", "eval_summarize", "eval_run_heldout", "proposal")


def _load_server_module():
    """按唯一模块名装载 server.py（避开车道内裸名 server 的跨插件互覆）。"""
    path = os.path.join(_PLUGIN_DIR, "server.py")
    spec = importlib.util.spec_from_file_location(
        "mode_evolution_schema_parity_server", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def manifest():
    with open(os.path.join(_PLUGIN_DIR, "plugin.json"), encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def runtime_tools():
    return _load_server_module().plugin._tools


def test_declared_tool_set_matches_manifest(manifest, runtime_tools):
    """manifest 声明面 = 四个业务工具（基础设施工具 http.handle/mode.* 不在此列）。"""
    declared = {t["name"] for t in manifest["capabilities"]["tools"]}
    assert declared == set(_DECLARED_TOOL_NAMES)
    for name in declared:
        assert name in runtime_tools, f"manifest 声明了 {name}，实现未注册"


@pytest.mark.parametrize("tool_name", _DECLARED_TOOL_NAMES)
def test_manifest_input_schema_matches_runtime(manifest, runtime_tools, tool_name):
    """manifest 声明与运行时上报的 input_schema 逐字相等（G2 严格比对同口径）。"""
    declared = next(t for t in manifest["capabilities"]["tools"]
                    if t["name"] == tool_name)
    actual = runtime_tools[tool_name].schema
    assert declared["input_schema"] == actual
