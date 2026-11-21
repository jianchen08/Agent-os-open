# @feature: FP-0.2.二 工具面声明与动态定义集 | @vision: V2 全能闭环 | @ci: python-coverage
"""tool_schema 动态工具集（state["dynamic_tools"]）组装测试。

动态工具面批 1：动态工具定义集由插件侧 state 字段承载（内核零感知、不扩
`tool-surface` 契约、不进 `tool_schemas` 数组）。本文件锁的**可观察契约**：

- 来源：只认 `tool_results` 里 `resource_search` **成功**条目携带的结构化
  `tool_defs`；其他工具伪造同名字段不参与组装（定义真值 = manifest 声明）。
  同轮多条搜索**并列合并**（并行调用不许丢先完成的引入）。
- fail-closed：`name`/`description`/`plugin_id` 缺失、`input_schema` 缺失或
  **非 object** → 整条拒收并进 `dynamic_tools_errors`，**不静默补空对象**
  （补 `{}` = 零参数盲调）。
- 跨轮累积：`tool_results` 只承载上一工具迭代产出，动态集按 name 合并保留，
  不被后一轮无关工具结果冲掉；同名后到者覆盖。
- 隔离/worktree 会话按常驻面同一剥离清单剔除宿主 GUI 工具（动态面不得成为
  绕过隔离边界的暗道）。
- 条数封顶：超限丢最旧并进 errors。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)


def _load_plugin() -> Any:
    mod_name = "tool_schema_dynamic_tools_test"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(mod_name, Path(_THIS_DIR) / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_PLUGIN_MOD = _load_plugin()
ToolSchemaPlugin: Any = _PLUGIN_MOD.ToolSchemaPlugin


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(state: dict[str, Any] | None = None) -> Any:
    from _libs.pipeline_base.plugin import PluginContext

    return PluginContext(state=dict(state or {}))


class _FakeCaller:
    """tool-surface capability 轻量替身（外部依赖替身）：回放固定空面。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any], timeout: Any = None) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        return {"schemas": [], "contracts": {}}


def _def_item(name: str = "file_write", **overrides: Any) -> dict[str, Any]:
    """合法的动态定义项（manifest 声明形态）。"""
    item: dict[str, Any] = {
        "name": name,
        "description": "写文件",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        "output_schema": {"type": "object"},
        "render": {"card": "read"},
        "plugin_id": "agentos-builtin-tools",
        "source": "code",
    }
    item.update(overrides)
    return item


def _search_result(defs: list[Any], *, success: bool = True) -> dict[str, Any]:
    """构造 resource_search 的 tool_results 条目。"""
    return {
        "call_id": "c1",
        "tool_name": "resource_search",
        "success": success,
        "error": None,
        "data": {"tool_defs": defs, "tool_defs_count": len(defs)},
        "metadata": None,
        "duration_ms": 1.0,
    }


def _execute(state: dict[str, Any]) -> dict[str, Any]:
    p = ToolSchemaPlugin(config={})
    p.set_capability_caller(_FakeCaller())
    result = _run(p.execute(_make_ctx(state=state)))
    return result.state_updates


# ═══════════════════════════════════════════════════════════
# 来源与合法定义
# ═══════════════════════════════════════════════════════════


