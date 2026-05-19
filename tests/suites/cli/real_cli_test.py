"""真实 CLI 测试 — 接入 MiniMax M2.7 进行端到端验证。

使用真实 LLMCore（MiniMax M2.7）替代 Mock DemoLLMCore，
通过完整管道引擎验证以下场景：
1. 简单问答
2. 流式输出
3. 工具调用
4. 多轮对话
5. 错误处理

运行方式：
    $env:PYTHONPATH="src"; python real_cli_test.py

安全注意：
    - API Key 是真实密钥，测试脚本仅在本机运行
    - 测试完成后不要将结果文件提交到公开仓库
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pipeline.engine import PipelineEngine
from pipeline.plugin import (
    IOutputPlugin,
    OutputResult,
    PluginContext,
)
from pipeline.registry import PluginRegistry
from pipeline.route import (
    InputRouteEntry,
    InputRouteTable,
    OutputRouteEntry,
    OutputRouteTable,
)
from pipeline.types import ErrorPolicy, RouteSignal, StateKeys
from plugins.core.llm_core import LLMCore
from plugins.core.tool_core import ToolCore

# ---------------------------------------------------------------------------
# MiniMax M2.7 配置
# ---------------------------------------------------------------------------

MINIMAX_CONFIG: dict[str, Any] = {
    "provider": "minimax",
    "model_name": "MiniMax-M2.7",
    # 中国大陆用户使用 minimaxi.com，国际用户使用 minimax.io
    "api_base": "https://api.minimaxi.com/v1",
    "api_key": "[REDACTED]",
    "default_params": {
        "temperature": 0.7,
        "max_tokens": 4096,  # M2.7 使用 reasoning tokens，需要足够大
    },
}

# 错误 API Key 配置（用于错误处理测试）
BAD_KEY_CONFIG: dict[str, Any] = {
    **MINIMAX_CONFIG,
    "api_key": "sk-invalid-key-000000000000000000000000000000000000",
    "max_retries": 0,  # 不重试，快速失败
}


# ---------------------------------------------------------------------------
# 自定义 Output 插件
# ---------------------------------------------------------------------------


class StopOnEndPlugin(IOutputPlugin):
    """检测 should_stop 或 raw_error 时返回 END 信号。

    当管道需要终止时（should_stop=True 或 raw_error 非空），发出 end 路由信号。

    Attributes:
        error_policy: 错误策略为 SKIP
    """

    error_policy = ErrorPolicy.SKIP

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "stop_on_end"

    @property
    def priority(self) -> int:
        """插件执行优先级，数值越小越先执行。"""
        return 1

    @property
    def route_signals(self) -> list[str]:
        """关注所有 core_type。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """检测停止条件。

        Args:
            ctx: 插件执行上下文

        Returns:
            当 should_stop 为 True 时返回 END 信号
        """
        if ctx.state.get("should_stop"):
            return OutputResult(
                route_signal=RouteSignal(route_type="end", reason="user requested stop"),
            )
        return OutputResult()


