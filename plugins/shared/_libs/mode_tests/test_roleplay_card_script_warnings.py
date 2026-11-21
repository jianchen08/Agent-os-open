# @feature: FP-RP卡带脚本如实降级 | @vision: V1 可进化 | @ci: python-coverage
"""卡带 ST 脚本导入的如实降级标注测试（RP 收口方案批 5 缺口补齐）。

锁定行为（mode_roleplay/card_lint.py::lint_scripts + lint_card_extensions 合流）：
- 脚本执行面未实现（RP 方案 §十.E 语言选型：脚本层改道宿主 JS，STscript 转译
  shim 后置池，触发条件=带脚本 ST 卡转译需求真机出现）——导入侧只做结构透传，
  warnings 必须如实标注「携带脚本、当前不执行」，禁止静默不执行；
- 命中口径：extensions 顶层键与二层键（tavern_helper.scripts 形态）casefold 命中；
- 性质：无脚本声明的卡零误报（兼容态零行为变化）；既有四面 warnings 不被脚本
  面覆盖或吞掉（合流而非替换）；同一脚本键只报一条。

mock 仅外部依赖（无）：纯函数直调，零 IO。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROLEPLAY_DIR = os.path.join(MODES_DIR, "mode_roleplay")

pytestmark = pytest.mark.unit


def _load_card_lint() -> Any:
    """按真实包内名装载 card_lint（与包内裸名 import 面一致）。"""
    shared = MODES_DIR
    if shared not in sys.path:
        sys.path.insert(0, shared)
    roleplay = ROLEPLAY_DIR
    if roleplay not in sys.path:
        sys.path.insert(0, roleplay)
    name = "card_lint"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(ROLEPLAY_DIR, "card_lint.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


card_lint = _load_card_lint()


# ── 命中口径：顶层键 / 二层键 / 别名形态 ────────────────────────────────


@pytest.mark.parametrize(
    ("extensions", "expected_path"),
    [
        # 顶层 scripts（酒馆 JS 扩展导出形态）
        ({"scripts": [{"name": "a"}, {"name": "b"}]}, "extensions.scripts"),
        # 二层 tavern_helper.scripts（tavern_helper 扩展包裹脚本）
        ({"tavern_helper": {"scripts": [{"name": "a"}]}}, "extensions.tavern_helper.scripts"),
        # Quick Replies 别名（下划线/无分隔两形态）
        ({"quick_replies": {"1": {"cmd": "/echo"}}}, "extensions.quick_replies"),
        ({"quickreplies": "cmd"}, "extensions.quickreplies"),
        # STscript 直声明
        ({"stscript": ["/setvar hp 10"]}, "extensions.stscript"),
        # 大小写不敏感（ST 导出键名大小写不统一）
        ({"Scripts": [{"name": "a"}]}, "extensions.Scripts"),
    ],
)
def test_script_keys_hit_with_path(extensions: dict[str, Any], expected_path: str) -> None:
    warnings = card_lint.lint_scripts(extensions)
    assert any(expected_path in w for w in warnings), warnings
    assert all("结构透传" in w for w in warnings), "命中必须如实标注结构透传口径"


def test_script_warning_reports_entry_count_and_deferral() -> None:
    """条目数如实（列表元素数）；后置触发条件随 warning 上行（不假装生效）。"""
    three = card_lint.lint_scripts({"scripts": [{"n": 1}, {"n": 2}, {"n": 3}]})
    one = card_lint.lint_scripts({"scripts": [{"n": 1}]})
    assert "3 条" in three[0]
    assert "1 条" in one[0]
    assert "宿主 JS" in three[0]
    assert "真机出现" in three[0], "后置触发条件必须写进 warning（可追溯）"


# ── 零误报：兼容态 / 非脚本声明 ────────────────────────────────────────


@pytest.mark.parametrize(
    "extensions",
    [
        None,
        {},
        {"variables": {"hp": {"type": "integer"}}},
        {"behaviors": {"暴走": {"trigger": "暴", "effect": {"hp": "+1"}}}},
        # 形近但非脚本承载键
        {"description_scriptless": "无脚本", "regex_scripts": [{"script_name": "x"}]},
    ],
)
def test_non_script_extensions_zero_warnings(extensions: Any) -> None:
    """无脚本声明的卡 = 零脚本警告（regex_scripts 是已实现的文本面，不在此面）。"""
    assert card_lint.lint_scripts(extensions) == []


def test_single_key_reports_once_with_inner_count() -> None:
    """tavern_helper 包裹 scripts = 一条（内层路径 + 内层条目数），不重复报外层。"""
    warnings = card_lint.lint_scripts({"tavern_helper": {"scripts": [{"a": 1}, {"b": 2}]}})
    assert len(warnings) == 1, warnings
    assert "tavern_helper.scripts" in warnings[0]
    assert "2 条" in warnings[0]


# ── 合流：五面汇总不吞既有 warnings ────────────────────────────────────


def test_lint_card_extensions_merges_script_face() -> None:
    """合流面：既有四面 warnings 与脚本面同表上行（性质=互不吞没）。"""
    warnings = card_lint.lint_card_extensions(
        {
            "variables": {"hp": {"type": "not_a_type"}},
            "engine": {"allow_cheat": "yes"},
            "scripts": [{"name": "a"}],
        }
    )
    assert any("variables.hp" in w for w in warnings), warnings
    assert any("engine.allow_cheat" in w for w in warnings), warnings
    assert any("extensions.scripts" in w for w in warnings), warnings


def test_lint_card_extensions_pure_card_has_no_script_warning() -> None:
    """出厂无扩展卡形态（extensions 空/None）零警告——兼容态零行为变化。"""
    assert card_lint.lint_card_extensions({}) == []
    assert card_lint.lint_card_extensions(None) == []