class TestDynamicToolsIngest:
    def test_valid_definition_lands_with_real_schema(self) -> None:
        """合法定义整条落入 dynamic_tools，input_schema 原样保真。"""
        updates = _execute({"tool_ids": [], "tool_results": [_search_result([_def_item()])]})
        defs = updates["dynamic_tools"]
        assert len(defs) == 1
        assert defs[0]["name"] == "file_write"
        assert defs[0]["input_schema"]["required"] == ["path"]
        assert defs[0]["plugin_id"] == "agentos-builtin-tools"
        assert defs[0]["degraded"] is False
        assert updates["dynamic_tools_errors"] == []

    def test_dynamic_tools_never_enter_tool_schemas(self) -> None:
        """动态类不进常驻工具面（不进 LLM 原生 tools 数组）。"""
        updates = _execute({"tool_ids": [], "tool_results": [_search_result([_def_item()])]})
        assert updates["tool_schemas"] == []
        assert len(updates["dynamic_tools"]) == 1

    def test_forged_defs_from_other_tool_ignored(self) -> None:
        """非声明生产者返回的同名字段不参与组装（定义真值只信 manifest 声明面）。"""
        forged = {
            "call_id": "c2",
            "tool_name": "bash_execute",
            "success": True,
            "data": {"tool_defs": [_def_item(name="rm_everything")]},
        }
        updates = _execute({"tool_ids": [], "tool_results": [forged]})
        assert updates["dynamic_tools"] == []

    def test_failed_search_result_ignored(self) -> None:
        """失败的 resource_search 条目不产生动态定义。"""
        updates = _execute({"tool_ids": [], "tool_results": [_search_result([_def_item()], success=False)]})
        assert updates["dynamic_tools"] == []


# ═══════════════════════════════════════════════════════════
# fail-closed 校验
# ═══════════════════════════════════════════════════════════


class TestDynamicToolsFailClosed:
    def test_missing_input_schema_rejected(self) -> None:
        """缺 input_schema → 整条拒收，且**不补空对象**。"""
        item = _def_item()
        del item["input_schema"]
        updates = _execute({"tool_ids": [], "tool_results": [_search_result([item])]})
        assert updates["dynamic_tools"] == []
        assert updates["dynamic_tools_errors"] == [{"name": "file_write", "reason": "input_schema_missing"}]

    def test_non_object_input_schema_rejected(self) -> None:
        """input_schema 非 object → 整条拒收。"""
        item = _def_item(input_schema={"type": "string"})
        updates = _execute({"tool_ids": [], "tool_results": [_search_result([item])]})
        assert updates["dynamic_tools"] == []
        assert updates["dynamic_tools_errors"] == [{"name": "file_write", "reason": "input_schema_not_object"}]

    def test_non_dict_input_schema_rejected(self) -> None:
        """input_schema 非 dict（字符串/列表脏数据）→ 整条拒收。"""
        updates = _execute({"tool_ids": [], "tool_results": [_search_result([_def_item(input_schema="oops")])]})
        assert updates["dynamic_tools"] == []
        assert updates["dynamic_tools_errors"][0]["reason"] == "input_schema_not_object"

    @pytest.mark.parametrize(
        ("override", "reason"),
        [
            ({"name": ""}, "name_missing"),
            ({"description": ""}, "description_missing"),
            ({"plugin_id": ""}, "plugin_id_missing"),
        ],
    )
    def test_required_field_missing_rejected(self, override: dict[str, Any], reason: str) -> None:
        """必填字段缺失/空 → 拒收（无路由目标或无描述即不可用）。"""
        updates = _execute({"tool_ids": [], "tool_results": [_search_result([_def_item(**override)])]})
        assert updates["dynamic_tools"] == []
        assert updates["dynamic_tools_errors"][0]["reason"] == reason

    def test_degraded_definition_kept(self) -> None:
        """可选契约（output_schema/render）缺失只降级，不阻断可用性。"""
        item = _def_item()
        del item["output_schema"]
        del item["render"]
        updates = _execute({"tool_ids": [], "tool_results": [_search_result([item])]})
        assert len(updates["dynamic_tools"]) == 1
        assert updates["dynamic_tools"][0]["degraded"] is True


# ═══════════════════════════════════════════════════════════
# 跨轮累积与上限
# ═══════════════════════════════════════════════════════════


