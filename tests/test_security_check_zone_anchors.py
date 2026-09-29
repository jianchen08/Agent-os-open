# @feature: FP-0.2.六 安全与审批 | @ci: python-coverage
"""security_check 位置闸读链内建工作空间地界接线（用户裁定 2026-09-29）。

锁定契约（真实 zone_policy.read_verdict 路径 + 真实 repo_anchor 拒绝集，
不 mock 判定内核；名单/审批通道为外部依赖按缝钉桩）：
1. 读分支：workspace 在 .ai_workspaces 下时，兄弟工作树（sessions 兄弟
   目录）读放行且不弹读授权卡——修复前该路径撞仓库拒绝集必弹卡（前置
   事实在测试内以未传锚形态实证）；
2. 写分支：同一路径写仍走写区链硬拒（仓库自保），不因读链白名单放行、
   不弹授权卡（读写非对称，跨会话写污染防线保持）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests._pipeline_plugin_path import add_plugin_dir
from tests._security_check_harness import wire_approval_cap

pytestmark = pytest.mark.unit

add_plugin_dir("input", "security_check")

import plugin as sc_mod  # noqa: E402
import repo_anchor  # noqa: E402
import zone_policy  # noqa: E402
from pipeline.plugin import PluginContext  # noqa: E402
from plugin import SecurityCheckPlugin  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """假仓库根 + .ai_workspaces 装机同构布局（sessions/docs 兄弟）。"""
    root = tmp_path / "repo"
    (root / "config" / "kernel").mkdir(parents=True)
    users_dir = tmp_path / "users"
    (users_dir / "default").mkdir(parents=True)
    (users_dir / "default" / "project_whitelist.yaml").write_text(
        "entries: []\nread_allow: []\nread_deny: []\n", encoding="utf-8"
    )
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(root / "config"))
    monkeypatch.setenv("AGENTOS_CONFIG_USERS_DIR", str(users_dir))
    repo_anchor.reset_cache()
    yield root
    repo_anchor.reset_cache()


@pytest.fixture
def incident_layout(fake_repo: Path) -> dict[str, Path]:
    """事故同构：workspace = .ai_workspaces/<项目>__wt_x/sessions/<thread>，
    读取目标 = sessions 的兄弟目录 docs/working。"""
    ws = fake_repo / ".ai_workspaces" / "agentos_console__wt_x" / "sessions" / "thread-1"
    ws.mkdir(parents=True)
    sibling = fake_repo / ".ai_workspaces" / "agentos_console__wt_x" / "docs" / "working"
    sibling.mkdir(parents=True)
    return {"workspace": ws, "sibling": sibling}


def _tool_calls(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": name, "id": "call-zone-1", "args": args}]


def _zone_ctx(workspace: Path, tool_calls: list[dict[str, Any]]) -> PluginContext:
    """位置闸专用 ctx：workspace/project_root 锚 + raw_tool_calls 镜像
    （_soft_block 按 state 内调用清单产出预定拒绝条目）。"""
    return PluginContext(
        state={
            "core_type": "tool_execute",
            "workspace": str(workspace),
            "project_root": str(workspace),
            "raw_tool_calls": tool_calls,
            "messages": [],
        },
        _services={},
    )


class TestReadChainBuiltinZone:
    @pytest.mark.asyncio
    async def test_sibling_worktree_read_passes_without_grant_card(
        self, incident_layout: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws, sibling = incident_layout["workspace"], incident_layout["sibling"]

        # 前置事实（真实 read_verdict，修复前调用形态）：该路径确在读黑名单内
        allow, reason = zone_policy.read_verdict(sibling)
        assert not allow, "前置失真：兄弟工作树未传锚时应命中仓库拒绝集"
        assert reason is not None
        assert ".ai_workspaces" in reason

        _, counter = wire_approval_cap(sc_mod, [])
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        blocked = await plugin._enforce_zone_policy(
            _zone_ctx(ws, _tool_calls("file_read", {"path": str(sibling)})),
            _tool_calls("file_read", {"path": str(sibling)}),
            {},
        )

        assert blocked is None, "兄弟工作树读应放行（内建工作空间地界白名单命中）"
        assert counter.calls == 0, "白名单命中不得发起读授权卡"

    @pytest.mark.asyncio
    async def test_worktree_sibling_list_directory_read_passes(
        self, incident_layout: dict[str, Path]
    ) -> None:
        """事故同形操作：list_directory 列兄弟目录（2026-09-28 熔断同款工具）。"""
        ws, sibling = incident_layout["workspace"], incident_layout["sibling"]
        _, counter = wire_approval_cap(sc_mod, [])
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        blocked = await plugin._enforce_zone_policy(
            _zone_ctx(ws, _tool_calls("list_directory", {"path": str(sibling)})),
            _tool_calls("list_directory", {"path": str(sibling)}),
            {},
        )

        assert blocked is None
        assert counter.calls == 0


class TestWriteChainAsymmetry:
    @pytest.mark.asyncio
    async def test_sibling_worktree_write_still_hard_denied(
        self, incident_layout: dict[str, Path]
    ) -> None:
        ws, sibling = incident_layout["workspace"], incident_layout["sibling"]
        _, counter = wire_approval_cap(sc_mod, [])
        plugin = SecurityCheckPlugin(config={"enabled": True, "rules": []})

        blocked = await plugin._enforce_zone_policy(
            _zone_ctx(
                ws,
                _tool_calls("file_write", {"path": str(sibling / "out.txt"), "content": "x"}),
            ),
            _tool_calls("file_write", {"path": str(sibling / "out.txt"), "content": "x"}),
            {},
        )

        assert blocked is not None, "兄弟工作树写不得因读链白名单放行（写区链硬拒）"
        assert counter.calls == 0, "仓库自保硬拒不弹授权卡"
        entries = blocked.get("pre_decided_results") or []
        assert entries
        assert "运行时/产物区" in entries[0].get("error", "")
