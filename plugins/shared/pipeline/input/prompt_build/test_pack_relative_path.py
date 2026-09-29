# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""prompt_build 包内相对路径形态（{{path:./...}}）行为测试。

`./` 前缀 = 相对该 agent 所属模式包目录解析（state.context.agent_pack_root
由 context_build 经模式包注册表命中时写入）；非 `./` 形态行为不变（系统根
相对、绝对路径原样）。失败统一 fail-soft：warning 留痕（文案注明包内相对
形态）+ 注入空串，不抛异常。
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# 插件目录 + pipeline 包加入 sys.path（与 server.py 自身的 sys.path 注入对齐）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    """动态加载 plugin.py 模块（与 test_prompt_build.py 同范式）。"""
    mod_name = "prompt_build_plugin_packrel_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    assert module_path.exists(), f"plugin.py missing at {module_path}"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load plugin.py"
    assert spec.loader is not None, "Cannot load plugin.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    """同步执行协程（新建事件循环，避免 pytest-asyncio 冲突）。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_ctx(state: dict[str, Any] | None = None) -> PluginContext:
    """构造最小 PluginContext。"""
    return PluginContext(state=dict(state or {}))


@pytest.fixture
def isolated_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """系统根与模式包根都钉到 tmp，返回 (system_root, pack_root)。

    AGENTOS_CONFIG_ROOT 钉死保证旧形态（系统根相对）不落到真实仓根。
    """
    system_root = tmp_path / "sys-root"
    pack_root = tmp_path / "pack" / "mode_x"
    (system_root / "config").mkdir(parents=True)
    pack_root.mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(system_root / "config"))
    return system_root, pack_root


def _pack_file(pack_root: Path, rel: str, text: str) -> Path:
    target = pack_root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


class TestPackRelativePath:
    """{{path:./...}} 包内相对形态：命中注入 / fail-soft / 旧形态不变。"""

    def test_pack_relative_file_hit(self, isolated_roots: tuple[Path, Path]) -> None:
        """① state 带包根 + ./ 文件存在 → 注入内容命中。"""
        _system_root, pack_root = isolated_roots
        _pack_file(pack_root, "materials/rules.md", "包内私有规则")
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx(
            {
                "context.system_prompt": "骨架 {{path:./materials/rules.md}}",
                "context.agent_pack_root": str(pack_root),
            }
        )
        res = _run(plugin.execute(ctx))
        content = res.state_updates["system_message"]["content"]
        assert "骨架" in content
        assert "包内私有规则" in content
        assert "{{path:" not in content, "占位符须被消费，不得原样残留"

    @pytest.mark.parametrize(
        "pack_root_state",
        [{}, {"context.agent_pack_root": ""}],
        ids=["key-absent", "key-empty"],
    )
    def test_pack_relative_without_pack_root_fail_soft(
        self, isolated_roots: tuple[Path, Path], pack_root_state: dict[str, Any], caplog
    ) -> None:
        """② 无包根键/包根为空 + ./ → 注入空串且 execute 正常完成（fail-soft 不抛）。"""
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx(
            {"context.system_prompt": "骨架 {{path:./materials/rules.md}}", **pack_root_state}
        )
        with caplog.at_level(logging.WARNING):
            res = _run(plugin.execute(ctx))  # 不抛即 success
        content = res.state_updates["system_message"]["content"]
        assert content == "骨架 "
        assert any("包内相对形态" in r.getMessage() for r in caplog.records), (
            "失败留痕须注明包内相对形态，与系统根形态的失败可辨"
        )

    def test_pack_relative_missing_file_fail_soft(
        self, isolated_roots: tuple[Path, Path], caplog
    ) -> None:
        """③ 包根存在但文件缺失 → 注入空串，warning 注明包内相对形态。"""
        _system_root, pack_root = isolated_roots
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx(
            {
                "context.system_prompt": "骨架 {{path:./materials/missing.md}}",
                "context.agent_pack_root": str(pack_root),
            }
        )
        with caplog.at_level(logging.WARNING):
            res = _run(plugin.execute(ctx))
        content = res.state_updates["system_message"]["content"]
        assert content == "骨架 ", "其余段落不受影响，缺失占位符注入空串"
        assert any("包内相对形态" in r.getMessage() for r in caplog.records)

    def test_legacy_system_root_form_unchanged(self, isolated_roots: tuple[Path, Path]) -> None:
        """④ {{path:config/...}} 旧形态行为不变：仍按系统根解析，包根在场也不改语义。"""
        system_root, pack_root = isolated_roots
        legacy = system_root / "config" / "rules" / "legacy.md"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text("系统级规则", encoding="utf-8")
        _pack_file(pack_root, "config/rules/legacy.md", "包内同名文件")
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx(
            {
                "context.system_prompt": "{{path:config/rules/legacy.md}}",
                "context.agent_pack_root": str(pack_root),
            }
        )
        res = _run(plugin.execute(ctx))
        content = res.state_updates["system_message"]["content"]
        assert "系统级规则" in content
        assert "包内同名文件" not in content, "旧形态不得误走包根"

    def test_pack_relative_dir_inject(self, isolated_roots: tuple[Path, Path]) -> None:
        """⑤ 目录形态 {{path:./dir}} → 遍历包内目录顶层文件注入。"""
        _system_root, pack_root = isolated_roots
        _pack_file(pack_root, "materials/a.md", "AAA")
        _pack_file(pack_root, "materials/b.md", "BBB")
        mod = _load_plugin_module()
        plugin = mod.PromptBuildPlugin()
        ctx = _make_ctx(
            {
                "context.system_prompt": "{{path:./materials}}",
                "context.agent_pack_root": str(pack_root),
            }
        )
        res = _run(plugin.execute(ctx))
        content = res.state_updates["system_message"]["content"]
        assert '<files dir="./materials">' in content
        assert "AAA" in content
        assert "BBB" in content
