# @feature: FP-0.2.〇 管道引擎 | @vision: V6 可即用 | @ci: python-coverage
"""llm_core 多模态引用解析单元测试——发送前把本地引用转 base64（ADR 2026-08-21）。

分工：preprocessor 输出引用（/uploads/... 或绝对路径，state/trace 恒小）；
llm_core `_resolve_multimodal_blocks` 在 `_build_messages` 请求装配时读文件转
data URL——二进制只活在本次请求的局部变量里，不落任何持久层。

用例分组：
- /uploads/ 引用：UPLOADS_DIR env 定位 → data URL（mime 按扩展名）；
- 绝对路径引用：存在即解析；
- http(s)/data URL：原样透传（API 直连拉取）；
- 失败降级：文件缺失/超大 → warning + 丢弃该块，不阻断；
- _build_messages 集成：multimodal_content 引用合并进最后一条 user 消息时
  已是 data URL。
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

def _load_plugin_under_test() -> ModuleType:
    """唯一名动态加载 plugin.py（裸名 `import plugin` 会被兄弟插件目录串扰）。"""
    path = (
        Path(__file__).resolve().parents[4]
        / "plugins" / "shared" / "pipeline" / 'core' / 'llm_core' / "plugin.py"
    )
    name = '_llm_core_under_test'
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_under_test()

LLMCore = _mod.LLMCore


def _png_bytes() -> bytes:
    # 1x1 透明 PNG（最小合法图片，mime 判定按扩展名 .png）
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
        "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )


# ── _resolve_image_ref：单条引用 → data URL ─────────────────────


def test_resolve_uploads_ref_via_env_dir(tmp_path, monkeypatch):
    (tmp_path / "cat.png").write_bytes(_png_bytes())
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    url = LLMCore._resolve_image_ref("/uploads/cat.png")  # noqa: SLF001
    assert url.startswith("data:image/png;base64,")
    payload = url.split(",", 1)[1]
    assert base64.b64decode(payload) == _png_bytes()


def test_resolve_absolute_path_ref(tmp_path):
    f = tmp_path / "shot.jpg"
    f.write_bytes(_png_bytes())
    url = LLMCore._resolve_image_ref(str(f))  # noqa: SLF001
    assert url.startswith("data:image/jpeg;base64,")


def test_http_and_data_urls_return_empty_for_local_resolver():
    # 非本地引用：本地解析器不处理（调用方按透传分支保留原块）
    assert LLMCore._resolve_image_ref("https://a.com/x.png") == ""  # noqa: SLF001
    assert LLMCore._resolve_image_ref("data:image/png;base64,xxx") == ""  # noqa: SLF001


def test_resolve_missing_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    assert LLMCore._resolve_image_ref("/uploads/nope.png") == ""  # noqa: SLF001


def test_resolve_oversize_file_returns_empty(tmp_path, monkeypatch):
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * (LLMCore._MAX_IMAGE_BYTES + 1))  # noqa: SLF001
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    assert LLMCore._resolve_image_ref("/uploads/big.png") == ""  # noqa: SLF001


# ── _resolve_multimodal_blocks：块级分派与降级 ────────────────────


def test_blocks_local_ref_resolved_http_passthrough_text_kept(tmp_path, monkeypatch):
    (tmp_path / "cat.png").write_bytes(_png_bytes())
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    blocks = [
        {"type": "image_url", "image_url": {"url": "/uploads/cat.png"}},
        {"type": "image_url", "image_url": {"url": "https://a.com/x.png"}},
        {"type": "text", "text": "说明"},
    ]
    out = LLMCore._resolve_multimodal_blocks(blocks)  # noqa: SLF001
    assert out[0]["image_url"]["url"].startswith("data:image/png;base64,")
    assert out[1]["image_url"]["url"] == "https://a.com/x.png"
    assert out[2] == {"type": "text", "text": "说明"}


def test_blocks_failed_local_ref_dropped(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    blocks = [
        {"type": "image_url", "image_url": {"url": "/uploads/gone.png"}},
        {"type": "image_url", "image_url": {"url": "https://a.com/y.png"}},
    ]
    out = LLMCore._resolve_multimodal_blocks(blocks)  # noqa: SLF001
    assert len(out) == 1
    assert out[0]["image_url"]["url"] == "https://a.com/y.png"


def test_blocks_non_list_input_returns_empty():
    assert LLMCore._resolve_multimodal_blocks(None) == []  # noqa: SLF001
    assert LLMCore._resolve_multimodal_blocks("oops") == []  # noqa: SLF001


# ── _build_messages 集成：引用在合并点已是 data URL ───────────────


def test_build_messages_merges_resolved_data_url(tmp_path, monkeypatch):
    (tmp_path / "cat.png").write_bytes(_png_bytes())
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = LLMCore.__new__(LLMCore)  # 只用 _build_messages，绕开 __init__ 依赖
    state = {
        "system_message": {"role": "system", "content": "sys"},
        "messages": [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "在"},
            {"role": "user", "content": "看图"},
        ],
        "multimodal_content": [
            {"type": "image_url", "image_url": {"url": "/uploads/cat.png"}},
        ],
    }
    msgs = pre._build_messages(state)  # noqa: SLF001
    last_user = [m for m in msgs if m["role"] == "user"][-1]
    assert isinstance(last_user["content"], list)
    types = [p["type"] for p in last_user["content"]]
    assert types[0] == "text"
    assert "image_url" in types
    img_part = next(p for p in last_user["content"] if p["type"] == "image_url")
    assert img_part["image_url"]["url"].startswith("data:image/png;base64,")
