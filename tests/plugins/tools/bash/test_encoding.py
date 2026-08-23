"""bash 工具 encoding 测试——Windows 中文环境下的编码自适应解码/编码。

覆盖 EncodingHandler 全部公开接口：

- get_system_encoding：locale 首选编码透传、异常回退 utf-8；
- decode_output_line / decode_output_text：UTF-8 严格直通、null 字节
  剔除、GBK（CMD 输出）解码、UTF-8+surrogateescape（WSL 经 cmd 污染的
  混合字节）、Windows 代码页轮试、UTF-8+replace 兜底；
- safe_cmd_encode：系统编码可表示时直通、不可表示字符安全替换、未知
  编码名（LookupError）回退原文。

解码路径依赖系统代码页/平台，用 monkeypatch 固定 get_system_encoding
与 platform.system，保证跨平台确定性（不 mock 外部服务）。
"""

from __future__ import annotations

import locale

import pytest

import encoding as encoding_mod
from encoding import EncodingHandler

pytestmark = pytest.mark.unit


class TestGetSystemEncoding:
    def test_returns_locale_preferred_encoding(self) -> None:
        # 透传 locale 首选编码（非空时）
        result = EncodingHandler.get_system_encoding()
        assert result == locale.getpreferredencoding()

    def test_fallback_utf8_on_locale_failure(self, monkeypatch) -> None:
        def _boom():
            raise RuntimeError("locale unavailable")

        monkeypatch.setattr(locale, "getpreferredencoding", _boom)
        assert EncodingHandler.get_system_encoding() == "utf-8"

    def test_fallback_utf8_on_empty_locale(self, monkeypatch) -> None:
        monkeypatch.setattr(locale, "getpreferredencoding", lambda: "")
        assert EncodingHandler.get_system_encoding() == "utf-8"


class TestDecodeOutput:
    def test_empty_bytes_return_empty(self) -> None:
        assert EncodingHandler.decode_output_line(b"") == ""
        assert EncodingHandler.decode_output_text(b"") == ""

    def test_utf8_strict_pass_through(self) -> None:
        data = "hello 你好 world".encode("utf-8")
        assert EncodingHandler.decode_output_line(data) == "hello 你好 world"

    def test_null_bytes_stripped(self) -> None:
        # CMD 管道偶发混入 NUL 字节
        assert EncodingHandler.decode_output_line(b"a\x00b\x00c") == "abc"

    def test_decode_text_matches_line_for_multiline(self) -> None:
        data = "第一行\nsecond line\n".encode("utf-8")
        assert EncodingHandler.decode_output_text(data) == "第一行\nsecond line\n"

    def test_gbk_output_decoded_via_system_encoding(self, monkeypatch) -> None:
        # CMD (cp936) 原生输出：GBK 字节非 UTF-8、surrogate 占比过高被拒 → 系统编码
        monkeypatch.setattr(
            EncodingHandler, "get_system_encoding", staticmethod(lambda: "cp936")
        )
        data = "中文目录结构".encode("gbk")
        assert EncodingHandler.decode_output_line(data) == "中文目录结构"

    def test_mostly_utf8_with_few_bad_bytes_uses_surrogateescape(self) -> None:
        # WSL 输出经 cmd /c 污染：长 UTF-8 文本混入 1 个坏字节（<15%）→ 保留
        valid = "hello world this line is fine " * 3  # 87 chars, all ASCII/UTF-8
        data = valid.encode("utf-8") + b"\xff"
        result = EncodingHandler.decode_output_line(data)
        assert result.startswith(valid)
        assert "\udcff" in result  # 坏字节以 surrogate 保留，可回溯重编码

    def test_windows_codepage_fallback_loop(self, monkeypatch) -> None:
        # 系统编码 utf-8（非 Windows 代码页路径）+ Windows 平台 → 轮试 cp936
        monkeypatch.setattr(
            EncodingHandler, "get_system_encoding", staticmethod(lambda: "utf-8")
        )
        monkeypatch.setattr(encoding_mod.platform, "system", lambda: "Windows")
        data = "中文".encode("gbk")
        assert EncodingHandler.decode_output_line(data) == "中文"

    def test_replace_fallback_when_nothing_works(self, monkeypatch) -> None:
        # 非 Windows + 系统编码 utf-8：GBK 字节严格/转义/系统编码全失败 → replace
        monkeypatch.setattr(
            EncodingHandler, "get_system_encoding", staticmethod(lambda: "utf-8")
        )
        monkeypatch.setattr(encoding_mod.platform, "system", lambda: "Linux")
        result = EncodingHandler.decode_output_line(b"\xd6\xd0\xff")
        assert "\ufffd" in result  # 替换字符兜底，不抛异常

    def test_codepage_loop_skips_failed_codepages(self, monkeypatch) -> None:
        # 3 个连续坏字节（surrogateescape 阈值 max(len*0.15, 3) 判为整体非
        # UTF-8）+ 全部双字节代码页失败、仅 cp1252 可解 → 轮试跳过失败代码页
        # 与 cp950 自身（system encoding），命中 cp1252
        monkeypatch.setattr(
            EncodingHandler, "get_system_encoding", staticmethod(lambda: "cp950")
        )
        monkeypatch.setattr(encoding_mod.platform, "system", lambda: "Windows")
        result = EncodingHandler.decode_output_line(b"\x80\x98\x80")
        assert result == "€˜€"  # cp1252 解码结果


class TestSafeCmdEncode:
    def test_representable_text_unchanged(self, monkeypatch) -> None:
        monkeypatch.setattr(locale, "getpreferredencoding", lambda: "utf-8")
        text = "echo 你好 world"
        assert EncodingHandler.safe_cmd_encode(text) == text

    def test_unrepresentable_chars_replaced_for_gbk(self, monkeypatch) -> None:
        # CMD 代码页 cp936 无法表示 emoji → 替换为 ?，命令骨架保留
        monkeypatch.setattr(locale, "getpreferredencoding", lambda: "cp936")
        result = EncodingHandler.safe_cmd_encode("echo \U0001F600 done")
        assert "\U0001F600" not in result
        assert result.startswith("echo")
        assert result.endswith("done")

    def test_unknown_encoding_name_returns_original(self, monkeypatch) -> None:
        # LookupError（编码名不存在）→ 无法替换，返回原文
        monkeypatch.setattr(locale, "getpreferredencoding", lambda: "cp99999")
        text = "echo 测试"
        assert EncodingHandler.safe_cmd_encode(text) == text