class DefaultEndRoute(IOutputPlugin):
    """默认路由输出插件 — 单轮结束后返回 END 信号。

    每轮 LLM 调用后直接结束管道循环。适用于单轮问答场景。

    Attributes:
        error_policy: 错误策略为 SKIP
    """

    error_policy = ErrorPolicy.SKIP

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "default_end_route"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 99

    @property
    def route_signals(self) -> list[str]:
        """关注所有 core_type。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:
        """执行默认路由。

        Args:
            ctx: 插件执行上下文

        Returns:
            END 路由信号，表示单轮处理完成
        """
        return OutputResult(
            route_signal=RouteSignal(
                route_type="end",
                reason="single turn complete",
            ),
        )
def build_simple_pipeline(
    llm_config: dict[str, Any] | None = None,
) -> PipelineEngine:
    """构建简单单轮问答管道。

    Args:
        llm_config: LLM 配置字典，默认使用 MiniMax M2.7

    Returns:
        配置好的 PipelineEngine 实例
    """
    config = llm_config or MINIMAX_CONFIG
    registry = PluginRegistry()

    # 注册核心插件
    llm_core = LLMCore(config=config)
    registry.register_core("llm_call", llm_core)

    # 注册输出插件
    registry.register(StopOnEndPlugin())
    registry.register(DefaultEndRoute())

    # 配置路由表
    input_route = InputRouteTable([
        InputRouteEntry(
            name="stop",
            condition="should_stop == True",
            target="end",
            plugins=[],
            priority=1,
        ),
        InputRouteEntry(
            name="default",
            condition="True",
            target="core",
            plugins=[],
            priority=10,
        ),
    ])

    output_route = OutputRouteTable([
        OutputRouteEntry(
            route_type="end",
            condition="should_stop == True",
            priority=1,
        ),
        OutputRouteEntry(
            route_type="end",
            condition="True",
            priority=99,
        ),
    ])

    return PipelineEngine(
        input_route_table=input_route,
        output_route_table=output_route,
        plugin_registry=registry,
    )



# ---------------------------------------------------------------------------
# 测试场景
# ---------------------------------------------------------------------------

# 测试结果收集
test_results: list[dict[str, Any]] = []


async def test_simple_qa(console: Console) -> None:
    """测试场景1：简单问答。

    向 MiniMax M2.7 发送 "你好，请用一句话介绍你自己"，
    验证 raw_result 非空且无 raw_error。

    Args:
        console: Rich 控制台实例
    """
    console.rule("[bold cyan]测试1: 简单问答[/bold cyan]")
    test_name = "简单问答"

    try:
        engine = build_simple_pipeline()
        initial_state = {
            StateKeys.CORE_TYPE: "llm_call",
            "user_input": "你好，请用一句话介绍你自己",
        }

        start = time.monotonic()
        final_state = await engine.run(initial_state)
        elapsed = time.monotonic() - start

        raw_result = final_state.get(StateKeys.RAW_RESULT)
        raw_error = final_state.get(StateKeys.RAW_ERROR)
        iteration = final_state.get(StateKeys.ITERATION, 0)

        # 验证
        passed = raw_result is not None and raw_error is None

        # 输出
        result_preview = (raw_result or "")[:200]
        console.print(f"  迭代次数: {iteration}")
        console.print(f"  耗时: {elapsed:.2f}s")
        console.print(f"  raw_error: {raw_error}")
        console.print(f"  结果预览: {result_preview}")

        test_results.append({
            "name": test_name,
            "passed": passed,
            "elapsed": elapsed,
            "raw_result_preview": result_preview,
            "raw_error": raw_error,
            "detail": f"raw_result={'非空' if raw_result else '空'}, raw_error={raw_error}",
        })

    except Exception as exc:
        console.print(f"  [red]异常: {exc}[/red]")
        test_results.append({
            "name": test_name,
            "passed": False,
            "elapsed": 0,
            "raw_result_preview": "",
            "raw_error": str(exc),
            "detail": f"异常: {exc}",
        })


async def test_streaming(console: Console) -> None:
    """测试场景2：流式输出。

    向 MiniMax M2.7 发送 "请写一首关于春天的短诗"，
    设置 streaming=True 并通过 on_chunk 回调收集流式 chunk，
    验证 chunk 正确接收。

    Args:
        console: Rich 控制台实例
    """
    console.rule("[bold cyan]测试2: 流式输出[/bold cyan]")
    test_name = "流式输出"

    try:
        engine = build_simple_pipeline()
        chunks: list[dict[str, Any]] = []

        def on_chunk(chunk: dict[str, Any]) -> None:
            """流式 chunk 回调。

            Args:
                chunk: 流式数据块，包含 type 和 content
            """
            chunks.append(chunk)

        initial_state = {
            StateKeys.CORE_TYPE: "llm_call",
            "user_input": "请写一首关于春天的短诗",
            "streaming": True,
            "on_chunk": on_chunk,
        }

        start = time.monotonic()
        final_state = await engine.run(initial_state)
        elapsed = time.monotonic() - start

        raw_result = final_state.get(StateKeys.RAW_RESULT)
        raw_error = final_state.get(StateKeys.RAW_ERROR)
        text_chunks = [c for c in chunks if c.get("type") == "text"]

        # 验证：至少收到 1 个 text chunk
        passed = len(text_chunks) > 0 and raw_error is None

        # 输出
        result_preview = (raw_result or "")[:200]
        console.print(f"  收到 chunk 总数: {len(chunks)}")
        console.print(f"  文本 chunk 数: {len(text_chunks)}")
        console.print(f"  耗时: {elapsed:.2f}s")
        console.print(f"  raw_error: {raw_error}")
        console.print(f"  结果预览: {result_preview}")

        test_results.append({
            "name": test_name,
            "passed": passed,
            "elapsed": elapsed,
            "raw_result_preview": result_preview,
            "raw_error": raw_error,
            "detail": f"chunks={len(chunks)}, text_chunks={len(text_chunks)}",
        })

    except Exception as exc:
        console.print(f"  [red]异常: {exc}[/red]")
        test_results.append({
            "name": test_name,
            "passed": False,
            "elapsed": 0,
            "raw_result_preview": "",
            "raw_error": str(exc),
            "detail": f"异常: {exc}",
        })


async def test_tool_call(console: Console) -> None:
    """测试场景3：工具调用。

    分两个阶段验证：
    阶段A：向 MiniMax M2.7 发送包含工具声明的请求，
           验证 LLM 是否发起 tool_calls（raw_tool_calls 非空）
    阶段B：使用 ToolCore 直接执行工具调用，验证工具执行成功

    注意：LLMCore 目前不自动从 state 读取 tools 参数，
    因此需要创建包含 tools 的专用 LLMCore 实例。

    Args:
        console: Rich 控制台实例
    """
    console.rule("[bold cyan]测试3: 工具调用[/bold cyan]")
    test_name = "工具调用"

    try:
        # ---- 阶段A：验证 LLM 生成 tool_calls ----
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "calculator",
                    "description": "执行加减乘除运算",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "a": {"type": "number", "description": "第一个数"},
                            "b": {"type": "number", "description": "第二个数"},
                            "operation": {
                                "type": "string",
                                "enum": ["+", "-", "*", "/"],
                                "description": "运算符",
                            },
                        },
                        "required": ["a", "b", "operation"],
                    },
                },
            }
        ]

        # 创建带 tools 的 LLMCore
        tool_llm_config = {
            **MINIMAX_CONFIG,
            "default_params": {
                **MINIMAX_CONFIG["default_params"],
                "tools": tools,
            },
        }

        registry = PluginRegistry()
        llm_core = LLMCore(config=tool_llm_config)
        registry.register_core("llm_call", llm_core)

        registry.register(StopOnEndPlugin())
        registry.register(DefaultEndRoute())

        input_route = InputRouteTable([
            InputRouteEntry(
                name="stop",
                condition="should_stop == True",
                target="end",
                plugins=[],
                priority=1,
            ),
            InputRouteEntry(
                name="default",
                condition="True",
                target="core",
                plugins=[],
                priority=10,
            ),
        ])

        # 阶段A：只要 LLM 返回 tool_calls 就结束管道
        output_route = OutputRouteTable([
            OutputRouteEntry(
                route_type="end",
                condition="should_stop == True",
                priority=1,
            ),
            OutputRouteEntry(
                route_type="end",
                condition="True",
                priority=99,
            ),
        ])

        engine = PipelineEngine(
            input_route_table=input_route,
            output_route_table=output_route,
            plugin_registry=registry,
        )

        initial_state = {
            StateKeys.CORE_TYPE: "llm_call",
            "messages": [
                {
                    "role": "user",
                    "content": "请帮我计算 123 加 456 等于多少？请使用 calculator 工具。",
                }
            ],
        }

        start = time.monotonic()
        final_state = await engine.run(initial_state)
        elapsed_a = time.monotonic() - start

        raw_tool_calls = final_state.get(StateKeys.RAW_TOOL_CALLS, [])
        raw_result = final_state.get(StateKeys.RAW_RESULT)
        raw_error = final_state.get(StateKeys.RAW_ERROR)
        iteration = final_state.get(StateKeys.ITERATION, 0)

        console.print(f"  [bold]阶段A: LLM 生成 tool_calls[/bold]")
        console.print(f"    迭代次数: {iteration}")
        console.print(f"    耗时: {elapsed_a:.2f}s")
        console.print(f"    raw_tool_calls: {json.dumps(raw_tool_calls, ensure_ascii=False)[:200]}")
        console.print(f"    raw_result: {(raw_result or '')[:100]}")
        console.print(f"    raw_error: {raw_error}")

        phase_a_passed = len(raw_tool_calls) > 0

        # ---- 阶段B：验证 ToolCore 执行工具 ----
        console.print(f"  [bold]阶段B: ToolCore 执行工具[/bold]")
        tool_core = ToolCore(config={"timeout": 30.0})

        def calculator(args: dict[str, Any] | str) -> str:
            """简单计算器工具，执行加减乘除运算。

            Args:
                args: 工具调用参数，可能是 dict 或 JSON 字符串

            Returns:
                计算结果字符串
            """
            import operator

            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    return f"参数解析失败: {args}"

            ops = {
                "+": operator.add,
                "-": operator.sub,
                "*": operator.mul,
                "/": operator.truediv,
            }
            a = args.get("a", 0)
            b = args.get("b", 0)
            op = args.get("operation", "+")
            if op in ops:
                try:
                    result = ops[op](float(a), float(b))
                    return f"{a} {op} {b} = {result}"
                except (ValueError, ZeroDivisionError) as e:
                    return f"计算错误: {e}"
            return f"不支持的操作: {op}"

        tool_core.register_tool("calculator", calculator)

        # 使用 LLM 返回的 tool_calls 构造 ToolCore 执行上下文
        tool_ctx = PluginContext(state={
            StateKeys.RAW_TOOL_CALLS: raw_tool_calls,
            StateKeys.RAW_RESULT: None,
            StateKeys.RAW_ERROR: None,
        }, config={})

        tool_result = await tool_core.execute(tool_ctx)
        tool_results = tool_result.get(StateKeys.TOOL_RESULTS, [])
        tool_success = any(r.get("success") for r in tool_results) if tool_results else False

        console.print(f"    工具执行结果: {json.dumps(tool_results, ensure_ascii=False, indent=2)[:300]}")

        phase_b_passed = tool_success

        # 总体验证
        passed = phase_a_passed and phase_b_passed
        elapsed = elapsed_a

        console.print(f"  阶段A(LLM生成tool_calls): {'PASS' if phase_a_passed else 'FAIL'}")
        console.print(f"  阶段B(ToolCore执行工具): {'PASS' if phase_b_passed else 'FAIL'}")

        test_results.append({
            "name": test_name,
            "passed": passed,
            "elapsed": elapsed,
            "raw_result_preview": (raw_result or "")[:200],
            "raw_error": raw_error,
            "detail": f"phase_a={phase_a_passed}(tool_calls={len(raw_tool_calls)}), phase_b={phase_b_passed}(tool_success={tool_success})",
        })

    except Exception as exc:
        console.print(f"  [red]异常: {exc}[/red]")
        test_results.append({
            "name": test_name,
            "passed": False,
            "elapsed": 0,
            "raw_result_preview": "",
            "raw_error": str(exc),
            "detail": f"异常: {exc}",
        })


async def test_multi_turn(console: Console) -> None:
    """测试场景4：多轮对话。

    使用同一 session_id 连续调用管道两次，
    第二次携带第一次的对话历史，验证上下文保持和迭代正确。

    Args:
        console: Rich 控制台实例
    """
    console.rule("[bold cyan]测试4: 多轮对话[/bold cyan]")
    test_name = "多轮对话"

    try:
        session_id = "test-session-001"
        messages: list[dict[str, Any]] = []

        # --- 第1轮 ---
        engine1 = build_simple_pipeline()
        user_input_1 = "请记住这个数字：42"
        messages.append({"role": "user", "content": user_input_1})

        initial_state_1 = {
            StateKeys.CORE_TYPE: "llm_call",
            "user_input": user_input_1,
            StateKeys.SESSION_ID: session_id,
            "messages": list(messages),
        }

        start = time.monotonic()
        final_state_1 = await engine1.run(initial_state_1)
        elapsed_1 = time.monotonic() - start

        raw_result_1 = final_state_1.get(StateKeys.RAW_RESULT)
        raw_error_1 = final_state_1.get(StateKeys.RAW_ERROR)

        console.print(f"  [bold]第1轮[/bold]")
        console.print(f"    用户: {user_input_1}")
        console.print(f"    回复: {(raw_result_1 or '')[:150]}")
        console.print(f"    耗时: {elapsed_1:.2f}s")

        # 记录助手回复到 messages
        if raw_result_1:
            messages.append({"role": "assistant", "content": raw_result_1})

        # --- 第2轮 ---
        engine2 = build_simple_pipeline()
        user_input_2 = "我刚才让你记住的数字是多少？"
        messages.append({"role": "user", "content": user_input_2})

        initial_state_2 = {
            StateKeys.CORE_TYPE: "llm_call",
            "user_input": user_input_2,
            StateKeys.SESSION_ID: session_id,
            "messages": list(messages),
        }

        start = time.monotonic()
        final_state_2 = await engine2.run(initial_state_2)
        elapsed_2 = time.monotonic() - start

        raw_result_2 = final_state_2.get(StateKeys.RAW_RESULT)
        raw_error_2 = final_state_2.get(StateKeys.RAW_ERROR)

        console.print(f"  [bold]第2轮[/bold]")
        console.print(f"    用户: {user_input_2}")
        console.print(f"    回复: {(raw_result_2 or '')[:150]}")
        console.print(f"    耗时: {elapsed_2:.2f}s")

        # 验证：两轮都成功，且第2轮回复中包含 "42"
        both_ok = raw_result_1 is not None and raw_error_1 is None and raw_result_2 is not None and raw_error_2 is None
        context_ok = "42" in (raw_result_2 or "")
        passed = both_ok and context_ok

        console.print(f"  两轮均成功: {both_ok}")
        console.print(f"  上下文保持(含42): {context_ok}")

        test_results.append({
            "name": test_name,
            "passed": passed,
            "elapsed": elapsed_1 + elapsed_2,
            "raw_result_preview": f"R1: {(raw_result_1 or '')[:100]} | R2: {(raw_result_2 or '')[:100]}",
            "raw_error": raw_error_1 or raw_error_2,
            "detail": f"both_ok={both_ok}, context_ok={context_ok}",
        })

    except Exception as exc:
        console.print(f"  [red]异常: {exc}[/red]")
        test_results.append({
            "name": test_name,
            "passed": False,
            "elapsed": 0,
            "raw_result_preview": "",
            "raw_error": str(exc),
            "detail": f"异常: {exc}",
        })


async def test_error_handling(console: Console) -> None:
    """测试场景5：错误处理。

    使用错误的 API Key 调用 MiniMax，
    验证 raw_error 非空且不抛异常（Core 尽力而为原则）。

    Args:
        console: Rich 控制台实例
    """
    console.rule("[bold cyan]测试5: 错误处理（错误 API Key）[/bold cyan]")
    test_name = "错误处理"

    try:
        engine = build_simple_pipeline(llm_config=BAD_KEY_CONFIG)
        initial_state = {
            StateKeys.CORE_TYPE: "llm_call",
            "user_input": "这条消息不应该成功",
        }

        start = time.monotonic()
        final_state = await engine.run(initial_state)
        elapsed = time.monotonic() - start

        raw_result = final_state.get(StateKeys.RAW_RESULT)
        raw_error = final_state.get(StateKeys.RAW_ERROR)

        # 验证：raw_error 非空，raw_result 为 None，且没有抛异常
        passed = raw_error is not None and raw_result is None

        # 输出
        error_preview = (raw_error or "")[:200]
        console.print(f"  耗时: {elapsed:.2f}s")
        console.print(f"  raw_result: {raw_result}")
        console.print(f"  raw_error: {error_preview}")

        test_results.append({
            "name": test_name,
            "passed": passed,
            "elapsed": elapsed,
            "raw_result_preview": "",
            "raw_error": error_preview,
            "detail": f"raw_error={'非空' if raw_error else '空'}, raw_result={raw_result}",
        })

    except Exception as exc:
        # 不应该抛异常！Core 尽力而为原则
        console.print(f"  [red]意外异常（不应抛异常）: {exc}[/red]")
        test_results.append({
            "name": test_name,
            "passed": False,
            "elapsed": 0,
            "raw_result_preview": "",
            "raw_error": str(exc),
            "detail": f"意外异常（违反Core尽力而为）: {exc}",
        })


# ---------------------------------------------------------------------------
# 结果汇总
# ---------------------------------------------------------------------------


def print_summary(console: Console) -> None:
    """打印测试结果汇总表。

    Args:
        console: Rich 控制台实例
    """
    console.rule("[bold magenta]测试结果汇总[/bold magenta]")

    table = Table(title="MiniMax M2.7 端到端测试结果")
    table.add_column("序号", style="cyan", width=5)
    table.add_column("测试场景", style="white", width=15)
    table.add_column("状态", width=8)
    table.add_column("耗时", style="dim", width=8)
    table.add_column("详情", style="dim", width=40)
    table.add_column("API 返回样本", style="green", width=40)

    for i, r in enumerate(test_results, 1):
        status = "[green]OK[/green]" if r["passed"] else "[red]FAIL[/red]"
        elapsed_str = f"{r['elapsed']:.2f}s"
        detail = r.get("detail", "")
        preview = r.get("raw_result_preview") or r.get("raw_error", "")
        # 截断过长内容
        if len(preview) > 60:
            preview = preview[:57] + "..."
        table.add_row(str(i), r["name"], status, elapsed_str, detail, preview)

    console.print(table)

    # 统计
    total = len(test_results)
    passed = sum(1 for r in test_results if r["passed"])
    rate = (passed / total * 100) if total > 0 else 0

    console.print()
    console.print(
        Panel(
            f"通过: [green]{passed}[/green]/{total}  |  通过率: [bold]{rate:.0f}%[/bold]",
            title="最终结果",
            border_style="green" if rate == 100 else "red",
        )
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    """运行所有测试场景并输出汇总。"""
    import sys
    import io

    # Windows 控制台 UTF-8 输出
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

    console = Console()

    console.print(Panel(
        "[bold]Agent OS — MiniMax M2.7 真实 CLI 测试[/bold]\n"
        "模型: minimax/MiniMax-M2.7\n"
        "端点: https://api.minimaxi.com/v1",
        title="端到端验证",
        border_style="blue",
    ))

    # 依次执行 5 个测试场景
    await test_simple_qa(console)
    await test_streaming(console)
    await test_tool_call(console)
    await test_multi_turn(console)
    await test_error_handling(console)

    # 汇总
    print_summary(console)


if __name__ == "__main__":
    asyncio.run(main())
