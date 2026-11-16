# @feature: FP-0.2.〇 模式体系收口 B6 | @ci: python-coverage
"""{{project_agents_md_anchor}} 占位符行为测试（批 B6 项目宪法注入点）。

契约（ADR 2026-10-09 批 B6 + 2026-10-12 全文注入裁定）：
1. 挂靠项目（context_build 经 task.parent_project_id 注入 state.project_roots，
   仅挂靠管道有值）且项目根存在 AGENTS.md → 展开为来源头（带路径）+
   AGENTS.md 全文（UTF-8）；超过 PROJECT_AGENTS_MD_MAX_CHARS 按字符截断，
   尾部附"已截断，全文见 <路径>"标记；
2. 读取失败（权限/编码/IO）→ fail-soft 回退指针行（路径 + 先读再动手），
   warning 留痕，不阻断提示词构建；
3. 未挂靠 / project_roots 空 / 项目根无 AGENTS.md（方案段未定稿）/ 同名条目
   非文件 → 空串：仅挂靠项目注入（P7 裁定，普通任务零负担），不注错误锚点
   （静默清空同契约，渲染永不失败）；
4. 种子提示词（config/agents/main/agentos.yaml）在 system_prompt 引用本
   占位符——全文随种子渲染端到端展开（种子漂移即红）。
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

_SHARED_DIR = str(_PLUGIN_DIR.parents[0])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402

_REPO_ROOT = _PLUGIN_DIR.parents[2]
_SEED_YAML = _REPO_ROOT / "config" / "agents" / "main" / "agentos.yaml"


def _load_plugin_module() -> Any:
    mod_name = "prompt_build_plugin_project_agents_anchor_test"
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
    "state",
    [
        {"project_roots": [r"D:\ws\projects\no_md"]},
        {"project_roots": []},
        {},
        {"project_roots": "not-a-list"},
    ],
    ids=["attached-without-file", "empty-roots", "unattached", "malformed-roots"],
)
def test_anchor_zero_burden_without_agents_md(
    tmp_path: Path, state: dict[str, Any]
) -> None:
    """未挂靠 / 无 AGENTS.md / 形态非法 → 空串（零负担契约，不注错误锚点）。"""
    empty = tmp_path / "no_md"
    empty.mkdir()
    state = {k: ([empty] if v == [r"D:\ws\projects\no_md"] else v) for k, v in state.items()}
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    out = _run(plugin._resolve_placeholder(PluginContext(state=state), "project_agents_md_anchor"))
    assert out == ""


def test_small_file_injects_full_text(tmp_path: Path) -> None:
    """挂靠 + AGENTS.md 在场 → 来源头（带路径）+ 全文注入。"""
    proj = tmp_path / "demo"
    proj.mkdir()
    body = "# 宪法\n## 产物地图\n- tasks/<task_id>/ac.md"
    agents_md = proj / "AGENTS.md"
    agents_md.write_text(body, encoding="utf-8")
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    out = _run(
        plugin._resolve_placeholder(
            PluginContext(state={"project_roots": [str(proj)]}), "project_agents_md_anchor"
        )
    )
    assert str(agents_md) in out  # 来源头带路径，可溯源
    assert "已注入正文" in out
    assert body in out  # 性质断言（包含单调）：全文注入 ⇒ 原文逐字出现在注入结果内
    assert "先读再动手" not in out  # 指针行文案仅属读取失败回退路径


def test_oversized_file_truncated_at_cap(tmp_path: Path) -> None:
    """超上限 → 按字符截断（多字节不劈半）+ 截断标记带路径，越界尾部不出现。"""
    mod = _load_plugin_module()
    cap = mod.PROJECT_AGENTS_MD_MAX_CHARS
    proj = tmp_path / "big"
    proj.mkdir()
    tail = "\nTAIL-越界尾部标记"
    body = "甲" * (cap + 1000) + tail
    agents_md = proj / "AGENTS.md"
    agents_md.write_text(body, encoding="utf-8")
    plugin = mod.PromptBuildPlugin()
    out = _run(
        plugin._resolve_placeholder(
            PluginContext(state={"project_roots": [str(proj)]}), "project_agents_md_anchor"
        )
    )
    assert "已截断" in out and str(agents_md) in out  # 截断标记保留路径可溯源
    assert body[:cap] in out  # 按字符截断：原文前 cap 个字符逐字保留（中文多字节不劈半）
    assert body[: cap + 1] not in out  # 截断点精确：第 cap+1 个字符不出现
    assert tail not in out
    assert len(out) <= cap + 512  # 性质断言（长度上界）：注入总量受上限约束（余量 = 头尾标记）


def test_read_failure_falls_back_to_pointer_line(tmp_path: Path) -> None:
    """读取失败（UTF-8 解码不可行）→ fail-soft 回退指针行（路径 + 先读再动手）。"""
    proj = tmp_path / "broken"
    proj.mkdir()
    agents_md = proj / "AGENTS.md"
    agents_md.write_bytes(b"\xff\xfe\x80\x81 not-valid-utf8")  # is_file() 为真、解码必败
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    out = _run(
        plugin._resolve_placeholder(
            PluginContext(state={"project_roots": [str(proj)]}), "project_agents_md_anchor"
        )
    )
    assert str(agents_md) in out  # 指针行带路径，可指导 file_read
    assert "先读再动手" in out
    assert "已注入正文" not in out  # 回退是指针行形态，不是全文注入形态


def test_anchor_uses_first_project_root(tmp_path: Path) -> None:
    """多项目根（挂靠单项目语义下防御形态）→ 消费首个根。"""
    proj = tmp_path / "first"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("# 宪法", encoding="utf-8")
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    state = {"project_roots": [str(proj), str(tmp_path / "second")]}
    out = _run(plugin._resolve_placeholder(PluginContext(state=state), "project_agents_md_anchor"))
    assert str(proj) in out


def test_anchor_renders_without_residue_in_template() -> None:
    """端到端：模板锚点行经 _resolve_placeholders 整体替换，无占位符残留。"""
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()
    template = "    {{project_agents_md_anchor}}"
    ctx = PluginContext(state={"project_roots": []})  # 未挂靠 → 空串
    out = _run(plugin._resolve_placeholders(ctx, template))
    assert "{{" not in out


def test_main_seed_anchors_project_agents_md(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """种子契约 + 渲染 e2e：agentos.yaml system_prompt 引用本占位符；
    挂靠项目场景展开为来源头 + 全文，未挂靠场景整行收敛为空（无残留）。"""
    assert _SEED_YAML.is_file(), f"主 agent 种子不存在: {_SEED_YAML}"
    seed_text = _SEED_YAML.read_text(encoding="utf-8")
    anchor_lines = [line for line in seed_text.splitlines() if "{{project_agents_md_anchor}}" in line]
    assert anchor_lines, "种子提示词未引用 {{project_agents_md_anchor}}，项目宪法锚点缺失"

    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
    mod = _load_plugin_module()
    plugin = mod.PromptBuildPlugin()

    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "AGENTS.md").write_text("# 宪法", encoding="utf-8")
    attached = _run(
        plugin._resolve_placeholders(PluginContext(state={"project_roots": [str(proj)]}), anchor_lines[0].strip())
    )
    assert "AGENTS.md" in attached and "已注入正文" in attached and "# 宪法" in attached

    unattached = _run(
        plugin._resolve_placeholders(PluginContext(state={}), anchor_lines[0].strip())
    )
    assert unattached.strip() == ""
    assert "{{" not in unattached
