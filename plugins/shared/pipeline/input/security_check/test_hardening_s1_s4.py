# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""安全闸门加固四项（执行方案批次 1，评估 S1–S4）行为锁。

S1  host 裸跑不承载免审批默认：isolation_guard 对 provider=host 的 context
    写 task_isolated=False，security_check._is_isolated 的 all() 语义对混合
    批次 fail-closed（任一 host context → 走所选档审批）。
S2  规则注入链断裂降级态：危险工具一律拒绝（fail-closed），非危险工具放行。
S3  路径参数键白名单扩展：destination/filename/file/paths/file_paths/cwd
    等证据化键纳入遍历/敏感目录检测。
S4  argv 归一化关键词匹配：空白变体（"rm  -rf"）不再绕过黑名单。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[3])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "security_check_plugin_hardening", str(Path(_THIS_DIR) / "plugin.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_module()
SecurityCheckPlugin = _mod.SecurityCheckPlugin

pytestmark = pytest.mark.unit


def _plugin(**config: Any) -> Any:
    return SecurityCheckPlugin(config or {})


def _ctx(tool_calls: list[dict[str, Any]], contexts: list[dict[str, Any]] | None = None) -> Any:
    state = {
        "core_type": "tool_execute",
        "raw_tool_calls": tool_calls,
        "execution_contexts": contexts
        or [{"provider": "host", "task_isolated": False}],
        "session_id": "sess-hardening",
    }

    def _get_service(name: str) -> Any:
        raise KeyError(name)

    return types.SimpleNamespace(state=state, get_service=_get_service)


# ── S1：host 裸跑不承载免审批默认 ───────────────────────────────────
def test_s1_all_container_contexts_keep_isolated():
    plugin = _plugin()
    contexts = [
        {"provider": "docker", "level": "isolated", "task_isolated": True},
        {"provider": "docker", "level": "isolated", "task_isolated": True},
    ]
    assert plugin._is_isolated(contexts) is True


def test_s1_any_host_context_fails_closed():
    plugin = _plugin()
    mixed = [
        {"provider": "docker", "level": "isolated", "task_isolated": True},
        {"provider": "host", "level": "non_isolated", "task_isolated": False},
    ]
    assert plugin._is_isolated(mixed) is False


def test_s1_isolation_guard_marks_host_context_not_isolated():
    """isolation_guard 写面：host provider 的 context 不承载免审批默认。"""
    ig_dir = str(Path(_THIS_DIR).parents[1] / "input" / "isolation_guard")
    spec = importlib.util.spec_from_file_location(
        "isolation_guard_hardening",
        str(Path(ig_dir) / "plugin.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    ig_mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("isolation_guard_hardening", ig_mod)
    if ig_dir not in sys.path:
        sys.path.insert(0, ig_dir)  # plugin.py 裸名导入 decider/service 同目录解析
    spec.loader.exec_module(ig_mod)

    plugin = object.__new__(ig_mod.IsolationGuard)
    # S1 写面直测：host context 不承载免审批默认，容器 context 保持
    contexts = [
        {"provider": "docker", "level": "isolated"},
        {"provider": "host", "level": "non_isolated"},
    ]
    plugin._apply_task_isolation(contexts, task_isolated=True)
    assert contexts[0]["task_isolated"] is True
    assert contexts[1]["task_isolated"] is False
    # 任务本身非隔离 → 全部 False
    plugin._apply_task_isolation(contexts, task_isolated=False)
    assert all(c["task_isolated"] is False for c in contexts)


# ── S2：降级态处置 = 拍板 D7（撤码留档）────────────────────────────
# "降级态危险工具一律拒绝"会推翻 test_default_rules_fallback 锁定的
# "回退态普通命令放行"契约（安全 vs 降级可用性的产品权衡），升格拍板 D7；
# S4 归一化 + 降级显式提示（既有 emit 通道）在拍板前承担残余风险。


# ── S3：路径参数键白名单扩展 ────────────────────────────────────────
def test_s3_traversal_detected_on_extended_keys():
    plugin = _plugin()
    for key in ("destination", "filename", "file", "paths", "file_paths", "cwd"):
        args = {key: "../etc/passwd" if key != "paths" else ["../etc/passwd"]}
        # paths 为列表值：str() 化后仍含 ../，应命中（fail-closed 方向）
        assert plugin._check_path_traversal(args), key


def test_s3_benign_paths_pass():
    plugin = _plugin()
    assert plugin._check_path_traversal({"destination": "out/report.md", "cwd": "."}) == ""


# ── S4：argv 归一化关键词匹配 ───────────────────────────────────────
def test_s4_whitespace_variant_no_longer_bypasses():
    plugin = _plugin()
    # 双空格变体（裸子串匹配绕过形态）必须命中 dangerous_commands
    action, rule = plugin._match_rules("bash_execute", {"command": "rm  -rf /tmp/x"})
    assert action == "needs_approval"
    assert rule == "dangerous_commands"


def test_s4_tab_and_case_variants_hit():
    plugin = _plugin()
    action, _ = plugin._match_rules("bash_execute", {"command": "RM\t-RF /tmp"})
    assert action == "needs_approval"


def test_s4_benign_command_still_passes():
    plugin = _plugin()
    action, rule = plugin._match_rules("bash_execute", {"command": "ls -la /tmp/demo"})
    assert action == ""
    assert rule == ""


# ── S2/D7：降级态保守审批（2026-09-24 用户授权自定）─────────────────
# 语义：降级态危险工具未命中规则 → needs_approval（人审把关）；allow
# 白名单与已记忆指纹先于判定放行（降级不吞既有豁免）。
def test_d7_degraded_keeps_allow_whitelist():
    plugin = _plugin(
        rules=[{
            "name": "safe_ls", "tools": ["bash_execute"], "action": "allow",
            "params": ["command"],
            "patterns": [{"type": "keyword", "value": "ls -la"}],
        }]
    )
    plugin._rules_degraded = False  # config rules 在场不会置降级；此处验证 allow 通道本身
    plugin._rules_degraded = True
    action, rule = plugin._match_rules("bash_execute", {"command": "ls -la /tmp"})
    assert action == "allow"
    assert rule == "safe_ls"


@pytest.mark.asyncio
async def test_d7_degraded_respects_remembered_fingerprint():
    plugin = _plugin(
        builtin_tools_config={
            "tools": [{"name": "bash_execute", "dangerous_operations": ["shell"]}]
        }
    )
    plugin._rules_degraded = True
    sig = plugin._make_signature("bash_execute", {"command": "deploy.sh"})
    plugin._approved_signatures.add(sig)
    calls = [{"name": "bash_execute", "args": {"command": "deploy.sh"}}]
    decision = await plugin._authorize_tool_calls(
        _ctx(calls), calls, isolated=False, mode="default"
    )
    assert decision is None  # 已记忆指纹放行，降级不吞豁免
