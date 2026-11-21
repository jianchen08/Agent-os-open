# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""tool 型指标经内核工具执行服务执行的契约测试。

生产路径（server.py on_load 注入 tool_call）：所有 tool 型指标的外部 I/O 都
经内核 ``tool-executor.invoke`` 异步执行——命令类走 bash 工具（进程归 bash
插件 ProcessManager），路径元数据类 file_read 直读优先（读不动回退 bash
[[ ]]，兜底超时 120s），内容类（contains/matches_schema）走 file_read 工具；
评估器宿主只做纯计算，事件循环不因 I/O 等待被冻住。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
if str(_TE_DIR) not in sys.path:
    sys.path.insert(0, str(_TE_DIR))

import _executor  # noqa: E402


def _make_executor(
    monkeypatch: pytest.MonkeyPatch,
    tool_call: Any,
    metrics: dict[str, dict[str, Any]] | None = None,
) -> _executor.PipelineEvaluationExecutor:
    executor = _executor.PipelineEvaluationExecutor(
        chat_send=lambda _payload: None,
        state_rows=lambda: [],
        metrics_config_path="unused.yaml",
        tool_call=tool_call,
    )
    table = metrics or {"bash_check": {"evaluator_type": "tool"}, "file_check": {"evaluator_type": "tool"}, "scope_consistency": {"evaluator_type": "tool"}}
    monkeypatch.setattr(executor, "_load_metrics", lambda: table)
    return executor


def _bash_ok(output: str = "", exit_code: int = 0) -> dict[str, Any]:
    return {"success": True, "data": {"status": "completed", "exit_code": exit_code, "output": output}}


def test_bash_check_uses_kernel_tool_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        return _bash_ok("ok\n")

    executor = _make_executor(monkeypatch, tool_call)
    result = asyncio.run(
        executor._evaluate_metric(
            "task-x", "bash_check", {"command": "true", "workspace": "D:/ws", "timeout_seconds": 30}
        )
    )

    assert result.passed is True
    assert calls == [
        ("bash", {"command": "true", "timeout": 30, "background": False, "working_dir": "D:/ws"})
    ]


def test_bash_check_timeout_capped_and_nonzero_exit_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        return _bash_ok("boom", exit_code=9)

    executor = _make_executor(monkeypatch, tool_call)
    result = asyncio.run(
        executor._evaluate_metric("task-x", "bash_check", {"command": "false", "timeout_seconds": 9999})
    )

    assert result.passed is False
    assert "exit=9" in (result.message or "")
    assert "boom" in (result.message or "")
    assert calls[0][1]["timeout"] == 600, "bash 工具上限 600s，超限必须收敛"


def test_bash_check_running_kills_process_and_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        if tool_name == "bash":
            return {"success": True, "data": {"status": "running", "handle": "h1"}}
        return {"success": True, "data": {"status": "terminated"}}

    executor = _make_executor(monkeypatch, tool_call)
    result = asyncio.run(executor._evaluate_metric("task-x", "bash_check", {"command": "sleep 99"}))

    assert result.passed is False
    assert "超时" in (result.error or "")
    assert calls[-1] == ("kill", {"handle": "h1"}), "超时进程必须经 bash 工具 kill 收尾"


def test_bash_check_tool_executor_failure_is_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        called.append(tool_name)
        return {"success": False, "error": "工具未注册"}

    executor = _make_executor(monkeypatch, tool_call)
    result = asyncio.run(executor._evaluate_metric("task-x", "bash_check", {"command": "true"}))

    assert result.passed is False
    assert "工具未注册" in (result.error or "")
    assert called == ["bash"], "失败信封不得继续调 kill"


def test_content_check_uses_file_read_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        return {"success": True, "data": {"file": "D:/ws/a.py", "size": 12, "content": "hello world"}}

    executor = _make_executor(monkeypatch, tool_call)
    result = asyncio.run(
        executor._evaluate_metric(
            "task-x", "file_check::a.py", {"path": "a.py", "workspace": "D:/ws", "check": "contains", "pattern": "world"}
        )
    )

    assert result.passed is True
    assert calls == [("file_read", {"path": "a.py", "workspace": "D:/ws"})]


