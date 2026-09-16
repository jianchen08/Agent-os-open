# @feature: FP-0.2.〇 管道引擎 | @ci: python-coverage
"""prompt_build 残余缺口分支补测（coverage.xml 缺口行靶单）。

覆盖（经公开面驱动：execute / _build_system_content / 变量解析）：

1. ``_build_system_content`` 约束注入：``context.constraints_text`` 存在时
   在 system 内容尾部拼 ``## 约束`` 块（context_build 渲染的注入式通道）。
2. ``_system_root`` 探针全失（无 AGENTOS_CONFIG_ROOT 且文件树无
   config/+plugins/ 兄弟目录）→ None（调用方按无系统根降级）。
3. ``_resolve_target_path``：_system_root 为 None → None（不拼出半路径）。
4. ``_resolve_single_var_content`` 未知 var_type → 空串（不注入未识别条目）。

不可达/环境依赖残留（逐条说明）：
- ``_system_root`` 的 AGENTOS_CONFIG_ROOT 分支与非 env 祖先探针分支在车道
  内均已覆盖（既有测试）；本文件只补"全失"返回 None 一支。
- ``server.py`` 的 group_root 入 path 行（``if _paths.group_root not in
  sys.path``）由本目录既有 ``test_workflow_and_server.py`` 的 server 装载覆盖
  （同目录单跑实测该行命中）；本文件不重复造"摘除组根后按路径装载 server"的
  用例——那会在同进程共跑时与其它插件目录的同名裸模块 ``plugin`` 抢注冲突，
  属测试装配脆弱面而非被测契约。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_THIS_DIR = Path(__file__).resolve().parent
_SHARED_DIR = Path(__file__).resolve().parents[3]  # plugins/shared/
for _d in (str(_SHARED_DIR), str(_THIS_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from pipeline.plugin import PluginContext  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "prompt_build_plugin_residual_gaps", str(_THIS_DIR / "plugin.py")
)
assert _spec is not None and _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["prompt_build_plugin_residual_gaps"] = _mod
_spec.loader.exec_module(_mod)
PromptBuildPlugin: Any = _mod.PromptBuildPlugin


def _plugin(config: dict[str, Any] | None = None) -> Any:
    return PromptBuildPlugin(config=config or {})


def _ctx(state: dict[str, Any] | None = None) -> Any:
    return PluginContext(state=dict(state or {}), _services={})


# ═══════════════ 约束注入 ═══════════════


class TestConstraintsInjection:
    async def test_constraints_text_appended_to_system_content(self) -> None:
        """context.constraints_text 非空 → system 尾部出现 ## 约束 块（注入式通道）。"""
        p = _plugin({"include_static_vars": False, "include_language_instruction": False})
        ctx = _ctx({
            "context.constraints_text": "不得修改 config/ 下的文件",
            "context.system_prompt": "你是助手",
        })

        content = await p._build_system_content(ctx)

        assert "## 约束" in content
        assert "不得修改 config/ 下的文件" in content
        assert content.index("## 约束") > content.index("你是助手"), "约束拼在尾部"

    @pytest.mark.parametrize(
        "constraints",
        [None, "", "   ", "x"],
    )
    async def test_constraints_block_only_when_truthy(self, constraints: Any) -> None:
        """约束块按真值判定：空串/缺失不产生空标题段（避免噪声提示词）。

        注意：``"   "`` 是真值 → 产生块（现状契约，空白未 trim）。
        """
        p = _plugin({"include_static_vars": False, "include_language_instruction": False})
        ctx = _ctx({"context.constraints_text": constraints, "context.system_prompt": "S"})

        content = await p._build_system_content(ctx)

        expected = bool(constraints)
        assert ("## 约束" in content) is expected
        if expected:
            assert str(constraints) in content


# ═══════════════ 系统根探针 ═══════════════


class TestSystemRootProbe:
    def test_all_probes_miss_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """env 无效 + 文件树无 config/+plugins/ 兄弟目录 → None（无系统根）。"""
        p = _plugin()
        deploy = tmp_path / "deploy"
        deploy.mkdir()
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "elsewhere" / "config"))
        monkeypatch.setattr(_mod, "__file__", str(deploy / "plugin.py"))

        assert p._system_root() is None

    def test_config_root_env_used_when_dir_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """env 指向真实 config/ 目录 → 返回其父目录（项目根，部署布局无关）。"""
        p = _plugin()
        cfg = tmp_path / "proj" / "config"
        cfg.mkdir(parents=True)
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg))

        assert p._system_root() == tmp_path / "proj"

    def test_env_pointing_at_missing_dir_falls_through_to_ancestors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """env 指向不存在目录 → 回落祖先探针（本仓布局命中仓库根）。"""
        p = _plugin()
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "ghost"))

        root = p._system_root()

        assert root is not None
        assert (root / "config").is_dir() and (root / "plugins").is_dir()


