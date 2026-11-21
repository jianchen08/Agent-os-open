# @feature: FP-0.2.spill_guard bash 工具面 编码出口孤立代理清洗 | @ci: python-coverage
"""SDK bash 编码模块「出口孤立代理清洗」契约测试。

现状契约：subprocess 输出经 UTF-8+surrogateescape 分支解码后可能残留孤立
代理字符（U+D800-U+DFFF）；孤立代理无法被 JSON 序列化（pydantic
model_dump_json 抛 UnicodeEncodeError），会炸掉 MCP stdio 插件进程（内核侧
表现为 bash 工具 response channel closed）。因此 decode_output_line 出口
必须保证无孤立代理：干净文本直通，脏文本代理字符逐个替换为 U+FFFD，
且不得改变既有分支裁决（编码优先级 / 15% 阈值 / null bytes 去除）。
"""

from __future__ import annotations

import logging
import platform

import pytest

from agentos_plugin_sdk.process_runtime import EncodingHandler

pytestmark = pytest.mark.unit

_ENCODING_LOGGER = "agentos_plugin_sdk.process_runtime.encoding"

# 非 Windows 且系统首选编码为 UTF-8 的平台没有 GBK 解码分支（第三/四优先均不触发），
# 纯 GBK 输入按平台既有行为走 replace 兜底——该结果差异与出口清洗契约无关，跳过。
_GBK_BRANCH_UNAVAILABLE = platform.system() != "Windows" and (
    EncodingHandler.get_system_encoding().lower() in ("utf-8", "utf8")
)


def test_混合污染_代理占比低于阈值_出口无孤立代理且脏字节变替换符(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """主场景：合法 UTF-8 混入少量脏字节（WSL 输出经 Windows 管道污染形态）。

    脏字节经 surrogateescape 分支变为孤立代理且代理占比低于 15% 阈值，
    走原分支返回后必须在出口被清洗。
    """
    raw = b"valid utf8 \xe4\xbd\xa0\xe5\xa5\xbd text \xb0\xfc tail"
    with caplog.at_level(logging.DEBUG, logger=_ENCODING_LOGGER):
        result = EncodingHandler.decode_output_line(raw)
    # 性质：结果可无抛编码为 UTF-8（= JSON/pydantic 可序列化，MCP stdio 炸点根因）
    result.encode("utf-8")
    assert not any("\ud800" <= c <= "\udfff" for c in result)
    # 字面：合法 UTF-8 段逐字保留（含中文），脏字节位置逐字节变 U+FFFD
    assert result == "valid utf8 你好 text \ufffd\ufffd tail"
    # 清洗实际发生时打 debug 日志（替换字符数 + 原字节数），供装机侧采集频率数据
    # （过滤词 sanitized：不与 _try_decode 分支的 surrogateescape 既有日志混淆）
    sanitize_records = [r for r in caplog.records if "sanitized" in r.getMessage()]
    assert len(sanitize_records) == 1
    assert sanitize_records[0].getMessage() == (
        "lone surrogates sanitized: 2 replaced with U+FFFD, input 30 bytes"
    )


@pytest.mark.skipif(
    _GBK_BRANCH_UNAVAILABLE,
    reason="非 Windows 且系统首选编码为 UTF-8 的平台无 GBK 解码分支，纯 GBK 输入走 replace 兜底（平台既有行为）",
)
def test_纯GBK输入仍正确解码为中文() -> None:
    """防回归：出口清洗不得改变既有分支裁决（GBK 输入仍正确解码为中文）。

    Windows 上无论系统首选编码是 cp936（第三优先系统编码分支）还是 UTF-8
    （PYTHONUTF8 模式，第四优先 Windows 代码页分支命中 cp936），纯 GBK
    输入的结果契约一致：「你好」。
    """
    raw = "你好".encode("cp936")
    result = EncodingHandler.decode_output_line(raw)
    assert result == "你好"
    result.encode("utf-8")


def test_纯净utf8_原样透传且不打清洗日志(caplog: pytest.LogCaptureFixture) -> None:
    """干净文本零成本直通：结果逐字不变，且不触发清洗日志。"""
    raw = "hello 你好 world".encode()  # 默认即 utf-8，与性质断言同口径
    with caplog.at_level(logging.DEBUG, logger=_ENCODING_LOGGER):
        result = EncodingHandler.decode_output_line(raw)
    assert result == "hello 你好 world"
    assert not [r for r in caplog.records if "sanitized" in r.getMessage()]


def test_null_bytes去除_既有行为保留() -> None:
    """null bytes 去除与空输入短路的既有行为保持不变。"""
    assert EncodingHandler.decode_output_line(b"abc\x00def") == "abcdef"
    assert EncodingHandler.decode_output_line(b"") == ""
