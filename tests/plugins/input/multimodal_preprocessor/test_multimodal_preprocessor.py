# @feature: FP-0.2.〇 管道引擎 | @vision: V6 可即用 | @ci: python-coverage
"""multimodal_preprocessor 单元测试——markdown 附件引用检测与引用化输出（ADR 2026-08-21）。

覆盖决策链：前端把附件以 markdown 引用并入消息 content（内核零改动、只持有
索引）→ preprocessor 检测引用输出**引用块**（不转 base64，state/trace 恒小）
→ llm_core 发送前解析成 base64（另见 tests/plugins/core/llm_core/）。

用例分组：
- markdown 图片引用（![f](/uploads/x.png)）：整 token 消费（剩余文本无残渣）、
  引用块 url 保持原样、同一引用不与裸路径正则重复建块；
- http 图片 URL：既有行为不变；
- 本地路径：文件存在 → 引用块（不再是裸路径直传 API 的坏形态……注：引用
  语义下 url 仍是路径，由 llm_core 读文件转 data URL）；文件缺失 → 文本占位；
- 上传目录解析：经 uploads_path 统一（UPLOADS_DIR env > data/{tenant}/uploads），
  修复前默认 ./data/uploads 与落盘目录不一致导致"文件不存在"误判。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

def _load_plugin_under_test() -> ModuleType:
    """唯一名动态加载 plugin.py（裸名 `import plugin` 会被兄弟插件目录串扰）。"""
    path = (
        Path(__file__).resolve().parents[4]
        / "plugins" / "shared" / "pipeline" / 'input' / 'multimodal_preprocessor' / "plugin.py"
    )
    name = '_mm_preprocessor_under_test'
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_mod = _load_plugin_under_test()

MultimodalPreprocessor = _mod.MultimodalPreprocessor


def _make_ctx(user_input: str, attachments: list | None = None) -> SimpleNamespace:
    """构造最小 PluginContext 形状（execute 只读 state）。"""
    state: dict = {"user_input": user_input}
    if attachments is not None:
        state["attachments"] = attachments
    return SimpleNamespace(state=state)


def _detect(pre: MultimodalPreprocessor, text: str) -> list[dict]:
    # 单测直访私有法（检测器是被测单元）
    return pre._detect_multimodal(text)  # noqa: SLF001


# ── markdown 图片引用（ADR 2026-08-21 主路径）──────────────────────


def test_md_image_ref_produces_ref_block_and_clean_text():
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "看看这张 ![cat.png](/uploads/cat.png) 怎么样")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    # 引用原样（不转 base64——发送前由 llm_core 解析）
    assert img_blocks[0]["image_url"]["url"] == "/uploads/cat.png"
    # 剩余文本不含 markdown 残渣（整 token 消费）
    text_blocks = [b for b in blocks if b.get("type") == "text"]
    joined = "".join(t["text"] for t in text_blocks)
    assert "cat.png" not in joined
    assert "![" not in joined
    assert "看看这张" in joined


def test_md_image_ref_not_duplicated_by_bare_path_pattern():
    # ![f](/uploads/x.png) 里的 /uploads/x.png 同时匹配 _LOCAL_FILE_PATTERN——
    # span 重叠守卫必须阻止重复建块
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "![图](/uploads/a.png)")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1


def test_md_image_ref_requires_uploads_prefix():
    # 只认平台管理的 /uploads/ 引用；用户手打的任意 markdown 图片不劫持
    # （http 图片仍由 _IMAGE_URL_PATTERN 兜底）
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "![x](https://evil.com/a.png) 详见链接")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["image_url"]["url"] == "https://evil.com/a.png"


def test_md_file_link_not_matched_as_image():
    # 非图片语法 [report.pdf](/uploads/r.pdf)：md 图片正则不命中；pdf 路径
    # 由 _LOCAL_FILE_PATTERN 处理（存在性判定 → 文本占位/引用块）
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "见 [report.pdf](/uploads/r.pdf)")
    assert all(b.get("type") != "image_url" or "pdf" not in b["image_url"]["url"] for b in blocks)


# ── http 图片 URL（既有行为回归）──────────────────────────────────


def test_http_image_url_unchanged():
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "图 https://example.com/pic.jpg 完")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert img_blocks[0]["image_url"]["url"] == "https://example.com/pic.jpg"


# ── 本地路径：引用化 + 目录对齐 ──────────────────────────────────


def test_uploads_ref_resolves_via_env_dir(tmp_path, monkeypatch):
    # UPLOADS_DIR 环境变量覆盖 → 存在性判定命中 → 输出引用块
    (tmp_path / "cat.png").write_bytes(b"png")
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "看 /uploads/cat.png")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    assert img_blocks[0]["image_url"]["url"] == "/uploads/cat.png"


def test_uploads_ref_missing_file_falls_back_to_text(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOADS_DIR", str(tmp_path))
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, "看 /uploads/nope.png")
    placeholders = [b for b in blocks if b.get("type") == "text" and "文件不存在" in b["text"]]
    assert placeholders, f"缺文件应产生文本占位块，实际 {blocks}"


def test_absolute_path_existing_file_emits_ref(tmp_path):
    f = tmp_path / "shot.png"
    f.write_bytes(b"png")
    pre = MultimodalPreprocessor()
    blocks = _detect(pre, f"截图在 {f} 请分析")
    img_blocks = [b for b in blocks if b.get("type") == "image_url"]
    assert len(img_blocks) == 1
    # 引用原样（llm_core 发送前读文件转 base64 data URL）
    assert img_blocks[0]["image_url"]["url"] == str(f)


def test_absolute_path_missing_file_text_placeholder():
    pre = MultimodalPreprocessor()
    missing = str(Path(sys.executable).parent / "definitely_missing_xyz.png")
    blocks = _detect(pre, f"看 {missing}")
    placeholders = [b for b in blocks if b.get("type") == "text" and "文件不存在" in b["text"]]
    assert placeholders, f"缺文件应产生文本占位块，实际 {blocks}"


# ── execute 状态输出 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_writes_ref_blocks_to_state():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("看 ![c](/uploads/c.png)"))
    assert result.state_updates["has_multimodal"] is True
    mm = result.state_updates["multimodal_content"]
    assert isinstance(mm, list)
    assert len(mm) > 0, "引用块应写入 multimodal_content"
    # 引用块不含 base64（state/trace 恒小）
    assert "base64" not in str(mm)


@pytest.mark.asyncio
async def test_execute_no_multimodal_returns_empty():
    pre = MultimodalPreprocessor()
    result = await pre.execute(_make_ctx("纯文本消息"))
    assert result.state_updates == {}
