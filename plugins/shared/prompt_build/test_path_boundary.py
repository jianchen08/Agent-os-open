# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: python-coverage
"""prompt_build 路径边界（`{{path:...}}`）fail-closed 行为测试（P1-1 安全修复）。

修复前形态：`{{path:../../secret}}` 直接拼在系统根后交给 `is_file()` 读，
`..` 可越出系统根 / 模式包根，任意文件内容被注入 system prompt。
修复后契约（唯一解析点 = `_resolve_target`）：

- 允许根：`./` 形态 = 模式包根；其余相对路径 = 系统项目根；
  绝对路径 = 系统项目根或模式包根内（含根本身）；
- 越界（`..` 越根 / 根外绝对路径 / 包内 `../` 越根）→ **拒收**：
  `path=None`、`reason="escape"`、ERROR 留痕，注入内容为空串，
  且**不得**降级成"文件不存在"的 warning（那会把安全检查误导成配置写错）；
- 合法路径照常注入（正对照，证明不是"一律拒"）。

行为断言走公开面（`execute` 的 system_message 内容）与解析点（`_resolve_target`
的 path/reason），不断言内部调用次数。
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

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
_SHARED_DIR = str(_PLUGIN_DIR.parents[0])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "prompt_build_plugin_pathboundary_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _ctx(state: dict[str, Any] | None = None) -> PluginContext:
    return PluginContext(state=dict(state or {}))


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    """系统根 / 模式包根 / 根外目录三者互相隔离。

    返回 (system_root, pack_root, outside)，并把 AGENTOS_CONFIG_ROOT 钉到
    system_root/config，保证解析基准不落到真实仓根。
    """
    system_root = tmp_path / "sys-root"
    (system_root / "config").mkdir(parents=True)
    pack_root = tmp_path / "pack" / "mode_x"
    pack_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(system_root / "config"))
    return system_root, pack_root, outside


def _execute(plugin: Any, ctx: PluginContext) -> str:
    res = _run(plugin.execute(ctx))
    return res.state_updates["system_message"]["content"]


# ═══════════════ 正对照：合法路径照常注入 ═══════════════


class TestLegitPathPasses:
    """合法相对/根内绝对路径必须照常注入（证明边界不是"一律拒"）。"""

    def test_relative_path_inside_root_injected(self, roots: tuple[Path, Path, Path]) -> None:
        """① 合法相对路径：`config/rules/x.md`（系统根内）→ 内容注入。"""
        system_root, _pack, _outside = roots
        _write(system_root / "config" / "rules" / "x.md", "系统级规则正文")
        plugin = _load_plugin_module().PromptBuildPlugin()
        ctx = _ctx({"context.system_prompt": "骨架 {{path:config/rules/x.md}}"})

        content = _execute(plugin, ctx)

        assert "系统级规则正文" in content
        assert "{{path:" not in content

    def test_absolute_path_inside_root_injected(self, roots: tuple[Path, Path, Path]) -> None:
        """② 根内绝对路径 → 同样注入（绝对形态不因边界校验被误杀）。"""
        system_root, _pack, _outside = roots
        target = _write(system_root / "config" / "abs.md", "根内绝对路径正文")
        plugin = _load_plugin_module().PromptBuildPlugin()
        ctx = _ctx({"context.system_prompt": f"骨架 {{{{path:{target}}}}}"})

        content = _execute(plugin, ctx)

        assert "根内绝对路径正文" in content

    def test_pack_relative_inside_pack_injected(self, roots: tuple[Path, Path, Path]) -> None:
        """③ 包内相对路径（`./`）落在包根内 → 注入。"""
        _system, pack_root, _outside = roots
        _write(pack_root / "materials" / "m.md", "包内物料")
        plugin = _load_plugin_module().PromptBuildPlugin()
        ctx = _ctx(
            {
                "context.system_prompt": "骨架 {{path:./materials/m.md}}",
                "context.agent_pack_root": str(pack_root),
            }
        )

        content = _execute(plugin, ctx)

        assert "包内物料" in content


# ═══════════════ 负样本：越界必须拒收且留痕 ═══════════════


class TestEscapeRejected:
    """越界形态逐一必须拒收（修复前这些都会读到根外文件）。"""

    def test_dotdot_escape_relative_rejected(
        self, roots: tuple[Path, Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """① `{{path:../secret.md}}` 越出系统根 → 内容不注入 + ERROR 留痕。"""
        _system, _pack, outside = roots
        _write(outside / "secret.md", "SECRET-OUTSIDE-ROOT")
        plugin = _load_plugin_module().PromptBuildPlugin()
        # 系统根 = sys-root；`../outside/secret.md` 指向 tmp/outside/secret.md
        ctx = _ctx({"context.system_prompt": "骨架 {{path:../outside/secret.md}}"})

        with caplog.at_level(logging.WARNING):
            content = _execute(plugin, ctx)

        assert "SECRET-OUTSIDE-ROOT" not in content, "越界文件内容绝不得进入提示词"
        assert content == "骨架 ", "其余段落不受影响（拒绝注入而非中断构建）"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("路径越界" in r.getMessage() for r in errors), "越界须 ERROR 明确留痕"
        assert not any("不存在" in r.getMessage() for r in caplog.records), (
            "越界不得降级报成'文件不存在'（会把安全检查误导成配置写错）"
        )

    def test_dotdot_deep_escape_rejected(self, roots: tuple[Path, Path, Path]) -> None:
        """② 多级 `..`（`../../`）同样拒收——不靠单层路径比较。"""
        _system, _pack, outside = roots
        _write(outside / "deep.md", "DEEP-SECRET")
        plugin = _load_plugin_module().PromptBuildPlugin()
        ctx = _ctx({"context.system_prompt": "{{path:../../outside/deep.md}}"})

        content = _execute(plugin, ctx)

        assert "DEEP-SECRET" not in content
        assert content == ""

    def test_absolute_path_outside_roots_rejected(self, roots: tuple[Path, Path, Path]) -> None:
        """③ 根外绝对路径 → 拒收（绝对路径不再"原样使用"）。"""
        _system, _pack, outside = roots
        target = _write(outside / "abs-secret.md", "ABS-SECRET")
        plugin = _load_plugin_module().PromptBuildPlugin()
        ctx = _ctx({"context.system_prompt": f"{{{{path:{target}}}}}"})

        content = _execute(plugin, ctx)

        assert "ABS-SECRET" not in content
        assert content == ""

    def test_pack_relative_dotdot_escape_rejected(self, roots: tuple[Path, Path, Path]) -> None:
        """④ 包内形态 `{{path:./../outside/m.md}}` 越出包根 → 拒收。

        包根允许根只有包根自己：`./` 不是"相对 CWD"，越出包根同样越界。
        """
        _system, pack_root, outside = roots
        target = outside / "m.md"
        _write(target, "PACK-ESCAPE-SECRET")
        # 构造 ../ 相对包根的形态
        rel = "../" * 1 + "../outside/m.md"
        plugin = _load_plugin_module().PromptBuildPlugin()
        ctx = _ctx(
            {
                "context.system_prompt": f"{{{{path:./{rel}}}}}",
                "context.agent_pack_root": str(pack_root),
            }
        )

        content = _execute(plugin, ctx)

        assert "PACK-ESCAPE-SECRET" not in content
        assert content == ""

    def test_directory_escape_rejected(self, roots: tuple[Path, Path, Path]) -> None:
        """⑤ 目录形态越界（`{{path:../}}`，即 tmp 层）→ 不遍历根外目录。"""
        _system, _pack, outside = roots
        _write(outside / "leak.md", "DIR-LEAK-SECRET")
        plugin = _load_plugin_module().PromptBuildPlugin()
        ctx = _ctx({"context.system_prompt": "{{path:../}}"})

        content = _execute(plugin, ctx)

        assert "DIR-LEAK-SECRET" not in content
        assert content == ""
        assert "<files dir=" not in content, "越界目录不得进入遍历分支"

    def test_symlink_escape_rejected(
        self, roots: tuple[Path, Path, Path], caplog: pytest.LogCaptureFixture
    ) -> None:
        """⑥ 根内符号链接指向根外 → 经 resolve() 归一后同样拒收。"""
        system_root, _pack, outside = roots
        _write(outside / "linked-secret.md", "SYMLINK-SECRET")
        link = system_root / "config" / "innocent.md"
        try:
            link.symlink_to(outside / "linked-secret.md")
        except (OSError, NotImplementedError):  # pragma: no cover — 无符号链接权限的平台
            pytest.skip("平台不支持创建符号链接")
        plugin = _load_plugin_module().PromptBuildPlugin()
        ctx = _ctx({"context.system_prompt": "{{path:config/innocent.md}}"})

        with caplog.at_level(logging.WARNING):
            content = _execute(plugin, ctx)

        assert "SYMLINK-SECRET" not in content
        assert any("路径越界" in r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR)


# ═══════════════ 解析点契约：reason 三态可辨 ═══════════════


class TestResolveTargetReason:
    """`_resolve_target` 明确区分"没配 / 根不可得 / 越界"，日志文案因此不同。"""

    def test_reason_blank_and_roots(self, roots: tuple[Path, Path, Path]) -> None:
        """空串 → reason=empty；根内相对 → reason="" 且 path 存在。"""
        system_root, _pack, _outside = roots
        _write(system_root / "config" / "ok.md", "ok")
        plugin = _load_plugin_module().PromptBuildPlugin()

        assert plugin._resolve_target(_ctx(), "  ").reason == "empty"
        ok = plugin._resolve_target(_ctx(), "config/ok.md")
        assert ok.reason == "" and ok.path is not None and ok.path.is_file()

    def test_reason_escape(self, roots: tuple[Path, Path, Path]) -> None:
        """越界 → reason=escape 且 path=None（拒绝，不给半截路径）。"""
        plugin = _load_plugin_module().PromptBuildPlugin()

        escaped = plugin._resolve_target(_ctx(), "../outside/whatever.md")

        assert escaped.path is None
        assert escaped.reason == "escape"

    def test_reason_no_pack_root(self, roots: tuple[Path, Path, Path]) -> None:
        """`./` 形态但无包根 → reason=no_pack_root（与越界可辨）。"""
        plugin = _load_plugin_module().PromptBuildPlugin()

        assert plugin._resolve_target(_ctx(), "./materials/m.md").reason == "no_pack_root"

    def test_reason_no_system_root(self, roots: tuple[Path, Path, Path], tmp_path: Path) -> None:
        """系统根不可得 → reason=no_system_root（不拼半截路径、不放行）。"""
        plugin = _load_plugin_module().PromptBuildPlugin()
        plugin._system_root = lambda: None  # type: ignore[method-assign]

        resolved = plugin._resolve_target(_ctx(), "config/ok.md")

        assert resolved.path is None
        assert resolved.reason == "no_system_root"

    def test_root_itself_allowed(self, roots: tuple[Path, Path, Path]) -> None:
        """根本身（`{{path:.}}`）不算越界——边界包含根。"""
        system_root, _pack, _outside = roots
        plugin = _load_plugin_module().PromptBuildPlugin()

        resolved = plugin._resolve_target(_ctx(), ".")

        assert resolved.path == system_root.resolve()
        assert resolved.reason == ""
