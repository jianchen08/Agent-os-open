# @feature: FP-0.2.二 工具面声明与动态定义集 | @vision: V2 全能闭环 | @ci: python-coverage
"""动态工具薄转发壳 ``call_tool`` 行为测试。

锁的可观察契约（动态工具面批 1）：

- **单一入口 + 只信状态定义**：目标工具须在 ``dynamic_tools`` 里（由
  ``resource_search`` 的 ``tool_defs`` 引入），模型回传的定义不作真值；未引入
  的名字显式失败（``TOOL_NOT_FOUND`` 语义），不假成功。
- **参数解析后直接透传**（2026-11-18 用户裁定）：壳不做目标 ``input_schema``
  校验（缺必填/类型不符/未知键均由目标自身 handler/schema 终审）；``arguments``
  为 JSON 字符串时先解析为对象，非对象/解析失败 → ``INVALID_ARGUMENTS``，
  **不转发执行**。
- **黑名单门禁**：``tool_deny`` 否决集在转发前拒绝（壳唯一校验面）。
- **转发走既有执行面**：复用 ``tool-executor.invoke`` 通道（外部依赖，打桩合规），
  成功后信封带目标工具名；目标失败信封（``PLUGIN_CRASHED`` 等）**原样上行**，
  不吞异常、不编造结果。
- **管道隔离**：聚合行按 ``pipeline_id`` 过滤，不读他管道引入的定义；管道身份
  未知/通道未注入一律 fail-closed（不猜、不放行）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
for _p in (_THIS_DIR, _SHARED_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import call_tool as ct  # noqa: E402 —— 同目录模块（sys.path 装配后导入）

from agentos_plugin_sdk.logging import LogContext  # noqa: E402

_PIPELINE_ID = "pipe-batch1"


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _definition(name: str = "file_write", **overrides: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "name": name,
        "description": "写文件",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        "plugin_id": "agentos-builtin-tools",
    }
    item.update(overrides)
    return item


def _rows(defs: list[dict[str, Any]], *, pipeline_id: str = _PIPELINE_ID) -> list[dict[str, Any]]:
    """``pipeline-state.list`` 聚合行形态（**与内核真实出口一致**）。

    内核 ``summarize_state``（``kernel/crates/api/src/routes.rs``）把 manifest
    ``export_fields`` 声明的键**平铺在行顶层**，再补 ``pipeline_id``/``thread_id``/
    ``agent_id``/``source``。故 ``dynamic_tools`` 就在行根上，不是 ``row["state"]``。

    这里曾写成 ``{"state": {"dynamic_tools": ...}}`` 的嵌套形态——与真实出口不符，
    导致「测试全绿、真实链路恒返回空列表」的假绿（缺陷靠活体验证才暴露）。
    """
    return [{"pipeline_id": pipeline_id, "source": "memory", "dynamic_tools": defs}]


class _Recorder:
    """执行面转发替身（外部依赖 = 内核 tool-executor.invoke）。"""

    def __init__(self, result: Any = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self._result = result if result is not None else {"success": True, "output": {"ok": True}}

    async def __call__(self, tool_name: str, args: dict[str, Any], plugin_id: str) -> Any:
        self.calls.append((tool_name, args, plugin_id))
        return self._result


@pytest.fixture(autouse=True)
def _wire_cleanup() -> Any:
    """每个用例前后摘除接线（模块级全局，防跨用例污染）。"""
    ct.set_state_reader(None)
    ct.set_tool_invoker(None)
    yield
    ct.set_state_reader(None)
    ct.set_tool_invoker(None)


def _wire(defs: list[dict[str, Any]], recorder: _Recorder | None = None) -> _Recorder:
    rec = recorder or _Recorder()

    async def _state_rows() -> Any:
        return _rows(defs)

    ct.set_state_reader(_state_rows)
    ct.set_tool_invoker(rec)
    return rec


def _execute(payload: dict[str, Any]) -> Any:
    with LogContext.scoped(pipeline_id=_PIPELINE_ID):
        return _run(ct.CallTool().execute(payload))


class TestForwarding:
    def test_forwards_to_existing_executor(self) -> None:
        """引入的定义 + 合规参数 → 转发既有执行面，信封带目标工具名。"""
        rec = _wire([_definition()])
        result = _execute({"tool": "file_write", "arguments": {"path": "a.txt", "content": "x"}})
        assert result.success is True
        assert result.output["tool_name"] == "file_write"
        assert result.output["ok"] is True
        assert rec.calls == [("file_write", {"path": "a.txt", "content": "x"}, "agentos-builtin-tools")]

    def test_second_target_uses_its_own_definition(self) -> None:
        """多定义场景按名字选中正确的那条（不同 target 走不同定义/plugin_id）。"""
        rec = _wire([
            _definition(name="file_write", plugin_id="builtin"),
            _definition(name="bash", plugin_id="bash_tool"),
        ])
        result = _execute({"tool": "bash", "arguments": {"path": "cmd"}})
        assert result.success is True
        assert rec.calls[0][0] == "bash"
        assert rec.calls[0][2] == "bash_tool"

    def test_target_failure_envelope_passes_through(self) -> None:
        """目标失败（PLUGIN_CRASHED）原样上行，不吞异常、不编造成功。"""
        _wire([_definition()], _Recorder({"success": False, "error": "sidecar 崩溃", "error_code": "PLUGIN_CRASHED"}))
        result = _execute({"tool": "file_write", "arguments": {"path": "a.txt"}})
        assert result.success is True  # 转发本身成功
        assert result.output["success"] is False
        assert result.output["error_code"] == "PLUGIN_CRASHED"
        assert "崩溃" in result.output["error"]

    def test_timeout_envelope_passes_through(self) -> None:
        """超时类失败信封同样上行（不挂死、不静默）。"""
        _wire([_definition()], _Recorder({"success": False, "error": "timeout", "error_code": "TOOL_TIMEOUT"}))
        result = _execute({"tool": "file_write", "arguments": {"path": "a.txt"}})
        assert result.output["error_code"] == "TOOL_TIMEOUT"


class TestFailClosed:
    def test_target_not_introduced_is_not_found(self) -> None:
        """未引入的工具名 → 显式 TOOL_NOT_FOUND（不转发、不假成功）。"""
        rec = _wire([_definition()])
        result = _execute({"tool": "bash", "arguments": {"path": "x"}})
        assert result.success is False
        assert result.error_code == "TOOL_NOT_FOUND"
        assert rec.calls == []

    def test_empty_dynamic_surface_is_not_found(self) -> None:
        """动态面为空（本轮未搜索）→ 任何目标都不可调用。"""
        rec = _wire([])
        result = _execute({"tool": "file_write", "arguments": {"path": "a.txt"}})
        assert result.success is False
        assert result.error_code == "TOOL_NOT_FOUND"
        assert rec.calls == []

    def test_arguments_forwarded_without_schema_validation(self) -> None:
        """壳不做 input_schema 校验：缺必填/类型不符/未知键一律原样透传。"""
        rec = _wire([_definition()])
        args = {"content": "x", "path": 123, "bogus": 1}
        result = _execute({"tool": "file_write", "arguments": args})
        assert result.success is True, f"透传不得被 schema 校验拦下: {result.error}"
        assert len(rec.calls) == 1
        assert rec.calls[0][1] == args

    def test_underscore_keys_forwarded_verbatim(self) -> None:
        """``_`` 前缀服务端注入键原样透传（isolation_guard 写入内层 arguments）。

        丢弃它们等于让隔离失效；壳不做未知键判定（参数合法性终审归目标工具
        自身 handler/schema——2026-11-18 用户裁定：壳只解析并透传）。
        """
        rec = _wire([_definition()])
        result = _execute({
            "tool": "file_write",
            "arguments": {
                "path": "a.txt",
                "content": "x",
                "_container_id": "ctr-123",
                "_exec_backend": "docker",
            },
        })

        assert result.success is True, f"注入键不得触发拒收: {result.error}"
        assert len(rec.calls) == 1
        forwarded = rec.calls[0][1]
        assert forwarded["_container_id"] == "ctr-123"
        assert forwarded["_exec_backend"] == "docker"
        assert forwarded["path"] == "a.txt"

    def test_missing_target_name_rejected(self) -> None:
        """缺 tool 参数 → INVALID_ARGUMENTS。"""
        _wire([_definition()])
        result = _execute({"tool": "", "arguments": {}})
        assert result.success is False
        assert result.error_code == "INVALID_ARGUMENTS"

    def test_state_channel_not_wired_fails_closed(self) -> None:
        """动态定义读取通道未接线 → DYNAMIC_TOOLS_UNAVAILABLE（不猜空面）。"""
        ct.set_tool_invoker(_Recorder())
        result = _execute({"tool": "file_write", "arguments": {"path": "a.txt"}})
        assert result.success is False
        assert result.error_code == "DYNAMIC_TOOLS_UNAVAILABLE"

    def test_unknown_pipeline_identity_fails_closed(self) -> None:
        """管道身份未知（日志上下文无 pipeline_id）→ 不读聚合行、显式失败。"""
        rec = _Recorder()
        rows_called: list[int] = []

        async def _rows_probe() -> Any:
            rows_called.append(1)
            return _rows([_definition()])

        ct.set_state_reader(_rows_probe)
        ct.set_tool_invoker(rec)
        with LogContext.scoped(pipeline_id=""):
            result = _run(ct.CallTool().execute({"tool": "file_write", "arguments": {"path": "a.txt"}}))
        assert result.success is False
        assert result.error_code == "DYNAMIC_TOOLS_UNAVAILABLE"
        assert rows_called == []  # 身份未知不读全量聚合行
        assert rec.calls == []

    def test_other_pipeline_definitions_not_visible(self) -> None:
        """他管道引入的定义不可见（聚合行按 pipeline_id 过滤）。"""
        rec = _Recorder()

        async def _other_rows() -> Any:
            return _rows([_definition()], pipeline_id="pipe-other")

        ct.set_state_reader(_other_rows)
        ct.set_tool_invoker(rec)
        result = _execute({"tool": "file_write", "arguments": {"path": "a.txt"}})
        assert result.success is False
        assert result.error_code == "TOOL_NOT_FOUND"
        assert rec.calls == []

    def test_invoker_channel_not_wired_fails_closed(self) -> None:
        """执行面通道未接线 → TOOL_EXECUTOR_UNAVAILABLE。"""

        async def _state_rows() -> Any:
            return _rows([_definition()])

        ct.set_state_reader(_state_rows)
        result = _execute({"tool": "file_write", "arguments": {"path": "a.txt"}})
        assert result.success is False
        assert result.error_code == "TOOL_EXECUTOR_UNAVAILABLE"


class TestToolDefinition:
    def test_tool_definition_contract(self) -> None:
        """LLM 可见声明：名叫 call_tool，带 object input_schema 与 output_schema。"""
        tool = ct.CallTool.get_tool_definition()
        assert tool.name == "call_tool"
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema["required"] == ["tool", "arguments"]
        assert tool.output_schema["type"] == "object"
        assert "tool" in tool.description or "动态工具" in tool.description


class TestPipelineIdentityFromCallContext:
    """工具调用路径的真实身份来源（内核 dispatch 期写入入参的 ``_call_context``）。

    这一组**不调用** ``LogContext.scoped()``——生产工具路径不注入 ``_log_ctx``，
    该上下文恒为空。若只在日志上下文里给身份，测试会假绿而真实链路必然 fail-closed
    （这正是批 1 漏网的形态），故此处只走内核真正会提供的入参来源。
    """

    def test_forwards_with_identity_from_inputs_only(self) -> None:
        """仅凭入参 ``_call_context.pipeline_id`` 即可读到定义并转发成功。"""
        rec = _wire([_definition()])
        result = _run(
            ct.CallTool().execute(
                {
                    "tool": "file_write",
                    "arguments": {"path": "a.txt", "content": "x"},
                    "_call_context": {"pipeline_id": _PIPELINE_ID, "call_id": "c1"},
                }
            )
        )
        assert result.success is True
        assert rec.calls[0][0] == "file_write"

    def test_inputs_identity_wins_over_log_context(self) -> None:
        """入参身份优先于日志上下文（工具路径只有前者，且它才是权威值）。"""
        rec = _wire([_definition()])
        result = _run(
            ct.CallTool().execute(
                {
                    "tool": "file_write",
                    "arguments": {"path": "a.txt"},
                    "_call_context": {"pipeline_id": _PIPELINE_ID},
                }
            )
        )
        assert result.success is True
        assert rec.calls, "入参携身份时必须已转发"

        # 反向：入参指向他管道 → 读不到本管道定义（隔离生效，不是"哪个管道都行"）
        other = _run(
            ct.CallTool().execute(
                {
                    "tool": "file_write",
                    "arguments": {"path": "a.txt"},
                    "_call_context": {"pipeline_id": "pipe-other"},
                }
            )
        )
        assert other.success is False
        assert other.error_code == "TOOL_NOT_FOUND"
        assert len(rec.calls) == 1, "他管道身份不得读到本管道定义"

    def test_placeholder_identity_fails_closed(self) -> None:
        """``"-"`` / 空串占位不算身份 → 显式 DYNAMIC_TOOLS_UNAVAILABLE。"""
        rec = _wire([_definition()])
        for placeholder in ("-", "", "   "):
            result = _run(
                ct.CallTool().execute(
                    {
                        "tool": "file_write",
                        "arguments": {"path": "a.txt"},
                        "_call_context": {"pipeline_id": placeholder},
                    }
                )
            )
            assert result.success is False
            assert result.error_code == "DYNAMIC_TOOLS_UNAVAILABLE", placeholder
        assert rec.calls == []

    def test_non_dict_call_context_fails_closed(self) -> None:
        """``_call_context`` 形态异常（非对象）不猜身份，fail-closed。"""
        rec = _wire([_definition()])
        result = _run(
            ct.CallTool().execute(
                {"tool": "file_write", "arguments": {"path": "a.txt"}, "_call_context": "pipe-x"}
            )
        )
        assert result.success is False
        assert result.error_code == "DYNAMIC_TOOLS_UNAVAILABLE"
        assert rec.calls == []

    def test_pipeline_id_reads_only_own_rows(self) -> None:
        """聚合行含多管道时只取自己那行（跨会话越权面防线）。"""
        rec = _Recorder()

        async def _rows_multi() -> Any:
            return [
                {"pipeline_id": "pipe-other", "dynamic_tools": [_definition(name="bash")]},
                {"pipeline_id": _PIPELINE_ID, "dynamic_tools": [_definition()]},
            ]

        ct.set_state_reader(_rows_multi)
        ct.set_tool_invoker(rec)
        result = _run(
            ct.CallTool().execute(
                {
                    "tool": "file_write",
                    "arguments": {"path": "a.txt"},
                    "_call_context": {"pipeline_id": _PIPELINE_ID},
                }
            )
        )
        assert result.success is True
        assert rec.calls[0][0] == "file_write", "只能命本管道那条定义"

class TestSnapshotFromToolArgs:
    """工具调用路径的**主来源**：``tool_args_inject`` 同轮注入的 ``_dynamic_tools``。

    内核只在 pipeline 调用路径注入 ``_log_ctx``（``invoke_paths.rs:65,103``），
    工具调用路径没有该上下文；``pipeline-state.list`` 又只在 run 收尾后刷新
    （``stage_exec.rs:436``），run 进行中读不到本轮定义。故真实链路的定义来源
    只能是入参快照——本组用例**不绑 LogContext、不接 state reader**，只给快照，
    确保「无管道身份也必须有定义可读」这条成立。
    """

    def test_snapshot_alone_sufficient_without_identity_or_state_reader(self) -> None:
        """仅入参快照（无 `_call_context`、无 state reader、无 LogContext）→ 转发成功。"""
        rec = _Recorder()
        ct.set_tool_invoker(rec)  # 故意不接线 state reader：快照路径不该碰它
        result = _run(
            ct.CallTool().execute(
                {
                    "tool": "file_write",
                    "arguments": {"path": "a.txt", "content": "x"},
                    "_dynamic_tools": [_definition()],
                }
            )
        )
        assert result.success is True
        assert rec.calls == [("file_write", {"path": "a.txt", "content": "x"}, "agentos-builtin-tools")]

    def test_snapshot_wins_over_state_rows(self) -> None:
        """快照优先于回落读面（读面是 run 收尾的旧值，快照才是本轮真值）。"""
        rec = _Recorder()

        async def _stale_rows() -> Any:
            return [{"pipeline_id": _PIPELINE_ID, "dynamic_tools": [_definition(name="bash")]}]

        ct.set_state_reader(_stale_rows)
        ct.set_tool_invoker(rec)
        result = _run(
            ct.CallTool().execute(
                {
                    "tool": "file_write",
                    "arguments": {"path": "a.txt"},
                    "_dynamic_tools": [_definition()],
                    "_call_context": {"pipeline_id": _PIPELINE_ID},
                }
            )
        )
        assert result.success is True
        assert rec.calls[0][0] == "file_write", "本轮快照目标必须可调用（不被旧读面顶掉）"

    def test_snapshot_limits_surface_to_introduced_tools(self) -> None:
        """快照只认本轮引入的工具：不在快照里的名字显式 TOOL_NOT_FOUND，不转发。"""
        rec = _Recorder()
        ct.set_tool_invoker(rec)
        result = _run(
            ct.CallTool().execute(
                {
                    "tool": "bash",
                    "arguments": {"command": "ls"},
                    "_dynamic_tools": [_definition(name="file_write")],
                }
            )
        )
        assert result.success is False
        assert result.error_code == "TOOL_NOT_FOUND"
        assert rec.calls == []

    def test_empty_snapshot_reports_nothing_introduced(self) -> None:
        """空快照（本轮未引入）→ TOOL_NOT_FOUND 且明示 "无"，不 fail-closed 报不可用。"""
        result = _run(
            ct.CallTool().execute(
                {"tool": "file_write", "arguments": {"path": "a.txt"}, "_dynamic_tools": []}
            )
        )
        assert result.success is False
        assert result.error_code == "TOOL_NOT_FOUND"
        assert "无" in result.error


class TestExternalMcpForwarding:
    """external MCP 工具经转发壳调用（**只锁插件侧契约**，方案 §11 批 4 判据 4/5 的一半）。

    转发壳走同一条 ``tool-executor.invoke``；注入已由 ``tool_args_inject`` 前瞻
    完成（MCP 目标的跳过判定在注入方按 ``definition.source == "mcp"`` 执行，
    壳本身不注入、不剥）。本组如实只锁插件侧可观察贡献，**不冒充内核行为已验证**
    （审查 SF#4）：

    - **注入键不泄漏**（可断言）：MCP 目标的转发 payload 只有 ``tool_name`` /
      ``args`` / ``plugin_id``；``_call_context`` / ``_dynamic_tools`` / ``_log_ctx``
      / ``session_id`` / ``workspace`` 等调用期注入键一律不进目标参数。
    - **失败信封不吞不改写**（可断言）：执行面返回什么，壳就上行什么——壳不重写
      错误文本、不把失败包装成成功。

    **桩与真实出口一致**（教训：``_rows`` 曾因桩形态不符造成假绿）：真实定义面
    必带 ``source`` 标记（``code`` / ``mcp``），故本组 MCP 桩显式带 ``source="mcp"``。

    **本组不能证明的（须内核侧用例，见 ADR 影响段）**：external MCP 参数剥离
    （``sanitize_external_mcp_arguments``）、stdio 宿主的进程树杀与无残留
    （``McpClient::kill`` → ``kill_process_tree``）。二者都是内核既有实现、本方案
    零改动；本组用固定桩 dict，**不 spawn 宿主、不杀进程**——把实现改成恒返回该
    dict 也能全绿，故不得据此宣称树杀/剥离已验证。
    """

    def test_mcp_tool_forwarded_without_injection_keys(self) -> None:
        """MCP 目标工具的转发参数 = 模型给的目标参数，注入键不入 arg。"""
        mcp_def = _definition(
            name="mcp_fetch",
            plugin_id="mcp_demo",
            source="mcp",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        )
        rec = _wire([mcp_def])
        result = _execute({
            "tool": "mcp_fetch",
            "arguments": {"url": "https://example.invalid/x"},
            # 以下都是调用期注入键，必须留在壳这层、不进目标参数
            "_dynamic_tools": [mcp_def],
            "_call_context": {"pipeline_id": _PIPELINE_ID},
            "session_id": "s-1",
            "workspace": "/tmp/ws",
        })

        assert result.success is True
        assert len(rec.calls) == 1
        name, args, plugin_id = rec.calls[0]
        assert name == "mcp_fetch"
        assert plugin_id == "mcp_demo"
        # 性质断言：目标参数与模型给的完全一致，且不含任何注入键
        assert args == {"url": "https://example.invalid/x"}
        assert not ({"_dynamic_tools", "_call_context", "_log_ctx", "session_id", "workspace"} & set(args))

    def test_crashed_host_envelope_passes_through(self) -> None:
        """执行面报宿主崩溃 → 信封不吞不改写地上行（**壳侧契约**，非树杀验证）。"""
        mcp_def = _definition(
            name="mcp_fetch",
            plugin_id="mcp_demo",
            source="mcp",
            input_schema={"type": "object", "properties": {}},
        )
        rec = _Recorder({
            "success": False,
            "error": "sidecar 进程已死",
            "error_code": "PLUGIN_CRASHED",
        })
        _wire([mcp_def], rec)

        result = _execute({"tool": "mcp_fetch", "arguments": {}})

        assert result.success is True, "壳自身执行完成（失败在目标侧，经信封上行）"
        envelope = result.output
        # 壳侧契约：执行面给什么就上行什么——不改写错误文本、不补内容、不包装成成功
        assert envelope == {
            "success": False,
            "error": "sidecar 进程已死",
            "error_code": "PLUGIN_CRASHED",
        }
        # 注意：error_code 是**桩自带**的，不是壳生成的（内核 ToolExecutionResult 无该
        # 字段、Err 分支会降级为通用 error）。此处断言的是「壳不删不改」，不是「内核必给」。
