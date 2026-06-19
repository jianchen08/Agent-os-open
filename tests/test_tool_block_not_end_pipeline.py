"""工具级拦截应转为「工具失败结果」而非终结整个管道。

历史 Bug:
  config/pipelines/default.yaml 曾有三条 input 路由（security_blocked /
  level_blocked / isolation_blocked）用 target=end，把工具级权限/隔离/安全
  拦截放大成整个管道终结——拦截原因被写进 RAW_RESULT 当成最终输出，
  导致「权限不足」变成任务最终结果、任务被错误标记完成/失败、停止后重发
  消息时 agent 身份丢失。

修复:
  1. 删除这三条 target=end 路由
  2. tool_core 新增 _check_tool_blocked：执行工具前统一检查 level/isolation/
     security 三类拦截决策，被拦截的工具转为 success=False 的失败结果返回
     给 LLM，让 LLM 自行调整策略，管道继续流转。

本测试锁定该契约（参考 test_isolation_fallback.py 的 fail-closed 风格）。
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ═══════════════════════════════════════════════════════════════
# P0: default.yaml 不得再有 target=end 的工具拦截路由
# ═══════════════════════════════════════════════════════════════


class TestNoEndRouteForToolBlock:
    """P0: 工具级拦截路由不得用 target=end 终结管道。"""

    def test_no_security_blocked_end_route(self):
        cfg = _load_pipeline_yaml()
        for r in cfg.get("input_routes", []):
            cond = r.get("condition", "")
            if "security.decision" in cond or "security.level_decision" in cond \
                    or "isolation.blocked" in cond:
                assert r.get("target") != "end", (
                    f"路由 {r.get('name')} 用 target=end 终结管道，"
                    f"工具级拦截应转为失败结果由 tool_core 处理，而非终结管道"
                )

    def test_tool_execute_route_preserved(self):
        """tool_execute 路由必须保留（tool_core 在内部处理拦截）。"""
        cfg = _load_pipeline_yaml()
        names = [r.get("name") for r in cfg.get("input_routes", [])]
        assert "tool_execute" in names, "tool_execute 路由必须保留"


def _load_pipeline_yaml() -> dict:
    import yaml
    p = Path(__file__).resolve().parent.parent / "config" / "pipelines" / "default.yaml"
    with open(p, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ═══════════════════════════════════════════════════════════════
# P0: tool_core._check_tool_blocked — 拦截转失败结果
# ═══════════════════════════════════════════════════════════════


class TestCheckToolBlocked:
    """P0: _check_tool_blocked 把被拦截的工具转为失败结果。

    覆盖契约：三类拦截决策（level/isolation/security）命中时，
    返回 success=False 的失败结果 dict；未命中返回 None。
    """

    @staticmethod
    def _make_tool_core():
        from plugins.core.tool_core.plugin import ToolCore
        return ToolCore()

    def test_level_guard_block_returns_failure(self):
        """level_guard 拦截 → 返回失败结果，含权限原因。"""
        tc = self._make_tool_core()
        state = {
            "security.level_decision": {
                "allowed": False,
                "reason": "Agent level L1 not allowed to call: file_write",
                "blocked_tools": ["file_write"],
            },
        }

        result = tc._check_tool_blocked("file_write", state)

        assert result is not None
        assert result["success"] is False
        assert "file_write" in result["error"] or "权限" in result["error"]

    def test_level_guard_block_other_tool_not_affected(self):
        """level_guard 拦截 file_write 时，file_read 不被拦截。"""
        tc = self._make_tool_core()
        state = {
            "security.level_decision": {
                "allowed": False,
                "reason": "Agent level L1 not allowed to call: file_write",
                "blocked_tools": ["file_write"],
            },
        }

        result = tc._check_tool_blocked("file_read", state)
        assert result is None, "file_read 不在 blocked_tools 中，不应被拦截"

    def test_isolation_block_returns_failure(self):
        """isolation_guard 拦截 → 返回失败结果。"""
        tc = self._make_tool_core()
        state = {
            "execution_contexts": [
                {"tool_name": "bash_execute", "provider": "denied",
                 "blocked": True, "reason": "policy_fallback_denied"},
            ],
        }

        result = tc._check_tool_blocked("bash_execute", state)

        assert result is not None
        assert result["success"] is False
        assert "隔离" in result["error"]

    def test_security_check_block_returns_failure(self):
        """security_check 拦截 → 返回失败结果。"""
        tc = self._make_tool_core()
        state = {
            "security.decision": {
                "allowed": False,
                "reason": "危险操作 rm -rf /",
            },
        }

        result = tc._check_tool_blocked("bash_execute", state)

        assert result is not None
        assert result["success"] is False
        assert "安全" in result["error"]

    def test_no_block_decision_returns_none(self):
        """无任何拦截决策 → 返回 None（正常执行）。"""
        tc = self._make_tool_core()
        state = {}

        result = tc._check_tool_blocked("file_read", state)
        assert result is None

    def test_allowed_decision_returns_none(self):
        """拦截决策 allowed=True → 返回 None。"""
        tc = self._make_tool_core()
        state = {
            "security.level_decision": {"allowed": True, "reason": "ok"},
            "security.decision": {"allowed": True, "reason": "ok"},
        }

        result = tc._check_tool_blocked("file_write", state)
        assert result is None
