# @feature: FP-0.2.〇 管道引擎 | @vision: V3 可嵌入 | @ci: none-local
"""{{session_workspace}} 占位符行为测试。

背景（2026-09-28 R298 装机回归实证）：会话工作空间路径此前没有进入模型
上下文的稳定通道——种子提示词只引用 {{user_root}}（用户数据根），模型对
"我的工作空间在哪"的知情全靠 file_read 结果回显（成功载荷 file 字段 =
工作区锚定后的绝对路径；失败 reason 内嵌根路径）偶发得知：发起工具调用
的轮次答得对，纯文本轮次答成 user_root。占位符体系补 {{session_workspace}}
（state.workspace，workspace_lifecycle init 写入），种子提示词以工作空间
锚点行稳定引用——知情从偶发副作用变为稳定契约。

契约：
1. state.workspace 在场（非空）→ 展开为该路径原文；
2. state 无 workspace 键 / 空串 / None → 空串（未知名静默清空同契约，
   不注入错误锚点，渲染永不失败）；
3. 与 {{workspace}}（系统根）严格分名，互不串义；
4. 种子提示词（config/agents/main/agentos.yaml）在 system_prompt 引用本
   占位符——锚点行随种子渲染端到端展开为 thread 工作空间路径。
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

_REPO_ROOT = _PLUGIN_DIR.parents[4]
_SEED_YAML = _REPO_ROOT / "config" / "agents" / "main" / "agentos.yaml"


def _load_plugin_module() -> Any:
    mod_name = "prompt_build_plugin_session_workspace_test"
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


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"workspace": r"D:\ws\.ai_workspaces\sessions\thread-abc123"}, r"D:\ws\.ai_workspaces\sessions\thread-abc123"),
        ({"workspace": "/data/ws/sessions/thread-xyz789"}, "/data/ws/sessions/thread-xyz789"),
        ({"workspace": ""}, ""),
        ({"workspace": None}, ""),
        ({}, ""),
    ],
    ids=["thread-win-path", "thread-posix-path", "empty-string", "none-value", "key-absent"],
)
def test_session_workspace_resolves_from_state(state: dict[str, Any], expected: str) -> None:
    """state.workspace 非空 → 路径原文；空串/None/缺键 → 空串（清空契约）。"""
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    out = _run(plugin._resolve_placeholder(PluginContext(state=state), "session_workspace"))
    assert out == expected
    if expected:
        assert "thread-" in out  # 性质断言：透传的是工作区路径本身，非拼装值


def test_session_workspace_distinct_from_workspace_system_root(tmp_path: Path) -> None:
    """分名不串义：同 state 下 {{workspace}}=系统根、{{session_workspace}}=state.workspace。"""
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    state = {"workspace": str(tmp_path / "sessions" / "thread-dual")}
    ctx = PluginContext(state=state)
    ws_out = _run(plugin._resolve_placeholder(ctx, "workspace"))
    sw_out = _run(plugin._resolve_placeholder(ctx, "session_workspace"))
    assert sw_out == str(tmp_path / "sessions" / "thread-dual")
    assert ws_out != sw_out  # {{workspace}} 是系统根，不随 state.workspace 走


def test_session_workspace_in_system_prompt_renders() -> None:
    """端到端：system_prompt 锚点行经 _resolve_placeholders 整体替换，无残留。"""
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    template = "**当前会话工作空间**：`{{session_workspace}}`——相对路径锚定此目录。"
    ctx = PluginContext(state={"workspace": r"D:\ws\sessions\thread-e2e"})
    out = _run(plugin._resolve_placeholders(ctx, template))
    assert r"D:\ws\sessions\thread-e2e" in out
    assert "{{" not in out


def test_main_seed_anchors_session_workspace_and_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    """种子契约 + 渲染 e2e：agentos.yaml system_prompt 引用本占位符，
    锚点行以 thread 工作空间渲染展开（装机形态：AGENTOS_USER_ROOT 在场）。"""
    assert _SEED_YAML.is_file(), f"主 agent 种子不存在: {_SEED_YAML}"
    seed_text = _SEED_YAML.read_text(encoding="utf-8")
    anchor_lines = [
        line
        for line in seed_text.splitlines()
        if "{{session_workspace}}" in line
    ]
    assert anchor_lines, "种子提示词未引用 {{session_workspace}}，工作空间锚点缺失"

    monkeypatch.setenv("AGENTOS_USER_ROOT", r"D:\fake\user_root")
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    ctx = PluginContext(state={"workspace": r"D:\ws\.ai_workspaces\sessions\thread-seed"})
    for line in anchor_lines:
        rendered = _run(plugin._resolve_placeholders(ctx, line.strip()))
        assert r"D:\ws\.ai_workspaces\sessions\thread-seed" in rendered
        assert "{{" not in rendered  # 锚点行全部占位符（含 {{user_root}} 类）均收敛