class TestDynamicToolsLifecycle:
    def test_accumulates_across_rounds(self) -> None:
        """上一轮已引入的定义在无关工具结果轮次仍保留（不被冲掉）。"""
        first = _execute({"tool_ids": [], "tool_results": [_search_result([_def_item()])]})
        assert len(first["dynamic_tools"]) == 1

        unrelated = {
            "call_id": "c9",
            "tool_name": "bash_execute",
            "success": True,
            "data": {"stdout": "ok"},
        }
        second = _execute(
            {
                "tool_ids": [],
                "tool_results": [unrelated],
                "dynamic_tools": first["dynamic_tools"],
            }
        )
        assert [d["name"] for d in second["dynamic_tools"]] == ["file_write"]

    def test_same_turn_parallel_searches_all_merge(self) -> None:
        """同轮并行多个 resource_search：全部 tool_defs 合并，不只取最后一个。

        2026-11-18 矩阵 R2 §六.2 五轮复现：同轮 2/6 个搜索只有最后一条注册，
        先完成的引入全部丢失（调用侧表现为 TOOL_NOT_FOUND「不在本轮动态面」）。
        """
        updates = _execute(
            {
                "tool_ids": [],
                "tool_results": [
                    _search_result([_def_item(name="FileSystem")]),
                    _search_result([_def_item(name="Process")]),
                    _search_result([_def_item(name="PowerShell")]),
                ],
            }
        )
        assert [d["name"] for d in updates["dynamic_tools"]] == [
            "FileSystem",
            "Process",
            "PowerShell",
        ]

    def test_same_name_overwritten_by_newer_definition(self) -> None:
        """同名定义后到者覆盖（不重复堆积）。"""
        first = _execute({"tool_ids": [], "tool_results": [_search_result([_def_item(description="旧")])]})
        second = _execute(
            {
                "tool_ids": [],
                "tool_results": [_search_result([_def_item(description="新")])],
                "dynamic_tools": first["dynamic_tools"],
            }
        )
        assert len(second["dynamic_tools"]) == 1
        assert second["dynamic_tools"][0]["description"] == "新"

    def test_limit_drops_oldest(self) -> None:
        """超限丢最旧，最新引入的定义优先保留。"""
        limit = _PLUGIN_MOD.DYNAMIC_TOOL_LIMIT
        existing = [{"name": f"old_{i}", "description": "d", "input_schema": {"type": "object"}, "plugin_id": "p"}
                    for i in range(limit)]
        updates = _execute(
            {
                "tool_ids": [],
                "tool_results": [_search_result([_def_item(name="brand_new")])],
                "dynamic_tools": existing,
            }
        )
        names = [d["name"] for d in updates["dynamic_tools"]]
        assert len(names) == limit
        assert "brand_new" in names
        assert "old_0" not in names
        assert {"name": "old_0", "reason": "dynamic_tools_over_limit"} in updates["dynamic_tools_errors"]


# ═══════════════════════════════════════════════════════════
# 隔离边界：宿主 GUI 工具不得进动态面
# ═══════════════════════════════════════════════════════════


class TestBoundaryReliantStrip:
    def test_host_gui_tool_stripped_when_boundary_reliant(self) -> None:
        """隔离/worktree 会话：动态面同样剥离宿主 GUI 自动化工具（不成为暗道）。"""
        updates = _execute(
            {
                "tool_ids": [],
                "tool_results": [_search_result([_def_item(name="Click"), _def_item(name="file_read")])],
                # 无 execution_context = 缺省隔离（fail-closed 剥离）
            }
        )
        assert [d["name"] for d in updates["dynamic_tools"]] == ["file_read"]
        assert updates["dynamic_tools_errors"] == [
            {"name": "Click", "reason": "host_gui_tool_in_boundary_reliant_session"}
        ]

    def test_host_gui_tool_kept_in_non_isolated_session(self) -> None:
        """非隔离会话审批闸生效，不剥离宿主 GUI 工具。"""
        updates = _execute(
            {
                "tool_ids": [],
                "tool_results": [_search_result([_def_item(name="Click")])],
                "execution_context": {"isolation": {"level": "non_isolated"}, "workspace": {"mode": "plain"}},
            }
        )
        assert [d["name"] for d in updates["dynamic_tools"]] == ["Click"]