def _file_read_ok(payload: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "data": payload}


def test_path_metadata_not_empty_file_read_direct_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """not_empty 文本产物：file_read 直读即定论，零 bash 调用。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        return _file_read_ok({"content": "x" * 20})

    executor = _make_executor(monkeypatch, tool_call)
    result = asyncio.run(
        executor._evaluate_metric(
            "task-x",
            "file_check::report.md",
            {"path": "report.md", "workspace": "D:/ws", "check": "not_empty", "min_size": 10},
        )
    )

    assert result.passed is True
    assert "非空" in (result.message or "")
    assert [name for name, _ in calls] == ["file_read"], "直读成功绝不再落 bash"
    assert calls[0][1] == {"path": "report.md", "workspace": "D:/ws"}


def test_path_metadata_not_empty_bytes_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """not_empty min_size 按 UTF-8 字节数判（与 wc -c 同口径）。"""
    async def tool_call(_tool_name: str, _args: dict[str, Any]) -> Any:
        return _file_read_ok({"content": "汇"})  # UTF-8 3 字节

    executor = _make_executor(monkeypatch, tool_call)
    below = asyncio.run(
        executor._evaluate_metric(
            "task-x", "file_check::r.md", {"path": "r.md", "check": "not_empty", "min_size": 5}
        )
    )
    above = asyncio.run(
        executor._evaluate_metric(
            "task-x", "file_check::r.md", {"path": "r.md", "check": "not_empty", "min_size": 3}
        )
    )
    assert below.passed is False and "为空/过小" in (below.message or "")
    assert above.passed is True


def test_path_metadata_unreadable_falls_back_to_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    """二进制产物（file_read 模态拒绝）回退 bash [[ ]] 判定。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        if tool_name == "file_read":
            return {"success": False, "error": "二进制文件不走文本读取"}
        return _bash_ok("")

    executor = _make_executor(monkeypatch, tool_call)
    result = asyncio.run(
        executor._evaluate_metric(
            "task-x",
            "file_check::a.png",
            {"path": "a.png", "workspace": "D:/ws", "check": "not_empty", "min_size": 10},
        )
    )

    assert result.passed is True
    assert [name for name, _ in calls] == ["file_read", "bash"], "直读失败恰好回退一次 bash"
    assert "[[ -f 'a.png' ]]" in calls[1][1]["command"]
    assert calls[1][1]["working_dir"] == "D:/ws"
    assert calls[1][1]["timeout"] >= 120, "兜底超时 30→120（WSL 高载单次 37-90s）"


