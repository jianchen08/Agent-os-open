# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""隔离任务批次免审批判定收窄（ADR
2026-09-29-isolated-subtask-approval-card-batch-credit）行为锁。

装机 M1 实锤回归（2026-09-29）：混合批 [容器 bash_execute, 宿主 file_read] 在
隔离任务中被 `_is_isolated` 的 all() 语义整批判非隔离 → 无显式档按 default 档 →
规则降级态保守审批 → 弹审批卡无人值守停摆。收窄后批次判定只看「宿主裸跑的
命令执行类调用」；S1 宿主 bash 回落所选档审批语义保持。
"""

from __future__ import annotations

import asyncio
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
        "security_check_plugin_batch_credit", str(Path(_THIS_DIR) / "plugin.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_module()
SecurityCheckPlugin = _mod.SecurityCheckPlugin

pytestmark = pytest.mark.unit

# 装机 M1 实锤批次形状（isolation_guard 写面原样）：
# bash_execute 路由容器（task_isolated=True），file_read 走 policy host
# （task_isolated=False，S1 写面）。
_INCIDENT_MIXED_CONTEXTS = [
    {"tool_name": "bash_execute", "provider": "docker", "level": "isolated", "task_isolated": True},
    {"tool_name": "file_read", "provider": "host", "level": "non_isolated", "task_isolated": False},
]


def _plugin(**config: Any) -> Any:
    return SecurityCheckPlugin(config or {})


def _ctx(
    tool_calls: list[dict[str, Any]],
    contexts: list[dict[str, Any]] | None = None,
    session_id: str = "sess-batch-credit",
) -> Any:
    state = {
        "core_type": "tool_execute",
        "raw_tool_calls": tool_calls,
        "execution_contexts": contexts if contexts is not None else _INCIDENT_MIXED_CONTEXTS,
        "session_id": session_id,
    }

    def _get_service(name: str) -> Any:
        raise KeyError(name)

    return types.SimpleNamespace(state=state, get_service=_get_service)


# ── 批次隔离判定收窄（真源 = ADR 2026-09-29）─────────────────────────
def test_mixed_batch_container_bash_plus_host_file_read_is_isolated():
    """装机 M1 回归：容器 bash + 宿主只读工具混合批不拖垮免审批默认。"""
    plugin = _plugin()
    assert plugin._is_isolated(_ctx([], _INCIDENT_MIXED_CONTEXTS), _INCIDENT_MIXED_CONTEXTS) is True


def test_host_routed_bash_still_fails_closed():
    """S1 保持：宿主裸跑的命令执行类调用回落所选档审批。"""
    plugin = _plugin()
    host_bash = [
        {"tool_name": "bash_execute", "provider": "host", "level": "non_isolated", "task_isolated": False},
    ]
    assert plugin._is_isolated(_ctx([], host_bash), host_bash) is False


def test_non_isolated_task_container_batch_not_isolated():
    """非隔离任务（writer 全写 False）不因批次形态获得免审批默认。"""
    plugin = _plugin()
    contexts = [
        {"tool_name": "bash_execute", "provider": "docker", "level": "isolated", "task_isolated": False},
    ]
    assert plugin._is_isolated(_ctx([], contexts), contexts) is False


def test_empty_contexts_fail_closed():
    """无 context（guard 未写入/禁用）维持 fail-closed。"""
    plugin = _plugin()
    assert plugin._is_isolated(_ctx([], []), []) is False


def test_unknown_host_shape_fail_closed():
    """tool_name 缺失的宿主 context 保守按执行平面处理（fail-closed）。"""
    plugin = _plugin()
    contexts = [{"provider": "host", "task_isolated": False}]
    assert plugin._is_isolated(_ctx([], contexts), contexts) is False


def test_isolated_task_pure_host_aux_batch_fails_closed_like_before():
    """隔离任务纯宿主辅助批（无容器 context）：writer 单键不可辨任务级标志，
    维持修复前的非隔离判定（fail-closed 方向，ADR 残余风险声明）。"""
    plugin = _plugin()
    contexts = [
        {"tool_name": "file_write", "provider": "host", "level": "non_isolated", "task_isolated": False},
    ]
    assert plugin._is_isolated(_ctx([], contexts), contexts) is False


# ── 端到端：装机 M1 同形批次不再弹审批卡 ────────────────────────────
def test_incident_round_end_to_end_no_approval_card():
    """降级态 + 无显式档 + 混合批（容器 bash + 宿主 file_read）→ 旁路档整体放行。

    修复前同形批次走 default 档 → bash 危险工具未命中规则 → 降级保守审批
    （human-interaction 缺席时软拦截，state_updates 带 pre_decided_results）。
    """
    plugin = _plugin()
    plugin._rules_degraded = True  # 装机/开发双侧 sidecar 实况：注入链失效降级态
    ctx = _ctx(
        [{"name": "bash_execute", "args": {"command": "pwd && ls -la"}}],
        _INCIDENT_MIXED_CONTEXTS,
    )
    result = asyncio.run(plugin.execute(ctx))
    assert result.state_updates == {}
