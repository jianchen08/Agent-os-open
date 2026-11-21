# @feature: FP-0.2.〇 TB参赛适配 | @ci: none-local
"""bash 失败信封上下文测试（R9）。

失败信封必须让模型免猜（R9 裁定）：
1. `_build_failure_message` 构造契约：error_msg 首段带实际执行的命令原文；
   metadata 携带 command / stdout / stderr / exit_code 四件——stdout/stderr
   按 reader 的 `[stderr] ` 行前缀从合并捕获里拆分；
2. 超长字段 64KB 截断留痕；
3. `run_command` 端到端（桩 ProcessManager）：非零退出信封 metadata 实际
   携带命令与 stderr/stdout 拆分（两个调用方——run_command 与 log 完成路径
   ——共用同一构造，契约由本测试钉死）。

隔离策略：ProcessManager 是外部依赖（真进程 spawn），桩注入；被测对象
BashTool 本体真实。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent


def _load_tool() -> Any:
    mod_name = "bash_tool_envelope_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_tool()
BashTool = _MOD.BashTool


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestBuildFailureMessage:
    def test_envelope_carries_command_and_split_streams(self) -> None:
        output = "stdout line 1\n[stderr] Traceback (most recent call last)\n[stderr] NameError: done\nstdout line 2\n"
        error_msg, meta = BashTool._build_failure_message(
            exit_code=1,
            output=output,
            summary_obj={},
            command='python3 -c "print(1)\nprint(2)"',
        )
        # error_msg 首段 = 实际执行的命令原文（模型免猜）
        assert "实际执行的命令：" in error_msg
        assert 'python3 -c "print(1)' in error_msg
        assert "退出码: 1" in error_msg
        # metadata 四件
        assert meta["exit_code"] == 1
        assert meta["command"] == 'python3 -c "print(1)\nprint(2)"'
        assert meta["stdout"] == "stdout line 1\nstdout line 2"
        assert meta["stderr"] == "Traceback (most recent call last)\nNameError: done"

    def test_no_command_omits_command_line(self) -> None:
        error_msg, meta = BashTool._build_failure_message(2, "out", {}, command=None)
        assert "实际执行的命令" not in error_msg
        assert meta["command"] == ""
        assert meta["stdout"] == "out"
        assert meta["stderr"] == ""

    def test_long_field_clipped_with_marker(self) -> None:
        big = "x" * (BashTool.FAILURE_CONTEXT_FIELD_LIMIT + 100)
        _msg, meta = BashTool._build_failure_message(1, big, {}, command="c")
        assert len(meta["stdout"]) < BashTool.FAILURE_CONTEXT_FIELD_LIMIT + 80
        assert "截断，原始" in meta["stdout"]
        assert meta["command"] == "c"


class _StubProcessManager:
    """ProcessManager 桩：固定一次完成的非零退出与合并捕获输出。"""

    def __init__(self, output: str) -> None:
        self._output = output
        self.started: dict[str, Any] = {}

    async def start_process(self, **kwargs: Any) -> tuple[int, Path]:
        self.started = kwargs
        log_file = (Path(kwargs.get("log_dir") or ".")) / "bash_1.log"
        return 1, log_file

    def get_process_info(self, pid: int) -> Any:
        import types

        return types.SimpleNamespace(
            pid=pid, status="completed", exit_code=1, start_time=0.0, command=self.started.get("command", "")
        )

    def get_output(self, pid: int) -> str:
        return self._output

    def get_summary(self, pid: int) -> dict[str, Any]:
        return {"exit_code": 1}

    async def wait_output_settled(self, pid: int) -> None:
        return None


class TestRunCommandEnvelope:
    def test_run_command_failure_envelope_fields(self, tmp_path: Path) -> None:
        tool = BashTool()
        tool.process_manager = _StubProcessManager(
            "stdout keep\n[stderr] cat: /nope: No such file\n"
        )
        inputs: dict[str, Any] = {
            "command": 'ls /nope && python3 -c "print(1)"',
            "workspace": str(tmp_path),
        }
        result = _run(tool.run_command(inputs))
        assert result.success is False
        assert result.error_code == "COMMAND_FAILED"
        meta = result.metadata or {}
        assert meta["exit_code"] == 1
        assert meta["command"] == 'ls /nope && python3 -c "print(1)"'
        assert meta["stdout"] == "stdout keep"
        assert meta["stderr"] == "cat: /nope: No such file"
        # error_msg 命令原文在信封 message 里（模型免猜）
        assert "实际执行的命令" in str(result.error or "")
