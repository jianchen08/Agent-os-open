# @feature: FP-0.2.四 工具声明契约 | @ci: python-coverage
"""task_submit 声明↔实现 schema 一致性回归锁。

G2 注册闸逐字比对 manifest ``plugin.json`` 的 ``tools[].input_schema`` 与
sidecar 运行时上报的 schema（``compare_tools``：声明了 input_schema 即严格
比较）；漂移即按净化 manifest 重注册并剔除该工具。2026-11-12 装机实证：
``inherit_from`` 描述只改了 manifest、漏同步 ``tool.py``，task_submit 被剔除，
全部 agent 工具面收到「本轮不可用」注入、编排任务无法派发。

本测试锁死单一真值：``server.py`` 注册所用的
``TaskSubmitTool.get_tool_definition().input_schema`` 必须与 manifest 逐字一致。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _PLUGIN_DIR.parent
for _p in (str(_SHARED_ROOT), str(_PLUGIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_tool_module():
    """按文件路径唯一加载 tool.py（避免同车道其他插件的裸名 tool 模块互劫持）。"""
    name = "task_submit_tool_under_test"
    spec = importlib.util.spec_from_file_location(name, _PLUGIN_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_manifest_input_schema_matches_runtime_definition() -> None:
    """manifest 声明与代码内 schema 必须逐字相等（G2 严格比对同口径）。"""
    tool = _load_tool_module()

    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = manifest["capabilities"]["tools"][0]["input_schema"]
    actual = tool.TaskSubmitTool.get_tool_definition().input_schema
    assert declared == actual


def test_acceptance_schema_does_not_advertise_semantic_expect() -> None:
    """LLM 工具面不得再广告 expect→semantic_check 通道（ADR 2026-11-11）。

    指标实现与存量兼容路径保留；新任务不生成 semantic_check。schema 描述
    是 agent 唯一可见的提交契约，删掉 expect 才不会把语义评估路线喂给模型。
    """
    tool = _load_tool_module()

    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    declared = manifest["capabilities"]["tools"][0]["input_schema"]
    actual = tool.TaskSubmitTool.get_tool_definition().input_schema
    for schema in (declared, actual):
        description = schema["properties"]["acceptance_criteria"]["description"]
        assert "- expect" not in description
        assert "评估 agent" not in description
        assert "semantic_check" not in description
