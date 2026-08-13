# @feature: FP-0.2.二 内部模块manifest化 | @vision: V3 可嵌入 | @ci: python-plugins-test
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

pytestmark = pytest.mark.unit

# plugins/ 目录本身（测试文件就在 plugins/ 下）
_PLUGINS_DIR = str(Path(__file__).resolve().parent)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)

# 确保 SDK 在路径中
_SDK_SRC = str(Path(__file__).resolve().parent / "sdk" / "src")
if _SDK_SRC not in sys.path:
    sys.path.insert(0, _SDK_SRC)


# 插件名 → 功能分类目录的映射（插件已按功能分类组织）
_PLUGIN_CATEGORY_MAP: dict[str, str] = {
    "approval": "shared/system",
    "evaluation": "shared/system",
    "review": "shared/system",
    "builtin_tools": "shared/tools",
    "triggers": "shared/tools",
}


def _get_plugin_dir(plugin_name: str) -> Path:
    """根据插件名返回其所在的分类子目录路径。"""
    category = _PLUGIN_CATEGORY_MAP.get(plugin_name, "")
    if category:
        return Path(_PLUGINS_DIR) / category / plugin_name
    # 回退：直接在根目录查找（兼容未分类的插件）
    return Path(_PLUGINS_DIR) / plugin_name


def _load_plugin_module(plugin_name: str) -> Any:
    """动态加载插件模块并返回 module 对象。"""
    mod_name = f"{plugin_name}_server_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    plugin_path = _get_plugin_dir(plugin_name) / "server.py"
    spec = importlib.util.spec_from_file_location(
        mod_name,
        plugin_path,
    )
    assert spec is not None and spec.loader is not None, f"Cannot load plugin from {plugin_path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    # 模拟内核的 notifications/on_load：不触发则依赖 on_load 初始化的插件
    #（memory store / tasks service 等）无法工作，工具调用会静默降级。
    _fire_lifecycle(module, "on_load")
    return module


def _fire_lifecycle(module: Any, event: str) -> None:
    """触发插件生命周期钩子（与内核 notification 语义一致）。

    插件通过 @plugin.on_load 注册的处理器存在 _lifecycle_handlers 中；
    加载后手动触发，保证内存/任务服务等在工具调用前完成初始化。
    """
    plugin = getattr(module, "plugin", None)
    if plugin is None:
        return
    handler = plugin._lifecycle_handlers.get(event)
    if handler is None:
        return
    result = handler({})
    if asyncio.iscoroutine(result):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(result)
        finally:
            loop.close()


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

    def test_evaluation_run_pass(self, tmp_path) -> None:
        mod = _load_plugin_module("evaluation")
        # file_check 按 os.path.exists 判定，必须指向真实存在的文件（绝对路径，与 CWD 无关）
        target = tmp_path / "artifact.txt"
        target.write_text("artifact content")
        result = _call_tool(
            mod, "evaluation.run",
            task_id="task_1",
            metrics=[
                {"metric_id": "m1", "type": "file_check", "params": {"path": str(target)}},
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
# AC-09-1~6: manifest 校验
# ═══════════════════════════════════════════════════════════

class TestManifestValidation:
    """验证全部 5 个插件的 plugin.json manifest 格式。"""

    @pytest.mark.parametrize("plugin_dir", [
        "approval", "evaluation", "review", "triggers",
    ])
    def test_manifest_exists_and_valid(self, plugin_dir: str) -> None:
        manifest_path = _get_plugin_dir(plugin_dir) / "plugin.json"
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
        "approval", "evaluation", "review", "triggers",
    ])
    def test_server_exists(self, plugin_dir: str) -> None:
        server_path = _get_plugin_dir(plugin_dir) / "server.py"
        assert server_path.exists(), f"server.py missing for {plugin_dir}"


# ═══════════════════════════════════════════════════════════
# 监控 M7：monitoring 插件改走 record_metric 上报 D 类系统资源
# （监控设计 §三 D 类 + §十一 + 落地清单 M7）
# ═══════════════════════════════════════════════════════════


class TestMonitoringRecordMetric:
    """验证 monitoring 插件经 record_metric 上报 D 类系统资源（M7）。"""

    def _load_monitoring(self) -> Any:
        # monitoring 在 shared/system/monitoring/
        plugin_path = Path(_PLUGINS_DIR) / "shared" / "system" / "monitoring" / "server.py"
        spec = importlib.util.spec_from_file_location("monitoring_server_test", plugin_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["monitoring_server_test"] = module
        spec.loader.exec_module(module)
        return module

    def test_monitoring_uses_record_metric(self) -> None:
        """server.py 引用了 record_metric 上报路径（M7 改造标记）。"""
        mod = self._load_monitoring()
        # 验证后台上报函数存在
        assert hasattr(mod, "_report_system_metrics_once")
        assert hasattr(mod, "_report_metrics_loop")

    @pytest.mark.asyncio
    async def test_report_system_metrics_calls_record_metric(self) -> None:
        """_report_system_metrics_once 在 metrics capability 注入时调 record_metric。

        用 mock monitor + mock record_metric 验证：采到的系统指标经
        record_metric 上报（counter/gauge 分流，这里都是 gauge）。
        """
        mod = self._load_monitoring()

        # mock monitor.get_system_metrics 返回固定值
        class _FakeSystem:
            cpu_usage = 75.0
            memory_usage = 60.0
            disk_usage = 50.0
            network_sent = 12.3
            network_recv = 45.6

        class _FakeMonitor:
            async def get_system_metrics(self) -> Any:
                return _FakeSystem()

        # mock plugin.record_metric 记录调用
        recorded: list[tuple] = []

        async def _fake_record(name, value, metric_type="counter", labels=None, unit=None, help_text=None):
            recorded.append((name, value, metric_type, labels, unit))
            return {"status": "recorded"}

        with patch.object(mod, "_monitor", _FakeMonitor()), \
             patch.object(mod.plugin, "record_metric", _fake_record):
            await mod._report_system_metrics_once()

        # 应上报 5 个系统资源 gauge 指标
        names = [r[0] for r in recorded]
        assert "system.cpu_usage_ratio" in names
        assert "system.memory_usage_ratio" in names
        assert "system.disk_usage_ratio" in names
        assert "system.network_sent_kbytes_per_sec" in names
        assert "system.network_recv_kbytes_per_sec" in names
        # 全部 gauge 类型（系统资源是当前值）
        assert all(r[2] == "gauge" for r in recorded)
        # cpu 75% → 0.75 ratio
        cpu_rec = next(r for r in recorded if r[0] == "system.cpu_usage_ratio")
        assert abs(cpu_rec[1] - 0.75) < 0.001
        # labels 含 source=psutil
        assert all(r[3] == {"source": "psutil"} for r in recorded)

    @pytest.mark.asyncio
    async def test_report_silently_skips_when_capability_not_injected(self) -> None:
        """metrics capability 未注入（KeyError）→ 静默跳过，不阻断插件。"""
        mod = self._load_monitoring()

        class _FakeSystem:
            cpu_usage = 75.0
            memory_usage = 60.0
            disk_usage = 50.0
            network_sent = 0.0
            network_recv = 0.0

        class _FakeMonitor:
            async def get_system_metrics(self) -> Any:
                return _FakeSystem()

        async def _raise_keyerror(*args, **kwargs):
            raise KeyError("not injected")

        with patch.object(mod, "_monitor", _FakeMonitor()), \
             patch.object(mod.plugin, "record_metric", _raise_keyerror):
            # 不应抛异常
            await mod._report_system_metrics_once()
