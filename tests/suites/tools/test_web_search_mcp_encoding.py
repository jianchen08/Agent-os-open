"""web_search MCP 工具链路编码回归测试。

背景:
    BUG: Windows 中文系统下，bing-search 子进程 stdout 默认 cp936(GBK) 编码，
    主进程 MCPClient._read_response 用默认 UTF-8 解码，遇到 GBK 双字节汉字
    首字节（如 0xCA continuation byte）抛 UnicodeDecodeError，导致整次
    web_search 调用失败，并连锁触发 _fallback_bing_search 也失败。

本测试用 fake 子进程直接喂 GBK 字节流，验证:
1. _read_response 不再因编码崩溃（errors='replace' 兜底）
2. 合法 UTF-8 帧仍能正常解析

注意: 不启动真实子进程，避免依赖网络/bing.com，保证 CI 稳定。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from tools.mcp_client import MCPClient


class _FakeStdout:
    """模拟子进程 stdout，按预设字节行依次返回。

    asyncio.subprocess.stdout.readline() 返回 bytes（含换行符），
    EOF 时返回空 bytes。这里复刻该契约。
    """

    def __init__(self, lines: list[bytes]) -> None:
        self._lines = list(lines)

    async def readline(self) -> bytes:
        if not self._lines:
            return b""
        return self._lines.pop(0)


class _FakeProcess:
    """最小化的子进程替身，仅满足 MCPClient 用到的属性。"""

    def __init__(self, stdout_lines: list[bytes]) -> None:
        self.stdout = _FakeStdout(stdout_lines)
        self.stdin = None
        self.returncode = None


def _make_client(lines: list[bytes]) -> MCPClient:
    proc = _FakeProcess(lines)
    return MCPClient(proc, name="fake-server", use_sync=False)


def test_read_response_tolerates_gbk_bytes() -> None:
    """子进程吐 GBK 编码字节时，_read_response 不应抛 UnicodeDecodeError。

    回归保护: 修复前 `line.decode()` 默认 UTF-8，遇到 0xCA 等非法
    continuation byte 直接崩溃，整个 web_search 调用失败；修复后
    errors='replace' 保证解码不抛异常，调用链不再因此中断。

    注: GBK 字节经 replace 后可能恰好仍是合法 JSON（字符串值变乱码），
    此时该行会被当作首帧返回——重点是【不崩溃】，而非语义正确。
    语义正确由 bing-search 端 reconfigure(utf-8) 保证（治本路径）。
    """
    gbk_line = '{"msg": "标题摘要"}\n'.encode("gbk")

    client = _make_client([gbk_line])

    # 修复前：此处抛 UnicodeDecodeError；修复后：正常返回（值含替换符）
    result = asyncio.run(client._read_response(timeout=2.0))
    assert isinstance(result, dict)
    assert "msg" in result


def test_read_response_tolerates_pure_garbage_bytes() -> None:
    """纯非法字节（非任何编码）也不应崩溃，且后续合法帧能被读到。

    这模拟 bing-search stderr 误混入 stdout 或二进制噪声的场景。
    """
    # 纯非法 UTF-8 字节（0xCA 0xFE 等高位字节，且不是合法 GBK 字符）
    garbage = b"\xca\xfe\x80\xff\n"
    valid_frame = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})
        + "\n"
    ).encode("utf-8")

    client = _make_client([garbage, valid_frame])
    result = asyncio.run(client._read_response(timeout=2.0))

    # garbage 行 json.loads 失败被跳过，最终读到合法帧
    assert result["jsonrpc"] == "2.0"
    assert result["result"]["ok"] is True


def test_read_response_parses_utf8_chinese() -> None:
    """合法 UTF-8 中文帧应无损解析（reconfigure 治本路径的端到端验证）。"""
    frame = (
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"content": [{"type": "text", "text": "标题摘要"}]},
            },
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")

    client = _make_client([frame])
    result = asyncio.run(client._read_response(timeout=2.0))

    assert result["result"]["content"][0]["text"] == "标题摘要"


def test_read_response_skips_blank_and_non_json() -> None:
    """空行和非 JSON 行应被跳过，不影响后续合法帧。"""
    lines = [
        b"   \n",            # 空白行
        b"not a json\n",     # 非 JSON
        b"",                 # 这里若返回空会判定连接关闭，故放最后前给合法帧
    ]
    # 修正：放一个合法帧在空 bytes 之前
    valid = (json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}}) + "\n").encode("utf-8")
    lines = [b"   \n", b"not a json\n", valid]

    client = _make_client(lines)
    result = asyncio.run(client._read_response(timeout=2.0))

    assert result["id"] == 2
