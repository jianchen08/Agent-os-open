# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation approval.py 数据面与危险检测测试（A5.3 补）。

test_approval_policy_source.py 已覆盖 ApprovalDecisionEngine 决策主路径；
本文件补其未触达面：
1. ApprovalContext.to_dict（tool_definition 有无两形态、policy 有无）；
2. ApprovalDecision.to_dict 字段往返；
3. DangerChecker：工具声明危险操作前缀匹配（command/字段名两路径）、
   无声明返回 None、非字符串输入跳过；
4. _get_command_input：字段缺失/非字符串；
5. classify_tool_safety 三分类兜底；
6. ApprovalDecisionEngine 默认配置与 config 注入。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    mod_name = "isolation_approval_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "approval.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()
ApprovalContext = _MOD.ApprovalContext
ApprovalDecision = _MOD.ApprovalDecision
DangerChecker = _MOD.DangerChecker
ApprovalDecisionEngine = _MOD.ApprovalDecisionEngine
classify_tool_safety = _MOD.classify_tool_safety
IsolationLevel = _MOD.IsolationLevel


class TestApprovalContextDict:
    def test_to_dict_with_definition_and_policy(self) -> None:
        policy = MagicMock()
        policy.approval = True
        tool = MagicMock()
        tool.name = "bash_execute"
        ctx = ApprovalContext(
            tool_name="bash_execute",
            tool_definition=tool,
            inputs={"command": "ls"},
            isolation_level=IsolationLevel.HOST,
            policy=policy,
            user_id="u1",
            session_id="s1",
            task_id="t1",
        )
        d = ctx.to_dict()
        assert d["tool_name"] == "bash_execute"
        assert d["tool_definition"] == "bash_execute"
        assert d["inputs"] == {"command": "ls"}
        assert d["isolation_level"] == "non_isolated"
        assert d["policy_approval"] is True
        assert d["user_id"] == "u1" and d["session_id"] == "s1" and d["task_id"] == "t1"

    def test_to_dict_without_definition_and_policy(self) -> None:
        ctx = ApprovalContext(tool_name="file_read")
        d = ctx.to_dict()
        assert d["tool_definition"] is None
        assert d["policy_approval"] is None


class TestApprovalDecisionDict:
    def test_to_dict_roundtrip(self) -> None:
        decision = ApprovalDecision(
            requires_approval=True,
            decision_type="NEEDS_APPROVAL",
            reason="r",
            risk_score=0.9,
            risk_factors=["HOST_MODE"],
            details={"k": "v"},
        )
        d = decision.to_dict()
        assert d == {
            "requires_approval": True,
            "decision_type": "NEEDS_APPROVAL",
            "reason": "r",
            "risk_score": 0.9,
            "risk_factors": ["HOST_MODE"],
            "details": {"k": "v"},
        }


class TestDangerChecker:
    def test_no_dangerous_operations_declared(self) -> None:
        checker = DangerChecker()
        tool = MagicMock()
        tool.dangerous_operations = []
        assert checker.check("bash_execute", tool, {"command": "rm -rf /"}) is None
        assert checker.check("bash_execute", None, {"command": "rm -rf /"}) is None

    def test_command_prefix_match(self) -> None:
        checker = DangerChecker()
        tool = MagicMock()
        tool.dangerous_operations = ["rm -rf"]
        assert checker.check("bash_execute", tool, {"command": "rm -rf /tmp/x"}) == "rm -rf"

    def test_command_input_field_fallback_scan(self) -> None:
        """命令字段缺失时扫描输入字段名（write:/etc/ 前缀）。"""
        checker = DangerChecker()
        tool = MagicMock()
        tool.dangerous_operations = ["write:/etc/"]
        assert checker.check("custom_tool", tool, {"write:/etc/hosts": "data"}) == "write:/etc/"

    def test_no_match_returns_none(self) -> None:
        checker = DangerChecker()
        tool = MagicMock()
        tool.dangerous_operations = ["rm -rf"]
        assert checker.check("bash_execute", tool, {"command": "ls -la"}) is None
        assert checker.check("bash_execute", tool, {"other_key": "rm -rf"}) is None

    def test_non_string_command_input_ignored(self) -> None:
        checker = DangerChecker()
        tool = MagicMock()
        tool.dangerous_operations = ["rm -rf"]
        assert checker.check("bash_execute", tool, {"command": ["rm", "-rf"]}) is None

    def test_get_command_input_variants(self) -> None:
        checker = DangerChecker()
        assert checker._get_command_input("bash_execute", {"command": "ls"}) == "ls"
        assert checker._get_command_input("bash_execute", {"command": 123}) is None
        assert checker._get_command_input("bash_execute", {}) is None
        assert checker._get_command_input("unknown_tool", {"command": "ls"}) is None


class TestClassifyToolSafety:
    def test_three_classes(self) -> None:
        assert classify_tool_safety("file_read") == "safe"
        assert classify_tool_safety("bash_execute") == "dangerous"
        assert classify_tool_safety("some_new_tool") == "unknown"


class TestEngineConfig:
    def test_default_config_loaded(self) -> None:
        engine = ApprovalDecisionEngine()
        assert engine.config["enabled"] is True
        assert engine.config["policies"]["host_dangerous"]["action"] == "NEEDS_APPROVAL"

    def test_custom_config_injected(self) -> None:
        engine = ApprovalDecisionEngine(config={"enabled": False, "policies": {}})
        assert engine.config["enabled"] is False
