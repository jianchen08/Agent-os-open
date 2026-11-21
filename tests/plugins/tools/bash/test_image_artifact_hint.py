# @feature: FP-0.2.spill_guard bash 工具面 | @ci: python-coverage
"""图片产物引导测试（2026-11-16）：bash/log 结果的看图提示组装与追加。

背景：端到端验证实证，模型用 bash 截图后只 ls/rm 管产物、从不读图，以文本
正则代行视觉判定产出假绿。本组测试覆盖引导行的组装与追加行为——存在性
验证是核心语义：输出里仅提及但不存在的路径绝不提示。

打桩边界：图片文件全部真实落盘 tmp_path（存在性验证走真实 stat）；
BashTool 经 ``object.__new__`` 裸实例化（_append_image_hint 不触进程管理器）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tool import BashTool, build_image_artifact_hint

pytestmark = pytest.mark.unit

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _make_png(directory: Path, name: str) -> Path:
    img = directory / name
    img.write_bytes(_PNG_MAGIC)
    return img


def test_absolute_existing_path_gets_hint(tmp_path):
    img = _make_png(tmp_path, "shot.png")
    hint = build_image_artifact_hint(f"saved screenshot to {img}", None)
    assert hint is not None
    assert str(img) in hint
    assert "file_read" in hint


def test_relative_path_resolved_against_working_dir(tmp_path):
    _make_png(tmp_path, "out.jpeg")
    hint = build_image_artifact_hint("wrote out.jpeg", str(tmp_path))
    assert hint is not None
    assert "out.jpeg" in hint


def test_relative_path_without_anchor_skipped(tmp_path):
    # 文件确实存在于某目录，但不给 working_dir 锚点 → 不提示（防误挂 CWD）
    _make_png(tmp_path, "lonely.png")
    assert build_image_artifact_hint("wrote lonely.png", None) is None


def test_mentioned_but_missing_path_skipped(tmp_path):
    ghost = tmp_path / "ghost.png"
    assert build_image_artifact_hint(f"would write {ghost}", None) is None


def test_no_image_no_hint():
    assert build_image_artifact_hint("plain stdout\nfoo.txt done", None) is None
    assert build_image_artifact_hint("", None) is None
    assert build_image_artifact_hint(None, None) is None


def test_many_images_listed_capped_with_count(tmp_path):
    for i in range(5):
        _make_png(tmp_path, f"s{i}.png")
    text = " ".join(str(tmp_path / f"s{i}.png") for i in range(5))
    hint = build_image_artifact_hint(text, None)
    assert hint is not None
    assert "等共 5 个" in hint
    # 只列前 3 个路径，其余以计数带过
    assert sum(1 for i in range(5) if f"s{i}.png" in hint) == 3


def test_append_hint_scans_command_and_output(tmp_path):
    img = _make_png(tmp_path, "b.png")
    tool = object.__new__(BashTool)
    data = {"output": "exit 0"}
    tool._append_image_hint(data, f"npx playwright screenshot {img}", None)
    assert "[图片产物提示]" in data["output"]
    assert str(img) in data["output"]


def test_append_hint_idempotent(tmp_path):
    img = _make_png(tmp_path, "a.png")
    tool = object.__new__(BashTool)
    data = {"output": f"done {img}"}
    tool._append_image_hint(data, None, None)
    first = data["output"]
    assert "[图片产物提示]" in first
    tool._append_image_hint(data, None, None)
    assert data["output"] == first


def test_append_hint_skips_non_string_output():
    tool = object.__new__(BashTool)
    data: dict = {"output": None}
    tool._append_image_hint(data, "x", None)
    assert data["output"] is None
