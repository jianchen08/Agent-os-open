# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""{{user_root}} 占位符行为测试。

背景（2026-09-27 R295 装机实证）：种子提示词写死开发机绝对路径随包出境，
主 agent 按提示词把工作空间锚到 dev 路径 → 隔离容器挂载源悬空。提示词中的
可写落盘锚点改用 {{user_root}}，运行时展开为用户数据根（AGENTOS_USER_ROOT，
装机版回落 %APPDATA%/agentos）——种子与机器真值分离。

契约：
1. 设置 AGENTOS_USER_ROOT 时展开为该值；
2. 未设 env 时回落 user_space.user_root() 的默认解析；
3. 解析结果为 None（无 env 且系统数据目录不可得）→ 空串 + warning 留痕，
   不抛异常（提示词渲染永不失败）；
4. {{user_root:...}} 带参数形态不归属本类型（走未知占位符路径）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_SHARED_DIR = str(_PLUGIN_DIR.parents[2])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from pipeline.plugin import PluginContext  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "prompt_build_plugin_user_root_test"
    module_path = _PLUGIN_DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None
    assert spec.loader is not None
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


def test_user_root_expands_to_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """AGENTOS_USER_ROOT 在场时 {{user_root}} 展开为该绝对路径。"""
    monkeypatch.setenv("AGENTOS_USER_ROOT", r"D:\fake\user_root")
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    out = _run(plugin._resolve_placeholder(PluginContext(state={}), "user_root"))
    assert out == r"D:\fake\user_root"


def test_user_root_falls_back_to_user_space_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """env 缺失时经 user_space.user_root() 默认解析（%APPDATA%/agentos）。"""
    monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
    import user_space

    monkeypatch.setattr(user_space, "user_root", lambda: Path(r"C:\fake\appdata\agentos"))
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    out = _run(plugin._resolve_placeholder(PluginContext(state={}), "user_root"))
    assert out == str(Path(r"C:\fake\appdata\agentos"))


def test_user_root_none_yields_empty_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """user_root() 返回 None → 空串（渲染不失败，warning 可观测）。"""
    monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
    import user_space

    monkeypatch.setattr(user_space, "user_root", lambda: None)
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    out = _run(plugin._resolve_placeholder(PluginContext(state={}), "user_root"))
    assert out == ""


def test_user_root_in_system_prompt_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：system_prompt 模板中的 {{user_root}} 经 _resolve_placeholders 整体替换。"""
    monkeypatch.setenv("AGENTOS_USER_ROOT", r"D:\fake\user_root")
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    ctx = PluginContext(state={"context.system_prompt": "家：{{user_root}}；配置：config/agents/main/"})
    out = _run(plugin._resolve_placeholders(ctx, ctx.state["context.system_prompt"]))
    expected_root = str(Path(r"D:\fake\user_root"))
    assert out == f"家：{expected_root}；配置：config/agents/main/"
