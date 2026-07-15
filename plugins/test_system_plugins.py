"""系统插件迁移测试——覆盖 AC-09-1 ~ AC-09-6。

验证 6 个系统插件（记忆/审批/评估/复盘/触发器/WebSocket适配器）的 MCP 工具注册和调用。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

# plugins/ 目录本身（测试文件就在 plugins/ 下）
_PLUGINS_DIR = str(Path(__file__).resolve().parent)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# 确保 SDK 在路径中
_SDK_SRC = str(Path(__file__).resolve().parent / "sdk" / "src")
if _SDK_SRC not in sys.path:
    sys.path.insert(0, _SDK_SRC)


def _load_plugin_module(plugin_name: str) -> Any:
    """动态加载插件模块并返回 module 对象。"""
    mod_name = f"{plugin_name}_server_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    spec = importlib.util.spec_from_file_location(
        mod_name,
        Path(_PLUGINS_DIR) / plugin_name / "server.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _call_tool(module: Any, tool_name: str, **kwargs: Any) -> Any:
    """调用插件注册的工具并返回结果。"""
    td = module.plugin._tools[tool_name]
    result = td.handler(**kwargs)
    if asyncio.iscoroutine(result):
        # 创建新事件循环运行协程（避免与 pytest-asyncio 冲突）
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(result)
        finally:
            loop.close()
    return result


# ═══════════════════════════════════════════════════════════
# AC-09-1: 记忆系统
# ═══════════════════════════════════════════════════════════

class TestMemoryPlugin:
    """验证记忆系统 MCP 服务。"""

    def test_memory_tools_registered(self) -> None:
        mod = _load_plugin_module("memory")
        assert "memory.search" in mod.plugin._tools
        assert "memory.store" in mod.plugin._tools
        assert "memory.summarize" in mod.plugin._tools

    def test_memory_resources_registered(self) -> None:
        mod = _load_plugin_module("memory")
        assert "memory://episode/recent" in mod.plugin._resources
        assert "memory://semantic/recent" in mod.plugin._resources

    def test_memory_store_and_search(self) -> None:
        mod = _load_plugin_module("memory")
        # Store
        result = _call_tool(mod, "memory.store", type="episode", content="Test memory about Python")
        assert result["stored"] is True
        assert "id" in result

        # Search
        result = _call_tool(mod, "memory.search", query="Python")
        assert result["total"] >= 1
        assert result["results"][0]["content"] == "Test memory about Python"

    def test_memory_search_empty(self) -> None:
        mod = _load_plugin_module("memory")
        result = _call_tool(mod, "memory.search", query="nonexistent_query_xyz")
        assert result["total"] == 0

    def test_memory_summarize(self) -> None:
        mod = _load_plugin_module("memory")
        # Store some data first
        _call_tool(mod, "memory.store", type="episode", content="Memory 1")
        _call_tool(mod, "memory.store", type="episode", content="Memory 2")

        result = _call_tool(mod, "memory.summarize", type="episode")
        assert result["count"] >= 2
        assert "Memory 1" in result["summary"]


# ═══════════════════════════════════════════════════════════
# AC-09-2: 审批系统
# ═══════════════════════════════════════════════════════════

class TestApprovalPlugin:
    """验证审批系统插件。"""

    def test_approval_tools_registered(self) -> None:
        mod = _load_plugin_module("approval")
        assert "approval.create_choice" in mod.plugin._tools
        assert "approval.create_conversation" in mod.plugin._tools
        assert "approval.submit" in mod.plugin._tools

    def test_create_choice(self) -> None:
        mod = _load_plugin_module("approval")
        result = _call_tool(
            mod, "approval.create_choice",
            title="Choose option",
            options=["A", "B", "C"],
        )
        assert result["status"] == "pending"
        assert result["mode"] == "choice"
        assert "approval_id" in result

    def test_create_conversation(self) -> None:
        mod = _load_plugin_module("approval")
        result = _call_tool(
            mod, "approval.create_conversation",
            message="Please review",
        )
        assert result["status"] == "pending"
        assert result["mode"] == "conversation"

    def test_submit_resolves(self) -> None:
        mod = _load_plugin_module("approval")
        created = _call_tool(mod, "approval.create_choice", title="T", options=["A"])
        result = _call_tool(
            mod, "approval.submit",
            approval_id=created["approval_id"],
            result="A",
        )
        assert result["status"] == "resolved"

    def test_submit_nonexistent(self) -> None:
        mod = _load_plugin_module("approval")
        result = _call_tool(mod, "approval.submit", approval_id="fake", result="X")
        assert "error" in result


# ═══════════════════════════════════════════════════════════
# AC-09-3: 评估系统
# ═══════════════════════════════════════════════════════════

class TestEvaluationPlugin:
    """验证评估系统插件。"""

    def test_evaluation_tools_registered(self) -> None:
        mod = _load_plugin_module("evaluation")
        assert "evaluation.run" in mod.plugin._tools
        assert "evaluation.get_result" in mod.plugin._tools

    def test_evaluation_run_pass(self) -> None:
        mod = _load_plugin_module("evaluation")
        result = _call_tool(
            mod, "evaluation.run",
            task_id="task_1",
            metrics=[
                {"metric_id": "m1", "type": "file_check", "params": {"path": "test.txt"}},
            ],
            gate_mode=True,
        )
        assert result["all_passed"] is True
        assert result["gated"] is False
        assert result["total"] == 1
        assert result["passed"] == 1

    def test_evaluation_run_fail_gate(self) -> None:
        mod = _load_plugin_module("evaluation")
        result = _call_tool(
            mod, "evaluation.run",
            task_id="task_2",
            metrics=[
                {"metric_id": "m1", "type": "file_check", "params": {}},
            ],
            gate_mode=True,
        )
        assert result["all_passed"] is False
        assert result["gated"] is True

    def test_evaluation_get_result(self) -> None:
        mod = _load_plugin_module("evaluation")
        run = _call_tool(
            mod, "evaluation.run",
            task_id="task_3",
            metrics=[{"metric_id": "m1", "type": "file_check", "params": {"p": "v"}}],
        )
        result = _call_tool(mod, "evaluation.get_result", eval_id=run["eval_id"])
        assert result["task_id"] == "task_3"


# ═══════════════════════════════════════════════════════════
# AC-09-4: 复盘系统
# ═══════════════════════════════════════════════════════════

class TestReviewPlugin:
    """验证复盘系统插件。"""

    def test_review_tools_registered(self) -> None:
        mod = _load_plugin_module("review")
        assert "review.trigger" in mod.plugin._tools
        assert "review.get_report" in mod.plugin._tools

    def test_trigger_review(self) -> None:
        mod = _load_plugin_module("review")
        result = _call_tool(
            mod, "review.trigger",
            task_id="task_1",
            summary="Completed data analysis",
            metrics={"accuracy": 0.95},
        )
        assert result["status"] == "completed"
        assert "review_id" in result

    def test_trigger_review_with_low_metrics(self) -> None:
        mod = _load_plugin_module("review")
        result = _call_tool(
            mod, "review.trigger",
            task_id="task_2",
            summary="Failed task",
            metrics={"accuracy": 0.3},
        )
        assert result["status"] == "completed"
        assert result["lessons_count"] >= 1

    def test_get_report(self) -> None:
        mod = _load_plugin_module("review")
        triggered = _call_tool(
            mod, "review.trigger",
            task_id="task_3",
            summary="Test",
        )
        report = _call_tool(mod, "review.get_report", review_id=triggered["review_id"])
        assert report["task_id"] == "task_3"
        assert "lessons" in report


# ═══════════════════════════════════════════════════════════
# AC-09-5: 触发器系统
# ═══════════════════════════════════════════════════════════

class TestTriggerPlugin:
    """验证触发器系统插件。"""

    def test_trigger_tools_registered(self) -> None:
        mod = _load_plugin_module("triggers")
        assert "trigger.register" in mod.plugin._tools
        assert "trigger.cancel" in mod.plugin._tools
        assert "trigger.list" in mod.plugin._tools

    def test_register_cron_trigger(self) -> None:
        mod = _load_plugin_module("triggers")
        result = _call_tool(
            mod, "trigger.register",
            type="cron",
            schedule="0 9 * * *",
            action={"pipeline": "daily_report"},
        )
        assert result["status"] == "registered"
        assert "trigger_id" in result

    def test_register_interval_trigger(self) -> None:
        mod = _load_plugin_module("triggers")
        result = _call_tool(
            mod, "trigger.register",
            type="interval",
            schedule="60",
            action={"pipeline": "health_check"},
        )
        assert result["status"] == "registered"

    def test_cancel_trigger(self) -> None:
        mod = _load_plugin_module("triggers")
        created = _call_tool(
            mod, "trigger.register",
            type="event",
            schedule="user_login",
            action={"pipeline": "welcome"},
        )
        result = _call_tool(mod, "trigger.cancel", trigger_id=created["trigger_id"])
        assert result["status"] == "cancelled"

    def test_list_triggers(self) -> None:
        mod = _load_plugin_module("triggers")
        _call_tool(
            mod, "trigger.register",
            type="cron",
            schedule="0 * * * *",
            action={"pipeline": "hourly"},
        )
        result = _call_tool(mod, "trigger.list")
        assert result["count"] >= 1

    def test_list_triggers_filtered(self) -> None:
        mod = _load_plugin_module("triggers")
        _call_tool(
            mod, "trigger.register",
            type="event",
            schedule="test_event",
            action={"pipeline": "p1"},
        )
        result = _call_tool(mod, "trigger.list", type="cron")
        # Should not contain event triggers
        for t in result["triggers"]:
            assert t["type"] == "cron"


# ═══════════════════════════════════════════════════════════
# AC-09-6: WebSocket 通道适配器
# ═══════════════════════════════════════════════════════════

class TestChannelPlugin:
    """验证 WebSocket 通道适配器。"""

    def test_channel_tools_registered(self) -> None:
        mod = _load_plugin_module("channel_ws")
        assert "channel.send_message" in mod.plugin._tools
        assert "channel.receive" in mod.plugin._tools
        assert "channel.broadcast" in mod.plugin._tools

    def test_send_to_disconnected_client(self) -> None:
        mod = _load_plugin_module("channel_ws")
        result = _call_tool(
            mod, "channel.send_message",
            client_id="unknown_client",
            message={"text": "hello"},
        )
        assert "error" in result

    def test_broadcast_no_clients(self) -> None:
        mod = _load_plugin_module("channel_ws")
        result = _call_tool(
            mod, "channel.broadcast",
            message={"event": "update"},
        )
        assert result["sent_count"] == 0

    def test_receive_empty(self) -> None:
        mod = _load_plugin_module("channel_ws")
        result = _call_tool(mod, "channel.receive")
        assert result["count"] == 0


# ═══════════════════════════════════════════════════════════
# AC-09-1~6: manifest 校验
# ═══════════════════════════════════════════════════════════

class TestManifestValidation:
    """验证全部 6 个插件的 plugin.json manifest 格式。"""

    @pytest.mark.parametrize("plugin_dir", [
        "memory", "approval", "evaluation", "review", "triggers", "channel_ws",
    ])
    def test_manifest_exists_and_valid(self, plugin_dir: str) -> None:
        manifest_path = Path(_PLUGINS_DIR) / plugin_dir / "plugin.json"
        assert manifest_path.exists(), f"plugin.json missing for {plugin_dir}"

        manifest = json.loads(manifest_path.read_text())
        assert "id" in manifest and manifest["id"]
        assert "name" in manifest and manifest["name"]
        assert "version" in manifest and manifest["version"]
        assert manifest["plugin_type"] == "system"
        assert manifest["host_type"] == "sidecar"
        assert "entry" in manifest
        assert "capabilities" in manifest
        assert "tools" in manifest["capabilities"]
        assert len(manifest["capabilities"]["tools"]) > 0

    @pytest.mark.parametrize("plugin_dir", [
        "memory", "approval", "evaluation", "review", "triggers", "channel_ws",
    ])
    def test_server_exists(self, plugin_dir: str) -> None:
        server_path = Path(_PLUGINS_DIR) / plugin_dir / "server.py"
        assert server_path.exists(), f"server.py missing for {plugin_dir}"