class TestDynamicRefs:
    """动态引用计数（方案 §6.2 / §11 批 4 判据 2-3）：只作观测/保活提示，不控制回收。

    口径：计数按 ``plugin_id`` 从当前定义集**每轮重算**——归零 **≠ 卸载**（回收
    仍由内核既有 idle GC 按空闲阈值决定），计数漏归零也不会把宿主永久钉住
    （不存在累积闭包状态，重算即自愈）。
    """

    def test_refs_counted_per_plugin_id(self) -> None:
        """同宿主多工具各计一条；不同宿主分开计数（性质：总数 = 定义条数）。"""
        updates = _execute({
            "tool_ids": [],
            "tool_results": [_search_result([
                _def_item(name="file_read"),
                _def_item(name="file_write"),
                _def_item(name="mcp_fetch", plugin_id="mcp_demo"),
            ])],
        })
        refs = updates["dynamic_tool_refs"]
        assert refs == {"agentos-builtin-tools": 2, "mcp_demo": 1}
        assert sum(refs.values()) == len(updates["dynamic_tools"])

    def test_refs_recomputed_from_current_set_not_accumulated(self) -> None:
        """计数从当前集重算：上一轮引用 I 撤出后本轮自然归零（不存在漏归零钉住）。"""
        # 第一轮：引入 mcp_demo 的一个工具
        first = _execute({
            "tool_ids": [],
            "tool_results": [_search_result([_def_item(name="mcp_fetch", plugin_id="mcp_demo")])],
        })
        assert first["dynamic_tool_refs"] == {"mcp_demo": 1}
        # 第二轮：state 里定义已撤出（模拟宿主卸载/定义过期），计数归零
        second = _execute({
            "tool_ids": [],
            "dynamic_tools": [],
            "tool_results": [],
        })
        assert second["dynamic_tool_refs"] == {}
        assert second["dynamic_tools"] == []

    def test_zero_refs_does_not_trigger_unload(self) -> None:
        """引用归零**不触发任何立即卸载**：更新面只有工具面三键 + 计数，无卸载动作。"""
        updates = _execute({"tool_ids": [], "dynamic_tools": [], "tool_results": []})
        assert updates["dynamic_tool_refs"] == {}
        # 插件更新面不含任何卸载/停止/注销语义的键（回收归内核 idle GC，非本插件职责）
        assert not any(
            k in str(updates) for k in ("unload_host", "stop_plugin", "unregister", "kill_host")
        )

    def test_refs_are_observational_only(self) -> None:
        """计数只写 state（观测/保活提示），不影响工具面产出与拒收结果。"""
        updates = _execute({
            "tool_ids": [],
            "tool_results": [_search_result([
                _def_item(name="file_read"),
                _def_item(name="bad_tool", input_schema={"type": "array"}),
            ])],
        })
        # 拒收结果不受引用计数影响
        assert updates["dynamic_tools_errors"] == [
            {"name": "bad_tool", "reason": "input_schema_not_object"}
        ]
        # 计数只统计成功落入的定义
        assert updates["dynamic_tool_refs"] == {"agentos-builtin-tools": 1}
        # 动态类仍不进常驻工具面
        assert updates["tool_schemas"] == []

    def test_disabled_clears_refs(self) -> None:
        """禁用路径写空表（防上一轮残留把已撤出的宿主显示成仍被引用）。"""
        p = ToolSchemaPlugin(config={"enabled": False})
        p.set_capability_caller(_FakeCaller())
        updates = _run(p.execute(_make_ctx(state={"tool_ids": ["x"]}))).state_updates
        assert updates["dynamic_tool_refs"] == {}


# ═══════════════════════════════════════════════════════════
# manifest 声明面契约：一次引入长期有效（volatile 退役）
# ═══════════════════════════════════════════════════════════


