"""file_write 工具行尾（CRLF/LF）回归测试。

背景：
agent 在 Windows 上用 file_write 写脚本（.sh），若 open() 不带 newline 参数，
Python 文本模式会把 \\n 翻译成 \\r\\n（CRLF）。该脚本之后在 Linux/容器里被
/bin/sh 解释时，shebang 或 `set -e` 行变成 `set\\r`，报：
    /bin/sh: set: Illegal option -
真实事故记录见 docker/agentos Rust 安装踩坑复盘（Q4）。

修复：_atomic_write / _append_to_file 的 open 必须带 newline="\\n"，
强制 LF 落盘，不依赖平台默认行为。host_provider 的文件写入同理。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.builtin.file_write.tool import FileWriteTool


@pytest.fixture
def tool(tmp_path: Path) -> FileWriteTool:
    """实例化 FileWriteTool，base_path 指向临时目录。"""
    return FileWriteTool(base_path=str(tmp_path))


class TestAtomicWriteLineEndings:
    """_atomic_write 行尾测试（所有内容写入的唯一入口）。"""

    def test_write_preserves_lf_on_windows(self, tool: FileWriteTool, tmp_path: Path):
        """Windows 上写入含 \\n 的内容，落盘必须是 LF，不能是 CRLF。

        这是 Q4 事故的核心：修复前 _atomic_write 用 open(fd, "w") 不带 newline，
        Windows 上 \\n → \\r\\n，脚本喂给 /bin/sh 就报 Illegal option -。
        """
        target = tmp_path / "install.sh"
        content = "#!/bin/sh\nset -e\necho hello\n"
        tool._atomic_write(target, content)

        # 二进制读回，检查行尾
        raw = target.read_bytes()
        assert b"\r\n" not in raw, (
            f"文件含 CRLF 行尾（Windows 文本模式翻译了 \\n），"
            f"该脚本在 Linux/容器里会报 '/bin/sh: set: Illegal option -'。"
            f"实际字节: {raw!r}"
        )
        # 确认确实是 LF（不是被吃掉了换行）
        assert b"\n" in raw, "文件应包含 LF 换行"
        # 内容正确
        assert target.read_text(encoding="utf-8") == content

    def test_write_multiline_script_stays_lf(self, tool: FileWriteTool, tmp_path: Path):
        """多行脚本（模拟 rust 安装脚本）落盘必须全 LF。"""
        target = tmp_path / "setup.sh"
        content = (
            "#!/bin/sh\n"
            "set -e\n"
            "wget -q -O /tmp/x.sh https://example.com/install\n"
            "sh /tmp/x.sh -y\n"
            "rustup component add rustfmt clippy\n"
            "echo done\n"
        )
        tool._atomic_write(target, content)

        raw = target.read_bytes()
        assert b"\r\n" not in raw, f"多行脚本含 CRLF：{raw!r}"
        assert raw.count(b"\n") == 6

    def test_write_content_without_trailing_newline(self, tool: FileWriteTool, tmp_path: Path):
        """无尾换行的内容也不应被注入 CR。"""
        target = tmp_path / "no_newline.txt"
        tool._atomic_write(target, "line1\nline2")
        raw = target.read_bytes()
        assert b"\r\n" not in raw
        assert raw == b"line1\nline2"

    def test_write_existing_crlf_content_is_not_double_converted(self, tool: FileWriteTool, tmp_path: Path):
        """若传入的 content 本身含 \\r\\n（上游已是 CRLF），落盘应保持原样不被二次破坏。

        注意：本测试只验证 _atomic_write 不主动把 \\n 翻译成 \\r\\n。
        content 里原本就有的 \\r\\n 属于上游问题，_atomic_write 不负责清洗
        （那是 file_write 更上层或 .gitattributes 的职责）。
        """
        target = tmp_path / "mixed.txt"
        # content 里有明确的 \r\n 和纯 \n 混合
        content = "crlf-line\r\nlf-line\n"
        tool._atomic_write(target, content)
        raw = target.read_bytes()
        # 原有的 \r\n 保留，纯 \n 不应被额外翻译成 \r\n
        assert raw == b"crlf-line\r\nlf-line\n", f"纯 \\n 被错误翻译：{raw!r}"