class TestResolveTargetPath:
    def test_no_system_root_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """系统根不可得 → 相对路径返回 None（不拼出半截路径）。"""
        p = _plugin()
        monkeypatch.setattr(p, "_system_root", lambda: None)

        assert p._resolve_target_path(_ctx(), "docs/notes.md") is None

    @pytest.mark.parametrize("rel_path", ["", "   ", "\t\n"])
    async def test_blank_path_returns_none(self, rel_path: str) -> None:
        """空白路径 → None（配置写空串不产生目录级误读）。"""
        p = _plugin()

        assert p._resolve_target_path(_ctx(), rel_path) is None

    async def test_absolute_path_used_verbatim(self, tmp_path: Path) -> None:
        """绝对路径原样返回（不受系统根影响）。"""
        p = _plugin()
        target = tmp_path / "abs" / "file.md"
        target.parent.mkdir(parents=True)
        target.write_text("x", encoding="utf-8")

        assert p._resolve_target_path(_ctx(), str(target)) == target

    async def test_relative_path_joined_to_system_root(self, tmp_path: Path) -> None:
        """相对路径以系统根拼接（配置声明的 config/... 不随 CWD 漂移）。"""
        p = _plugin(
            config={"system_root": None},
        )
        root = tmp_path / "repo"
        (root / "docs").mkdir(parents=True)
        (root / "docs" / "note.md").write_text("n", encoding="utf-8")
        p._system_root = lambda: root  # type: ignore[method-assign]

        resolved = p._resolve_target_path(_ctx(), "docs/note.md")

        assert resolved == root / "docs" / "note.md"


# ═══════════════ 动态变量条目分派尾部 ═══════════════


class TestDynamicVarDispatch:
    """经公开面 ``_resolve_dynamic_var``（dynamic_vars 装载路径）驱动各类型分支。

    该函数是"声明条目 → 一行注入文本"的唯一定点，覆盖尾部 return ""（未知类型）
    与各已识别类型的取值/缺值对称形态。
    """

    def _resolve(self, p: Any, item: Any, **extra: Any) -> str:
        from datetime import datetime

        ctx_state: dict[str, Any] = {"session_id": "s1", "llm_model": "m-1", **extra}
        import time as _time

        return asyncio.run(p._resolve_dynamic_var(
            _ctx(ctx_state), item, datetime(2026, 9, 14, 12, 0, 0), "(UTC+8, Asia/Shanghai)",
            "s1", "灵汐",
        ))

    def test_unknown_type_yields_empty_line(self) -> None:
        """未识别 var_type → 空串（不注入猜语义的行）。"""
        assert self._resolve(_plugin(), {"type": "no-such-type", "name": "X"}) == ""

    @pytest.mark.parametrize(
        ("item", "expect"),
        [
            ({"type": "model", "name": "模型"}, "m-1"),
            ({"type": "session", "name": "会话"}, "s1"),
            ({"type": "agent", "name": "Agent"}, "灵汐"),
            ({"type": "content", "name": "内容", "content": "正文"}, "正文"),
            ({"type": "inline", "name": "内联", "content": "片段"}, "片段"),
            ({"type": "reference", "name": "引用", "content": "参考内容"}, "参考内容"),
        ],
    )
    def test_known_types_render_expected_value(self, item: dict[str, Any], expect: str) -> None:
        """已识别类型各行渲染形态：``- <name>: <value>``。"""
        line = self._resolve(_plugin(), item)

        assert line == f"- {item['name']}: {expect}"

    @pytest.mark.parametrize(
        "item",
        [
            {"type": "reference", "name": "引用"},       # 无 content
            {"type": "content", "name": "内容"},
            {"type": "inline", "name": "内联"},
            {"type": "", "name": "空类型"},               # "" 属 reference 族
        ],
    )
    def test_contentless_types_not_injected(self, item: dict[str, Any]) -> None:
        """内容族缺内容体 → 不注入（避免空值噪声行）。"""
        assert self._resolve(_plugin(), item) == ""

    def test_placeholder_without_name_returns_empty(self) -> None:
        """placeholder 类型缺 name → 空串（无占位符文本可解析）。"""
        assert self._resolve(_plugin(), {"type": "placeholder"}) == ""

    def test_disabled_item_not_injected(self) -> None:
        """enabled=False 条目 → 空串（配置开关可关单条注入）。"""
        assert self._resolve(
            _plugin(), {"type": "session", "name": "会话", "enabled": False}
        ) == ""

    @pytest.mark.parametrize("item", [[1, 2], 42, None, True])
    def test_non_dict_non_str_item_returns_empty(self, item: Any) -> None:
        """条目既非字符串也非 dict → 空串（配置写坏不崩、不注入）。"""
        assert self._resolve(_plugin(), item) == ""

    def test_string_item_delegates_to_placeholder_resolution(self) -> None:
        """字符串条目 → 占位符语法路径，取值经 state 的 context.session_id。"""
        line = self._resolve(
            _plugin(), "{{session}}", **{"context.session_id": "sess-from-state"}
        )

        assert line == "sess-from-state", "字符串条目不是配置语法，走 {{...}} 解析"

    def test_string_item_unknown_placeholder_yields_empty(self) -> None:
        """未识别占位符 → 空串（留 warning，不静默拼原文）。"""
        assert self._resolve(_plugin(), "{{no_such_placeholder}}") == ""

    def test_timestamp_uses_configured_format(self) -> None:
        """timestamp 类型按 format 格式化（缺省含日期与时间）。"""
        line = self._resolve(_plugin(), {"type": "timestamp", "name": "时间"})
        custom = self._resolve(
            _plugin(), {"type": "timestamp", "name": "时间", "format": "%Y"}
        )

        assert line.startswith("- 时间: 2026-09-14")
        assert custom == "- 时间: 2026 (UTC+8, Asia/Shanghai)"