class TestManifestContract:
    """manifest 声明面契约：动态集「一次引入长期有效」由 volatile_keys 退役承载。

    2026-11-21 用户裁定：动态工具一经引入即长期有效，不再需要重复
    resource_search。此前 plugin.json 把 dynamic_tools 声明进
    state.volatile_keys（per-run 键），内核每轮派发恢复合并时跳过
    （stages.rs merge_recovered_scalars）+ checkpoint 落档剥离
    （store.rs save_checkpoint），run 边界动态面被清空——与 call_tool
    错误文案「已引入的定义跨轮持续有效」矛盾，模型在新 run 必然重搜。
    本契约锁定声明面：dynamic_tools 必须持久（不在 volatile_keys），
    仍可被读取（reads）且跨进程出口（export_fields）。
    """

    @staticmethod
    def _manifest() -> dict[str, Any]:
        return json.loads((Path(_THIS_DIR) / "plugin.json").read_text(encoding="utf-8"))

    def test_dynamic_tools_not_declared_volatile(self) -> None:
        """dynamic_tools 不在 volatile_keys——跨 run 持久（一次引入长期有效）。"""
        manifest = self._manifest()
        volatile = (manifest.get("state") or {}).get("volatile_keys") or []
        assert "dynamic_tools" not in volatile

    def test_dynamic_tools_still_readable_and_exported(self) -> None:
        """dynamic_tools 仍在 reads（可消费）与 export_fields（跨进程出口）。"""
        manifest = self._manifest()
        state = manifest.get("state") or {}
        assert "dynamic_tools" in (state.get("reads") or [])
        assert "dynamic_tools" in (manifest.get("export_fields") or [])


# ═══════════════════════════════════════════════════════════
# 跨 run 残留的存量动态面：隔离边界整面剥离
# ═══════════════════════════════════════════════════════════


class TestBoundaryReliantStripResidual:
    """dynamic_tools 退役 volatile 后，隔离会话必须对「存量+新增」整面剥离。

    引入持久化后，隔离/worktree 会话里上一 run 引入的定义会残留进本轮
    state——若只对新增 tool_defs 剥离宿主 GUI 工具，残留面就成了绕过
    隔离边界的暗道。契约：隔离会话对合并后的整面剥离宿主 GUI 工具；
    非隔离会话存量照常保留。
    """

    def test_residual_host_gui_tool_stripped_in_boundary_reliant_session(self) -> None:
        """隔离会话：上一 run 残留的宿主 GUI 定义在合并后被剥出动态面。"""
        residual = [_def_item(name="Click", plugin_id="external_mcp")]
        updates = _execute(
            {
                "tool_ids": [],
                "dynamic_tools": residual,  # 跨 run 残留（恢复合并产物）
                "tool_results": [],  # 本轮零新增
            }
        )
        assert updates["dynamic_tools"] == []
        assert updates["dynamic_tools_errors"] == [
            {"name": "Click", "reason": "host_gui_tool_in_boundary_reliant_session"}
        ]

    def test_residual_non_gui_tool_survives_run_boundary(self) -> None:
        """非 GUI 存量定义跨 run 保留（一次引入长期有效的动态面本体）。"""
        residual = [_def_item(name="file_read")]
        updates = _execute(
            {
                "tool_ids": [],
                "dynamic_tools": residual,
                "tool_results": [],
            }
        )
        assert [d["name"] for d in updates["dynamic_tools"]] == ["file_read"]
        assert updates["dynamic_tools_errors"] == []

    def test_residual_gui_tool_kept_in_non_isolated_session(self) -> None:
        """非隔离会话：存量宿主 GUI 定义照常保留（审批闸生效）。"""
        residual = [_def_item(name="Click", plugin_id="external_mcp")]
        updates = _execute(
            {
                "tool_ids": [],
                "dynamic_tools": residual,
                "tool_results": [],
                "execution_context": {"isolation": {"level": "non_isolated"}, "workspace": {"mode": "plain"}},
            }
        )
        assert [d["name"] for d in updates["dynamic_tools"]] == ["Click"]
        assert updates["dynamic_tools_errors"] == []
