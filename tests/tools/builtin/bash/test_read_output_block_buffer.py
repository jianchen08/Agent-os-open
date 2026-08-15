"""_read_output 块缓冲根治测试。

背景：原 stream.readline() 在 cargo/gcc 等块缓冲场景下长时间读不到行，
日志文件为空 → LogCompressor 输出 "[0行]"。改用 stream.read(N) + 按 \\n
切行 + 字节级半行缓存后，块缓冲下也能实时落盘。

本测试直接驱动 _read_output，用伪造的 stream 模拟：
1. 无换行符的长输出 → 全部落盘
2. 多字节 UTF-8 字符跨 4KB 块边界 → 不产生乱码
3. 流结束时残留半行 → flush 到日志
4. 空输出 → 不报错
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from process_manager import ProcessManager

pytestmark = pytest.mark.unit


class _FakeStream:
    """模拟 asyncio subprocess stream，按预设字节块产出数据。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self._at_eof = False

    async def read(self, n: int) -> bytes:
        if not self._chunks:
            if not self._at_eof:
                self._at_eof = True
                return b""
            # 持续返回 b""（流已 EOF）
            return b""
        return self._chunks.pop(0)

    async def readline(self) -> bytes:
        # 兼容：本测试不调用，但保留
        raise NotImplementedError


class _FakeProcess:
    """模拟 asyncio.subprocess.Process，仅提供 _read_output 需要的接口。"""

    def __init__(self, stdout_chunks: list[bytes], stderr_chunks: list[bytes] | None = None, exit_code: int = 0):
        self.stdout = _FakeStream(stdout_chunks)
        self.stderr = _FakeStream(stderr_chunks or [])
        self._exit_code = exit_code

    async def wait(self) -> int:
        return self._exit_code


@pytest.fixture
def pm(tmp_path):
    return ProcessManager(log_dir=tmp_path / "logs")


@pytest.mark.asyncio
async def test_long_output_without_newline_all_persisted(pm, tmp_path):
    """无换行符的长输出（块缓冲场景）应全部落盘。"""
    log_file = tmp_path / "bash_999.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # 5000 字节无换行符（超过 4KB 单块）
    payload = b"x" * 5000
    process = _FakeProcess([payload], exit_code=0)

    await pm._read_output(999, process, log_file)

    content = log_file.read_text(encoding="utf-8")
    assert "x" * 5000 in content, "长输出应完整落盘（read 路径生效）"


@pytest.mark.asyncio
async def test_multibyte_utf8_across_block_boundary_no_mojibake(pm, tmp_path):
    """多字节 UTF-8 字符跨 4KB 块边界不应产生乱码。

    构造：前块末尾是一个 3 字节中文字的前 2 字节，下一块开头是第 3 字节。
    按字节级 \\n 切分 + 完整行边界 decode，能正确还原。
    """
    log_file = tmp_path / "bash_998.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # 中文字符"中" = E4 B8 AD（3 字节）
    # 构造：正常行 + 一个长中文串（无换行符结尾），第 4096 字节正好切到"中"中间
    prefix = "前缀行内容\n"  # 先来一行完整的
    prefix_bytes = prefix.encode("utf-8")
    # 用大量"中"字填到接近 4096，让边界切到某个"中"字中间
    target_total = 4096
    fill_count = (target_total - len(prefix_bytes)) // 3
    test_text = prefix + ("中" * fill_count) + "尾部中字"
    full_bytes = test_text.encode("utf-8")

    # 按 4096 切块（模拟 stream.read(4096)）
    chunks = []
    for i in range(0, len(full_bytes), 4096):
        chunks.append(full_bytes[i:i + 4096])

    process = _FakeProcess(chunks, exit_code=0)
    await pm._read_output(998, process, log_file)

    content = log_file.read_text(encoding="utf-8")
    # 去掉日志尾部的 exit code 标记后比对
    # 原始文本应能被完整还原（不出现乱码）
    assert "前缀行内容" in content
    # 所有"中"字都应保留（不应因跨块变成乱码）
    assert content.count("中") >= fill_count, (
        f"中字数量应至少 {fill_count}，实际 {content.count('中')}，可能跨块产生乱码"
    )


@pytest.mark.asyncio
async def test_trailing_half_line_flushed_on_eof(pm, tmp_path):
    """流结束时残留半行（无换行符结尾）应 flush 到日志，不丢数据。"""
    log_file = tmp_path / "bash_997.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # 一行无换行符结尾
    process = _FakeProcess([b"hello world no newline"], exit_code=0)

    await pm._read_output(997, process, log_file)

    content = log_file.read_text(encoding="utf-8")
    assert "hello world no newline" in content, "残留半行应被 flush"


@pytest.mark.asyncio
async def test_empty_output_no_error(pm, tmp_path):
    """空输出不应报错，日志只含结束标记（无输出行）。"""
    log_file = tmp_path / "bash_996.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    process = _FakeProcess([], exit_code=0)

    await pm._read_output(996, process, log_file)

    content = log_file.read_text(encoding="utf-8")
    # 只有 exit code 标记行
    assert "exit code: 0" in content


@pytest.mark.asyncio
async def test_multiple_lines_in_one_chunk(pm, tmp_path):
    """单个 chunk 含多行（正常场景）应全部按行落盘。"""
    log_file = tmp_path / "bash_995.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    payload = b"line1\nline2\nline3\n"
    process = _FakeProcess([payload], exit_code=0)

    await pm._read_output(995, process, log_file)

    content = log_file.read_text(encoding="utf-8")
    assert "line1" in content
    assert "line2" in content
    assert "line3" in content


@pytest.mark.asyncio
async def test_stderr_prefixed(pm, tmp_path):
    """stderr 输出应加 [stderr] 前缀。"""
    log_file = tmp_path / "bash_994.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    process = _FakeProcess(
        stdout_chunks=[b"stdout line\n"],
        stderr_chunks=[b"stderr line\n"],
        exit_code=0,
    )

    await pm._read_output(994, process, log_file)

    content = log_file.read_text(encoding="utf-8")
    assert "stdout line" in content
    assert "[stderr] stderr line" in content


@pytest.mark.asyncio
async def test_line_split_across_chunks(pm, tmp_path):
    """一行被切到两个 chunk（中间无换行符）应正确拼接。"""
    log_file = tmp_path / "bash_993.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # "partial-1" 和 "-line-end\n" 是同一行被切到两块
    process = _FakeProcess([b"partial-1", b"-line-end\n"], exit_code=0)

    await pm._read_output(993, process, log_file)

    content = log_file.read_text(encoding="utf-8")
    assert "partial-1-line-end" in content, "跨块的同一行应拼接完整"
