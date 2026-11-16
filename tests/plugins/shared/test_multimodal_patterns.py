# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""multimodal_patterns 单源共享件测试（B11-L7 正则与扩展名 4 份复制单源化）。

验收三面：
- 行为等价：三正则与重构前各插件私副本字面量逐字节一致（重构零行为变化锚），
  外加检出/回收两插件对同一输入的同判定性质断言；
- 同变（删值实验）：改共享件值（收窄词汇重建）一次注入，两插件行为同变——
  证明消费面确实挂在共享件上而非各持副本；
- 跨端对账：pre 的 ``_TEXT_ATTACHMENT_EXTS`` 与前端 files.ts
  ``TEXT_FILE_EXTENSIONS`` 集合对齐（差集恰为 .svg——后端归图片链路），
  原先"注释宣称对齐"无闸，此测试补闸。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_DIR = _REPO_ROOT / "plugins" / "shared"
_FRONTEND_FILES_TS = _REPO_ROOT / "frontend" / "src" / "services" / "api" / "files.ts"

import multimodal_patterns  # noqa: E402
from multimodal_patterns import (  # noqa: E402
    IMAGE_EXTS,
    build_image_url_pattern,
    build_local_file_pattern,
    build_md_image_pattern,
)


def _load_plugin_module(plugin_id: str, unique_name: str) -> ModuleType:
    """唯一名动态加载插件模块（裸名 ``plugin`` 会兄弟目录串扰，同
    multimodal_preprocessor 既有测试做法）。"""
    sys.modules.pop(unique_name, None)
    path = _SHARED_DIR / plugin_id / "plugin.py"
    spec = importlib.util.spec_from_file_location(unique_name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def preprocessor() -> ModuleType:
    return _load_plugin_module(
        "multimodal_preprocessor", "_mm_preprocessor_patterns_ut"
    )


@pytest.fixture(scope="module")
def postprocessor() -> ModuleType:
    return _load_plugin_module(
        "multimodal_postprocessor", "_mm_postprocessor_patterns_ut"
    )


# ============================================================
# 行为等价：与重构前字面量逐字节一致 + 两插件同判定
# ============================================================


class TestBehaviorEquivalence:
    def test_三正则与重构前私副本字面量一致(self) -> None:
        # 金样 = 单源化前两插件里的原字面量（字节级对照，重构零行为变化锚）
        assert multimodal_patterns.MD_IMAGE_PATTERN.pattern == (
            r"!\[[^\]]*\]\((/uploads/[^\s)]+\.(?:jpg|jpeg|png|gif|webp|svg))\)"
        )
        assert multimodal_patterns.IMAGE_URL_PATTERN.pattern == (
            r"(https?://\S+\.(?:jpg|jpeg|png|gif|webp|svg)(?:\?\S*)?)"
        )
        assert multimodal_patterns.LOCAL_FILE_PATTERN.pattern == (
            r"((?:[A-Za-z]:)?[/\\][\S]+\.(?:jpg|jpeg|png|gif|webp|svg|pdf))"
        )
        for pattern in (
            multimodal_patterns.MD_IMAGE_PATTERN,
            multimodal_patterns.IMAGE_URL_PATTERN,
            multimodal_patterns.LOCAL_FILE_PATTERN,
        ):
            assert pattern.flags & re.IGNORECASE

    @pytest.mark.parametrize("ext", IMAGE_EXTS.split("|"))
    def test_词汇六扩展名全命中(self, ext: str) -> None:
        # 性质断言：IMAGE_EXTS 里的每个扩展名都被三正则接受（大小写不敏感）
        assert build_md_image_pattern(IMAGE_EXTS).search(f"![f](/uploads/a.{ext})")
        assert build_image_url_pattern(IMAGE_EXTS).search(f"http://x/a.{ext}")
        assert build_local_file_pattern(IMAGE_EXTS).search(f"/pics/a.{ext}")
        assert build_image_url_pattern(IMAGE_EXTS).search(f"http://x/a.{ext.upper()}")

    def test_检出与回收对同一URL集同判定(
        self, preprocessor: ModuleType, postprocessor: ModuleType
    ) -> None:
        # 性质：pre 检出的 http 图片引用集合 == post 从同文本回收的集合
        # （「能检出即可回收」同一判定，删任一侧扩展名即破坏此不变量）
        text = (
            "看 https://cdn.example.com/a.png?q=1 和 https://x.io/pic/a.JPEG 大图，"
            "附件 ![doc](/uploads/b.webp) 以及本地 C:\\pics\\c.gif"
        )
        pre_urls = [
            ref
            for ref in preprocessor.MultimodalPreprocessor._extract_image_refs(text)
            if ref.startswith("http")
        ]
        post_urls = postprocessor.MultimodalPostprocessor()._extract_urls(text)
        assert pre_urls == post_urls == [
            "https://cdn.example.com/a.png?q=1",
            "https://x.io/pic/a.JPEG",
        ]


# ============================================================
# 删值实验：改共享件值 → 两插件同变
# ============================================================


class TestDeleteValueExperiment:
    def test_URL正则收窄两插件同变(
        self,
        monkeypatch: pytest.MonkeyPatch,
        preprocessor: ModuleType,
        postprocessor: ModuleType,
    ) -> None:
        # 模拟改共享件值：URL 正则词汇收窄到只剩 png（删值）——一次注入，
        # pre 检出与 post 回收必须同时跟随（各持副本则必有其一不变）
        monkeypatch.setattr(
            multimodal_patterns, "IMAGE_URL_PATTERN", build_image_url_pattern("png")
        )
        jpg_text = "看 https://x.io/a.jpg 好图"
        png_text = "看 https://x.io/b.png 好图"
        assert postprocessor.MultimodalPostprocessor()._extract_urls(jpg_text) == []
        assert preprocessor.MultimodalPreprocessor._extract_image_refs(jpg_text) == []
        assert postprocessor.MultimodalPostprocessor()._extract_urls(png_text) == [
            "https://x.io/b.png"
        ]
        assert preprocessor.MultimodalPreprocessor._extract_image_refs(png_text) == [
            "https://x.io/b.png"
        ]

    def test_词汇收窄pre三分支同变(
        self,
        monkeypatch: pytest.MonkeyPatch,
        preprocessor: ModuleType,
    ) -> None:
        # md 引用/本地路径两分支同挂共享词汇：收窄到 png 后 jpg 引用三路全不命中
        monkeypatch.setattr(
            multimodal_patterns, "MD_IMAGE_PATTERN", build_md_image_pattern("png")
        )
        monkeypatch.setattr(
            multimodal_patterns,
            "LOCAL_FILE_PATTERN",
            build_local_file_pattern("png"),
        )
        monkeypatch.setattr(
            multimodal_patterns, "IMAGE_URL_PATTERN", build_image_url_pattern("png")
        )
        extract = preprocessor.MultimodalPreprocessor._extract_image_refs
        assert extract("![f](/uploads/a.jpg) 与 C:\\pics\\a.jpg") == []
        assert extract("![f](/uploads/b.png) 与 C:\\pics\\b.png") == [
            "/uploads/b.png",
            "C:\\pics\\b.png",
        ]


# ============================================================
# 跨端对账：_TEXT_ATTACHMENT_EXTS 与前端 files.ts
# ============================================================


class TestTextAttachmentExtsReconciliation:
    def test_后端文本附件词汇与前端集合对齐(self, preprocessor: ModuleType) -> None:
        # 对账闸（原注释宣称对齐无闸）：差集恰为 .svg（后端图片链路认领；
        # svg 落图片正则的行为面由 test_词汇六扩展名全命中[svg] 覆盖）
        source = _FRONTEND_FILES_TS.read_text(encoding="utf-8")
        match = re.search(
            r"const TEXT_FILE_EXTENSIONS = new Set\(\[(.*?)\]\)", source, re.DOTALL
        )
        assert match is not None, "前端 files.ts TEXT_FILE_EXTENSIONS 结构漂移"
        frontend_exts = set(re.findall(r"'\.([a-z0-9]+)'", match.group(1)))
        backend_exts = set(preprocessor._TEXT_ATTACHMENT_EXTS.split("|"))
        assert backend_exts <= frontend_exts
        assert frontend_exts - backend_exts == {"svg"}
