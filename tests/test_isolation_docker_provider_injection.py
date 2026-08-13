# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-plugins-test
"""docker_provider shell 注入安全测试（F-ISO-2）。

为什么重要（意图）：
- `_file_op_in_container` 的 exists 分支把 path 直接 f-string 进
  `sh -c "test -e '{path}' ..."`，`_write_container_file` 把 path 直接插进
  `python3 -c "open('{path}','w')..."`——path 经 MCP 工具来自 LLM 输出，属
  不可信输入。含 `'` 的 path 可提前闭合引号，注入 `;`/`$()` 在容器内执行任意
  命令。影响范围虽限容器内，但仍是越权执行面，必须消除「path 参与源码/脚本
  插值」这一注入通道。
- 断言方式（行为而非实现）：对生成的命令做 shell 词法切分（shlex.split），
  注入载荷不得以独立 token 出现——出现即意味着 shell 会把它们当作独立命令/
  参数执行；写入路径则断言 path 只以独立 argv 元素出现、绝不嵌进更大的字符串。

覆盖：
- exists 分支：含 `'`+`;` 载荷、含 `'`+`$()` 载荷 → 命令中无独立注入 token；
- write 分支：path 含 `'`/`;`/`$()`/空格 → 绝不嵌入更大的字符串参数；
- 正常路径的 exists/write 行为不受影响（回归护栏）。
"""

from __future__ import annotations

import tests._isolation_path  # noqa: F401  # isort: skip —— 须在 providers import 前注入 sys.path

import json
import shlex
from unittest.mock import AsyncMock

import pytest
from isolation_types import ExecutionResult
from providers.docker_provider import DockerProvider


def _make_provider() -> DockerProvider:
    """构造不依赖真实 docker daemon 的提供者实例。"""
    return DockerProvider({"workspace_mount": False})


def _exec_result(stdout: str = "yes\n") -> ExecutionResult:
    """构造 exists 分支所需的 _exec_in_container 返回值。"""
    return ExecutionResult(success=True, output={"stdout": stdout, "stderr": "", "return_code": 0})


class TestFileOpExistsInjection:
    """exists 分支：path 参与 sh -c 脚本插值，必须被安全引用。"""

    @pytest.mark.parametrize(
        "path",
        [
            "foo'; touch /tmp/pwned; echo '",
            "foo'$(id); #",
            "a b 'c'; rm -rf /tmp/x #",
        ],
        ids=["quote_semicolon", "quote_command_substitution", "spaces_quote_rm"],
    )
    @pytest.mark.asyncio
    async def test_exists_payload_not_injected_as_tokens(self, path: str) -> None:
        """恶意 path → 命令经 shell 词法切分后不出现独立注入 token。

        意图：注入载荷若以独立 token（; / touch / $(id) 等）出现在命令里，
        sh -c 执行时就会被当成真实命令/命令替换执行——token 缺失即注入失败。
        """
        provider = _make_provider()
        provider._exec_in_container = AsyncMock(return_value=_exec_result())  # type: ignore[method-assign]

        result = await provider._file_op_in_container(
            "cid", {"operation": "exists", "path": path}
        )

        assert result.success is True
        assert result.output == {"exists": True}
        command = provider._exec_in_container.await_args.args[1]["command"]
        tokens = shlex.split(command)
        # 载荷片段不得以独立 token 出现（shlex 词法切分后若出现即会被 shell 执行）
        assert "touch" not in tokens
        assert "rm" not in tokens
        assert "/tmp/pwned" not in tokens
        assert "/tmp/x" not in tokens
        assert ";" not in tokens
        assert "$(id)" not in tokens
        assert "#" not in tokens

    @pytest.mark.asyncio
    async def test_exists_normal_path_still_works(self) -> None:
        """正常相对路径：命令仍含 test -e 且 exists 解析正确（回归护栏）。"""
        provider = _make_provider()
        provider._exec_in_container = AsyncMock(return_value=_exec_result("no\n"))  # type: ignore[method-assign]

        result = await provider._file_op_in_container(
            "cid", {"operation": "exists", "path": "docs/a.txt"}
        )

        assert result.success is True
        assert result.output == {"exists": False}
        command = provider._exec_in_container.await_args.args[1]["command"]
        assert "test -e" in command
        assert "docs/a.txt" in command


class TestWriteContainerFileInjection:
    """write 分支：path 参与 python3 -c 源码插值，必须改为参数化传递。"""

    @pytest.mark.parametrize(
        "path",
        [
            "foo' ; rm -rf /tmp/x #",
            "foo'$(id).txt",
            "sub/dir'x ; touch /tmp/pwned",
            "a b 'c'",
            "x\"y",
        ],
        ids=["quote_rm", "quote_substitution", "slash_quote_touch", "spaces_quote", "double_quote"],
    )
    @pytest.mark.asyncio
    async def test_write_path_never_embedded_in_larger_arg(self, path: str) -> None:
        """恶意 path → 所有 _run_cmd 参数中，path 只以独立 argv 元素出现。

        意图：path 一旦被 f-string 拼进 `open('{path}','w')` 这类源码字符串，
        单引号提前闭合即 Python 语法注入（字符串逃逸 + 任意表达式）。path 只能
        作为独立 argv 传给容器内程序（python3 的 sys.argv），程序按数据对待。
        """
        provider = _make_provider()
        provider._run_cmd = AsyncMock(return_value=(0, b"", b""))  # type: ignore[method-assign]

        result = await provider._file_op_in_container(
            "cid", {"operation": "write", "path": path, "content": "hello"}
        )

        assert result.success is True
        assert provider._run_cmd.await_count >= 2  # mkdir + 写入
        for call in provider._run_cmd.await_args_list:
            args = call.args[0]
            for arg in args:
                if isinstance(arg, str) and path in arg:
                    # path 只允许以“整体等于它自己”的独立 argv 元素出现
                    assert arg == path, f"path 被嵌入更大的字符串参数: {arg!r}"

    @pytest.mark.asyncio
    async def test_write_normal_path_still_works(self) -> None:
        """正常路径：写入仍执行（mkdir + 参数化写），内容经 json 往返（回归护栏）。"""
        provider = _make_provider()
        calls: list[list[str]] = []
        async def fake_run(args: list[str], **kwargs: object) -> tuple[int, bytes, bytes]:
            calls.append(args)
            return 0, b"", b""

        provider._run_cmd = fake_run  # type: ignore[method-assign]

        result = await provider._file_op_in_container(
            "cid", {"operation": "write", "path": "docs/a.txt", "content": 'he"llo\n'}
        )

        assert result.success is True
        assert any("mkdir" in c and "docs" in c for c in calls)
        write_call = calls[-1]
        assert write_call[0] == "docker"
        assert write_call[-2] == "docs/a.txt"  # path 作为独立 argv 元素
        # 内容以 json 编码作为独立 argv 传给容器内脚本，脚本按 argv 解码（无插值）
        assert json.loads(write_call[-1]) == 'he"llo\n'
