# @feature: FP-0.2.二 工具面声明与动态定义集 | @vision: V2 全能闭环 | @ci: python-coverage
"""动态工具转发壳（call_tool）的权限判定键展开行为锁（安全核心）。

背景：动态工具**不在** `tool_ids` 白名单里，只经薄转发壳 `call_tool` 调用。
若判定链只按 `call_tool` 这个名字判定，则目标工具整体免审——真实越权面。
故 `security_check` 在三个判定入口（基础底线 / 位置闸 / 逐工具授权）与审批卡
指纹面统一展开为「目标工具名 + 目标参数」。

本文件锁的契约：

- 展开只认 `name == "call_tool"`（白名单式）：任意工具伪造 `tool`/`arguments`
  字段不参与判定（A13）。
- 目标是命令执行类危险工具 + 参数命中规则 → **走审批链**（测试环境交互通道
  缺席 = 预定拒绝 + `approval_channel_missing`），**非静默放行**。
- 目标是路径类工具时，位置闸/基础底线按**目标参数**执法（`../` 遍历命中
  目标参数即拦截）。
- 非转发壳调用判定行为零变化（回归保护）。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_dynamic_expand", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
SecurityCheckPlugin = _mod.SecurityCheckPlugin

_FORWARD_TOOL = "call_tool"


def _ctx(tool_calls: list[dict[str, Any]]) -> Any:
    """host provider + 非隔离的最小上下文（审批闸生效，非旁路）。"""
    state = {
        "core_type": "tool_execute",
        "raw_tool_calls": tool_calls,
        "execution_contexts": [{"provider": "host", "task_isolated": False}],
        "session_id": "sess-dynamic-expand",
    }

    def _get_service(name: str) -> Any:
        raise KeyError(name)

    return types.SimpleNamespace(state=state, get_service=_get_service)


def _forward(target: str, arguments: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"id": call_id, "name": _FORWARD_TOOL, "args": {"tool": target, "arguments": arguments}}


class TestEffectiveToolCall:
    def test_expands_forward_shell_to_target(self) -> None:
        """call_tool 条目展开为（目标工具名, 目标参数）。"""
        name, args = _mod._effective_tool_call(_forward("bash", {"command": "ls"}))
        assert name == "bash"
        assert args == {"command": "ls"}

    def test_non_forward_call_untouched(self) -> None:
        """非转发壳调用原样返回（其余判定链行为零变化）。"""
        name, args = _mod._effective_tool_call({"name": "bash", "args": {"command": "ls"}})
        assert name == "bash"
        assert args == {"command": "ls"}

    def test_other_tool_cannot_forge_target_field(self) -> None:
        """任意工具伪造 tool/arguments 字段不展开（白名单式，A13）。"""
        forged = {"name": "some_other_tool", "args": {"tool": "bash", "arguments": {"command": "rm -rf /"}}}
        name, _ = _mod._effective_tool_call(forged)
        assert name == "some_other_tool"

    def test_missing_target_falls_back_to_shell_name(self) -> None:
        """目标名缺失 → 按本工具原样判定（不明白放行成目标工具）。"""
        name, _ = _mod._effective_tool_call({"name": _FORWARD_TOOL, "args": {"arguments": {}}})
        assert name == _FORWARD_TOOL


class TestDangerousTargetTriggersGate:
    @pytest.mark.asyncio
    async def test_forward_to_dangerous_command_hits_approval_chain(self) -> None:
        """目标是命令执行类工具 + 参数命中危险规则 → 走审批链（非静默放行）。"""
        plugin = SecurityCheckPlugin(config={"enabled": True, "security_rules": {"mode": "blacklist"}})
        ctx = _ctx([_forward("bash", {"command": "rm -rf /tmp/dynamic-target"})])
        updates = (await plugin.execute(ctx)).state_updates

        entries = updates.get("pre_decided_results", [])
        assert entries, "动态调用危险目标必须产生预定拒绝/审批条目，禁止静默放行"
        assert entries[0]["metadata"].get("decided_by") == "security_check"
        assert "审批" in entries[0]["error"] or "拦截" in entries[0]["error"]

    @pytest.mark.asyncio
    async def test_forward_to_benign_target_not_blocked(self) -> None:
        """目标是普通只读工具 + 参数安全 → 不产生拦截条目（不误伤）。"""
        plugin = SecurityCheckPlugin(config={"enabled": True, "security_rules": {"mode": "blacklist"}})
        ctx = _ctx([_forward("file_read", {"path": "docs/a.md"})])
        updates = (await plugin.execute(ctx)).state_updates
        assert not updates.get("pre_decided_results")

    @pytest.mark.asyncio
    async def test_forward_to_task_tool_still_reaches_rules(self) -> None:
        """目标为任务类工具时同样进规则判定链（动态面不享有豁免）。

        用确定命中的路径遍历参数验证底线对目标参数生效；任务类目标的层级
        授权另由 level_guard 判定键展开用例覆盖。
        """
        plugin = SecurityCheckPlugin(config={"enabled": True, "security_rules": {"mode": "blacklist"}})
        ctx = _ctx([_forward("task_submit", {"path": "../../etc/passwd"})])
        updates = (await plugin.execute(ctx)).state_updates
        entries = updates.get("pre_decided_results", [])
        assert entries, "路径遍历命中必须拦截（动态面不得豁免基础底线）"


class TestBaseSafetyScanOnTargetArgs:
    def test_path_traversal_in_target_arguments_blocked(self) -> None:
        """基础底线按目标参数执法：目标参数含 ../ → 拦截。"""
        plugin = SecurityCheckPlugin(config={"enabled": True})
        ctx = _ctx([_forward("file_read", {"path": "../../etc/passwd"})])
        blocked = plugin._run_base_safety_scan(ctx, ctx.state["raw_tool_calls"])
        assert blocked is not None
        assert "路径遍历" in str(blocked.get("pre_decided_results", [{}])[0].get("error", ""))

    def test_sensitive_path_in_target_arguments_blocked(self) -> None:
        """敏感系统目录同样按目标参数命中。"""
        plugin = SecurityCheckPlugin(config={"enabled": True})
        # is_sensitive_path 按当前平台黑名单执法（Windows 拦 c:/windows 系、
        # Linux 拦 /etc 系）：跨平台各自真实验证本平台分支，非 skipif 弃测。
        sensitive = (
            "c:/windows/system32/drivers/etc/hosts"
            if sys.platform == "win32"
            else "/etc/shadow"
        )
        ctx = _ctx([_forward("file_read", {"path": sensitive})])
        blocked = plugin._run_base_safety_scan(ctx, ctx.state["raw_tool_calls"])
        assert blocked is not None

    def test_benign_target_arguments_pass(self) -> None:
        """普通工作区路径不拦（不误伤）。"""
        plugin = SecurityCheckPlugin(config={"enabled": True})
        ctx = _ctx([_forward("file_read", {"path": "docs/working/a.md"})])
        assert plugin._run_base_safety_scan(ctx, ctx.state["raw_tool_calls"]) is None


class TestUnknownTargetTool:
    """动态目标工具「未注册/未知」时安全面的行为锁（方案 §11 批 3 判据 4 的落点说明）。

    判据 3-4 原文要求「`call_tool` 目标是未注册工具 → 返回 `TOOL_NOT_FOUND` 语义的
    显式失败（不假成功）」。**该语义的实现落点是转发壳**（`search/call_tool.py`
    在目标不在本轮动态面时 fail-closed 返回 `TOOL_NOT_FOUND` 且**不发起转发**），
    原因：工具存在性的真值只有执行面持有（活注册表 + 本轮引入集），`security_check`
    是**参数级安全判定**插件，不持注册表——它无法也不应回答「这个工具存不存在」。

    故本组锁 security_check 侧**属于其职责**的那一半语义：
    - 目标名未知 **不构成豁免**：目标参数仍须过基础底线（未知名不得成为绕过通道）；
    - 条目名保留**原调用名**（`tool_core` 无 id 时按名兜底配对，写目标名会让预填
      拒绝失效），目标名只经 `metadata.target_tool` 下行。
    """

    @pytest.mark.asyncio
    async def test_unknown_target_still_goes_through_base_scan(self) -> None:
        """未知目标 + 路径遍历参数 → 仍被拦截（未知名不构成豁免通道）。"""
        plugin = SecurityCheckPlugin(config={"enabled": True, "security_rules": {"mode": "blacklist"}})
        ctx = _ctx([_forward("no_such_tool_xyz", {"path": "../../etc/passwd"})])
        updates = (await plugin.execute(ctx)).state_updates

        entries = updates.get("pre_decided_results", [])
        assert entries, "未知目标工具+危险参数必须拦截，不得因目标名未知而跳过判定"
        # 条目名 = 原调用名（tool_core 名字兜底可配对），目标名只走 metadata
        assert entries[0]["tool_name"] == _FORWARD_TOOL
        assert entries[0]["metadata"].get("target_tool") == "no_such_tool_xyz"
        assert entries[0]["metadata"].get("decided_by") == "security_check"

    @pytest.mark.asyncio
    async def test_unknown_target_with_safe_args_not_blocked(self) -> None:
        """未知目标 + 安全参数 → 不拦截（security_check 不冒充「工具存在性」判定）。"""
        plugin = SecurityCheckPlugin(config={"enabled": True, "security_rules": {"mode": "blacklist"}})
        ctx = _ctx([_forward("no_such_tool_xyz", {"path": "docs/a.md"})])
        updates = (await plugin.execute(ctx)).state_updates
        assert not updates.get("pre_decided_results")

    def test_prefill_entry_name_is_original_call_name(self) -> None:
        """预填条目名恒为原调用名（防 idless 兜底失配导致拦截不生效）。

        直接读源码文件断言：本插件经 importlib 自定义模块名装载，inspect 取不到源码
        （``getsource`` 会报 built-in class）。断言的是**结构性约束**而非文案——
        这两处一旦被改回「条目名用目标名」，idless 动态调用就会绕过预填拒绝。
        """
        src = (Path(_THIS_DIR) / "plugin.py").read_text(encoding="utf-8")
        assert "call_name or tool_name" in src, (
            "预填条目名必须用 tc['name']（原调用名）；写展开后的目标名会让 "
            "tool_core 的无 id 名字兜底失配、预填拒绝不生效"
        )
        assert 'entry_meta["target_tool"] = tool_name' in src, "目标名须经 metadata.target_tool 下行"
