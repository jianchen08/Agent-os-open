# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @ci: python-coverage
"""stop_check 停机词声明面同步契约测试（停机词声明化 ADR 2026-10-15）。

内核 core 静态词表已不持有 task_evaluate_failed——该署名词经本插件
manifest `capabilities.stop_reasons` 声明、由 watcher sync 收束重建进
core 声明注册表参与 Failed 折算。声明缺失 = 署名落 Completed 兜底
（静默语义回退），本测试锁声明面在位。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent


def _declared_stop_reasons() -> set[str]:
    with open(_DIR / "plugin.json", encoding="utf-8") as f:
        manifest = json.load(f)
    return set(manifest["capabilities"]["stop_reasons"])


def test_manifest_declares_evaluate_failed_stop_word() -> None:
    # 一期迁移词（core 静态表已摘除）：映射全赖此声明，缺席即语义回退
    assert "task_evaluate_failed" in _declared_stop_reasons()


def test_declared_stop_words_are_non_empty_strings() -> None:
    # 声明词格式契约：非空字符串（core 重建时空词剔除、空声明不入表）
    declared = _declared_stop_reasons()
    assert declared, "声明面不得为空集（本插件有在产失败署名词）"
    assert all(isinstance(w, str) and w.strip() for w in declared)