def test_path_metadata_exists_and_is_directory_via_file_read(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        return _file_read_ok({"content": "text"})

    executor = _make_executor(monkeypatch, tool_call)
    exists = asyncio.run(
        executor._evaluate_metric("task-x", "file_check::f.txt", {"path": "f.txt", "check": "exists"})
    )
    is_dir = asyncio.run(
        executor._evaluate_metric("task-x", "file_check::f.txt", {"path": "f.txt", "check": "is_directory"})
    )
    assert exists.passed is True and "存在" in (exists.message or "")
    assert is_dir.passed is False and "不是目录" in (is_dir.message or "")
    assert {name for name, _ in calls} == {"file_read"}, "file_read 成功即定论，零 bash"

    calls.clear()

    async def dir_tool(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        return _file_read_ok({"items": ["a", "b"]})

    executor = _make_executor(monkeypatch, dir_tool)
    is_dir_hit = asyncio.run(
        executor._evaluate_metric("task-x", "file_check::d", {"path": "d", "check": "is_directory"})
    )
    assert is_dir_hit.passed is True and "是目录" in (is_dir_hit.message or "")


def test_path_metadata_file_read_exception_falls_back_to_bash(monkeypatch: pytest.MonkeyPatch) -> None:
    """file_read 服务抛异常：回退 bash，不终止。"""
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        if tool_name == "file_read":
            raise RuntimeError("服务不可用")
        return _bash_ok("")

    executor = _make_executor(monkeypatch, tool_call)
    result = asyncio.run(
        executor._evaluate_metric(
            "task-x", "file_check::f.bin", {"path": "f.bin", "check": "exists", "workspace": "D:/ws"}
        )
    )
    assert result.passed is True
    assert [name for name, _ in calls] == ["file_read", "bash"]


def test_path_check_exit_one_is_negative_verdict_not_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tool_call(_tool_name: str, _args: dict[str, Any]) -> Any:
        return _bash_ok("", exit_code=1)

    executor = _make_executor(monkeypatch, tool_call)
    exists = asyncio.run(
        executor._evaluate_metric("task-x", "file_check::missing.txt", {"path": "missing.txt", "check": "exists"})
    )
    is_dir = asyncio.run(
        executor._evaluate_metric("task-x", "file_check::missing.txt", {"path": "missing.txt", "check": "is_directory"})
    )
    not_empty = asyncio.run(
        executor._evaluate_metric(
            "task-x", "file_check::missing.txt", {"path": "missing.txt", "check": "not_empty", "min_size": 1}
        )
    )

    assert exists.passed is False and "不存在" in (exists.message or "")
    assert is_dir.passed is False and "不是目录" in (is_dir.message or "")
    assert not_empty.passed is False and "为空/过小" in (not_empty.message or "")
    assert exists.error is None and is_dir.error is None and not_empty.error is None


def test_content_check_contains_and_matches_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    async def tool_call(_tool_name: str, _args: dict[str, Any]) -> Any:
        return {
            "success": True,
            "data": {"file": "D:/ws/a.yaml", "size": 20, "content": "name: demo\ncount: 3\n"},
        }

    executor = _make_executor(monkeypatch, tool_call)
    contains = asyncio.run(
        executor._evaluate_metric(
            "task-x", "file_check::a.yaml", {"path": "a.yaml", "check": "contains", "pattern": "demo"}
        )
    )
    schema_ok = asyncio.run(
        executor._evaluate_metric(
            "task-x",
            "file_check::a.yaml",
            {
                "path": "a.yaml",
                "check": "matches_schema",
                "schema": {"type": "object", "required": ["name", "count"]},
            },
        )
    )
    schema_bad = asyncio.run(
        executor._evaluate_metric(
            "task-x",
            "file_check::a.yaml",
            {
                "path": "a.yaml",
                "check": "matches_schema",
                "schema": {"type": "object", "required": ["missing_key"]},
            },
        )
    )

    assert contains.passed is True
    assert schema_ok.passed is True
    assert schema_bad.passed is False
    assert "结构不合规" in (schema_bad.message or "")


def test_scope_consistency_uses_bash_git_status(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def tool_call(tool_name: str, args: dict[str, Any]) -> Any:
        calls.append((tool_name, args))
        return _bash_ok(" M src/a.py\n M docs/b.md\n")

    executor = _make_executor(monkeypatch, tool_call)
    outside = asyncio.run(
        executor._evaluate_metric(
            "task-x", "scope_consistency", {"workspace": "D:/ws", "allowed_paths": ["src"]}
        )
    )
    inside = asyncio.run(
        executor._evaluate_metric(
            "task-x", "scope_consistency", {"workspace": "D:/ws", "allowed_paths": ["src", "docs"]}
        )
    )

    assert outside.passed is False
    assert "docs/b.md" in (outside.message or "")
    assert inside.passed is True
    assert calls[0][0] == "bash"
    assert calls[0][1]["command"] == "git status --porcelain -uall"
    assert calls[0][1]["working_dir"] == "D:/ws"


def test_without_tool_call_falls_back_to_local_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _make_executor(monkeypatch, None)
    seen: list[str] = []

    def fake_local(metric_id: str, params: dict[str, Any]) -> _executor.MetricResult:
        seen.append(metric_id)
        return _executor.MetricResult(metric_id=metric_id, passed=True, message="local")

    monkeypatch.setattr(executor, "_evaluate_tool_local", fake_local)
    result = asyncio.run(executor._evaluate_metric("task-x", "bash_check", {"command": "true"}))

    assert result.passed is True
    assert seen == ["bash_check"]
