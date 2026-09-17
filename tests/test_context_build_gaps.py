# @feature: FP-0.2.二 context_build 分支覆盖补齐 | @ci: python-coverage
"""context_build 插件未覆盖分支的行为契约补测。

锁八件事（对应 plugin.py 缺行）：
1. **config_id 回退匹配**：agent 文件名 ≠ agent_id 时按 yaml 头部
   config_id 匹配；不可读的 *.yaml 项（如同名目录）跳过不中断扫描；
2. **空 agent_id**：空身份不发起任何文件加载；
3. **yaml 缓存**：mtime 未变命中缓存，文件变更后失效重读；
4. **非 dict 形态 yaml**：列表/标量按空配置运行，不上抛；
5. **priority 属性**：默认 10，显式配置（含 0）原值生效；
6. **血缘投影**：有 lineage.* 无 agent.id 显式告警；task.id 一律取
   state.pipeline_id（引擎 id 权威，调用方预传值被覆盖）；
7. **static_vars / constraints_text 装载**：enabled 开关、空表、
   非 list 形态不装载；约束渲染区分 [必须]/[建议] 且过滤非字符串项；
8. **层级覆盖**：yaml level（strip+upper）覆盖默认 L1，插件配置
   agent_level 显式值最高优先，非 L 前缀不采纳。

[来源: coverage.xml 2026-09-13 缺行清单]
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import pytest

from tests._pipeline_plugin_path import add_plugin_dir

add_plugin_dir("input", "context_build")
import plugin as context_build_mod  # noqa: E402


def _ctx(state: dict):
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config={})


def _write_agent(agents_dir: Path, agent_id: str, content: str) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / f"{agent_id}.yaml").write_text(content, encoding="utf-8")


def _run(config, state: dict) -> dict:
    cb = context_build_mod.ContextBuildPlugin(config=config)
    return asyncio.run(cb.execute(_ctx(state))).state_updates


@pytest.fixture()
def config_root(tmp_path, monkeypatch):
    """隔离的 AGENTOS_CONFIG_ROOT（agents 目录按用例内容写入）。"""
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path))
    return tmp_path


# ── 1. config_id 回退匹配（_find_agent_yaml fallback 扫描）──


def test_agent_yaml_matched_by_config_id_fallback(config_root, tmp_path):
    """文件名 ≠ agent_id 时按 yaml 头部 config_id 匹配（code_writer 常态）。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "code_writer.yaml").write_text(
        "display_name: 代码审查专家\nconfig_id: cw_agent\n", encoding="utf-8"
    )
    updates = _run({}, {"agent.id": "cw_agent"})
    assert updates["context.agent_name"] == "代码审查专家", (
        "文件名未命中时应按 config_id 回退匹配并装载该 yaml"
    )


def test_config_id_mismatch_is_not_matched(config_root, tmp_path):
    """config_id 不一致/缺失 → 不回退命中，按无配置默认运行。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir(parents=True)
    (agents_dir / "other.yaml").write_text(
        "display_name: 其他\nconfig_id: someone_else\n", encoding="utf-8"
    )
    cb = context_build_mod.ContextBuildPlugin(config={"agent_name": "默认名"})
    updates = asyncio.run(cb.execute(_ctx({"agent.id": "ghost"}))).state_updates
    assert updates["context.agent_name"] == "默认名", "config_id 不匹配不得误装其他 agent"


def test_unreadable_fallback_entry_skipped(config_root, tmp_path):
    """回退扫描中不可读的 *.yaml 项（目录伪装）被跳过：不中断、不误报、默认运行。"""
    (tmp_path / "agents").mkdir(parents=True)
    (tmp_path / "agents" / "decoy.yaml").mkdir()  # 同名目录：open 必抛 OSError
    updates = _run({"system_prompt": "默认骨架"}, {"agent.id": "any_agent"})
    assert updates["context.system_prompt"] == "默认骨架", (
        "扫描遇不可读项应跳过并安全结束（未找到 → 默认运行），不得上抛"
    )


# ── 2. 空 agent_id ──


def test_empty_agent_id_loads_no_config(config_root, tmp_path):
    """空身份不发起文件加载；对照：非空 id 正常加载同一目录下的 yaml。"""
    _write_agent(tmp_path / "agents", "agentos", "display_name: 主\n")
    cb = context_build_mod.ContextBuildPlugin(config={})
    assert cb._load_agent_config("") == {}, "空 agent_id 必须直接返回空配置"
    assert cb._load_agent_config("agentos") == {"display_name": "主"}, (
        "对照：同目录非空 id 应正常命中 yaml"
    )


# ── 3. yaml 缓存与 mtime 失效 ──


def test_agent_yaml_cache_hit_and_mtime_invalidation(config_root, tmp_path):
    """mtime 未变命中缓存返回同内容；文件变更（mtime 前移）后失效重读。"""
    p = tmp_path / "agents" / "cached_agent.yaml"
    _write_agent(tmp_path / "agents", "cached_agent", "display_name: V1\n")
    os.utime(p, (1_000_000, 1_000_000))  # 显式固定 mtime，免时序假设
    cb = context_build_mod.ContextBuildPlugin(config={})
    first = cb._load_agent_config("cached_agent")
    again = cb._load_agent_config("cached_agent")
    assert again == first == {"display_name": "V1"}, "mtime 未变应命中缓存返回同内容"
    p.write_text("display_name: V2\n", encoding="utf-8")
    os.utime(p, (2_000_000, 2_000_000))
    reloaded = cb._load_agent_config("cached_agent")
    assert reloaded == {"display_name": "V2"}, "mtime 变更后缓存必须失效重读"


# ── 4. 非 dict 形态 yaml ──


@pytest.mark.parametrize(
    "raw_yaml",
    ["- alpha\n- beta\n", "只是一个字符串\n", "42\n"],
    ids=["list_form", "string_form", "scalar_form"],
)
def test_non_dict_yaml_treated_as_empty_config(config_root, tmp_path, raw_yaml):
    """yaml 合法解析但非 dict（列表/标量）→ 按空配置运行，不上抛。"""
    _write_agent(tmp_path / "agents", "flat_agent", raw_yaml)
    updates = _run({"system_prompt": "默认骨架"}, {"agent.id": "flat_agent"})
    assert updates["context.system_prompt"] == "默认骨架", (
        "非 dict yaml 等价无配置：回退插件默认 system_prompt，管道不中断"
    )


# ── 5. priority 属性 ──


@pytest.mark.parametrize(
    "config,expected",
    [({}, 10), ({"priority": 42}, 42), ({"priority": 0}, 0)],
    ids=["default", "explicit", "explicit_zero"],
)
def test_priority_property(config, expected):
    """priority 默认 10（准备级），显式配置原值生效（含显式 0）。"""
    cb = context_build_mod.ContextBuildPlugin(config=config)
    assert cb.priority == expected
    assert isinstance(cb.priority, int)


# ── 6. 血缘投影（task.id / agent.id 缺失告警）──


@pytest.mark.parametrize(
    "state,expect_present,expected",
    [
        (
            {
                "agent.id": "a",
                "lineage.parent_pipeline_id": "pp",
                "pipeline_id": "pipe-42",
                "task.id": "stale-old",
            },
            True,
            "pipe-42",
        ),
        ({"agent.id": "a", "lineage.parent_pipeline_id": "pp"}, True, ""),
        ({"agent.id": "a"}, False, None),
    ],
    ids=["lineage_with_pipeline_id_beats_stale", "lineage_without_pipeline_id_empty", "chat_pipeline_no_projection"],
)
def test_task_id_projection_follows_lineage(config_root, tmp_path, state, expect_present, expected):
    """有 lineage.* 才投影 task.id，且权威 = state.pipeline_id（预传值被覆盖）。"""
    (tmp_path / "agents").mkdir(parents=True)
    updates = _run({}, state)
    if expect_present:
        assert updates["task.id"] == expected
        assert updates["task.id"] == str(state.get("pipeline_id", "") or ""), (
            "task.id 必须等于引擎管道 id（pipeline_id），预传 task.id 被覆盖"
        )
    else:
        assert "task.id" not in updates, "聊天管道（无血缘键）不得写 task.*"


@pytest.mark.parametrize(
    "state,should_warn",
    [
        ({"lineage.parent_pipeline_id": "pp-1"}, True),
        ({"agent.id": "plain_agent"}, False),
        ({"agent.id": "a", "lineage.parent_pipeline_id": "pp"}, False),
    ],
    ids=["lineage_without_identity_warns", "identity_only_silent", "lineage_with_identity_silent"],
)
def test_lineage_identity_warning(config_root, tmp_path, caplog, state, should_warn):
    """有父血缘却缺 agent.id → 显式 warning；有身份（或无血缘）不告警。"""
    (tmp_path / "agents").mkdir(parents=True)
    with caplog.at_level(logging.WARNING, logger="plugin"):
        asyncio.run(context_build_mod.ContextBuildPlugin(config={}).execute(_ctx(state)))
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING and "agent.id" in r.getMessage()]
    assert bool(warned) == should_warn, "血缘与身份缺失组合必须决定是否告警"


# ── 7. static_vars 装载 ──


def test_static_vars_loaded_into_state(config_root, tmp_path):
    """yaml static_vars.items → context.static_vars 原值装载。"""
    _write_agent(
        tmp_path / "agents",
        "sv_agent",
        "display_name: SV\n"
        "static_vars:\n"
        "  enabled: true\n"
        "  items:\n"
        "  - name: 决策\n"
        "    template: t\n",
    )
    updates = _run({}, {"agent.id": "sv_agent"})
    assert updates["context.static_vars"] == [{"name": "决策", "template": "t"}]


@pytest.mark.parametrize(
    "yaml_extra",
    [
        "static_vars:\n  enabled: false\n  items:\n  - a\n",
        "static_vars:\n  items: []\n",
        "static_vars:\n  items: {k: v}\n",
        "",
    ],
    ids=["disabled", "empty_items", "items_not_list", "absent"],
)
def test_static_vars_not_loaded(config_root, tmp_path, yaml_extra):
    """关闭/空表/非 list/未声明 → 均不写 context.static_vars（零兜底）。"""
    _write_agent(tmp_path / "agents", "sv_off", "display_name: X\n" + yaml_extra)
    updates = _run({}, {"agent.id": "sv_off"})
    assert "context.static_vars" not in updates


# ── 8. constraints_text 渲染 ──


@pytest.mark.parametrize(
    "yaml_block,expected",
    [
        (
            "hard_constraints: [只读, 禁删除]\nsoft_constraints: [先规划]\n",
            "- [必须] 只读\n- [必须] 禁删除\n- [建议] 先规划",
        ),
        ("hard_constraints: [A]\n", "- [必须] A"),
        ("soft_constraints: [S]\n", "- [建议] S"),
        ("hard_constraints:\n- ok\n- 42\n- null\n", "- [必须] ok"),
        ("", None),
        ("hard_constraints: []\nsoft_constraints: []\n", None),
    ],
    ids=["both", "hard_only", "soft_only", "non_string_filtered", "absent", "empty_lists"],
)
def test_constraints_text_rendering(config_root, tmp_path, yaml_block, expected):
    """hard/soft 约束渲染为 [必须]/[建议] 文本块；非字符串项过滤；空声明不写键。"""
    _write_agent(tmp_path / "agents", "cst_agent", "display_name: C\n" + yaml_block)
    updates = _run({}, {"agent.id": "cst_agent"})
    if expected is None:
        assert "context.constraints_text" not in updates
    else:
        text = updates["context.constraints_text"]
        assert text == expected
        lines = text.splitlines()
        assert all(
            line.startswith("- [必须] ") or line.startswith("- [建议] ") for line in lines
        ), "每一行都必须是带前缀的约束项（非字符串项不得混入）"


# ── 9. agent 层级覆盖 ──


@pytest.mark.parametrize(
    "yaml_level,expected",
    [('"  l3 "', "L3"), ("L2", "L2"), ("M2", "L1"), (None, "L1")],
    ids=["lowercase_with_spaces", "already_upper", "not_l_prefix", "no_level"],
)
def test_agent_level_from_yaml(config_root, tmp_path, yaml_level, expected):
    """yaml level（strip+upper，L 前缀才采纳）覆盖默认 L1；is_project 随层级联动。"""
    body = "display_name: X\n" + (f"level: {yaml_level}\n" if yaml_level is not None else "")
    _write_agent(tmp_path / "agents", "lvl_agent", body)
    updates = _run({}, {"agent.id": "lvl_agent"})
    assert updates["agent_level"] == expected
    assert updates["context.is_project"] == (expected == "L1"), (
        "is_project 必须与最终层级联动（仅 L1 为项目级）"
    )


def test_plugin_agent_level_config_beats_yaml(config_root, tmp_path):
    """插件配置显式 agent_level 最高优先：yaml level 不覆盖。"""
    _write_agent(tmp_path / "agents", "lvl_explicit", "level: L3\n")
    updates = _run({"agent_level": "L2"}, {"agent.id": "lvl_explicit"})
    assert updates["agent_level"] == "L2"


# ── 10. extra_context 合并 ──


def test_extra_context_merged(config_root, tmp_path):
    """插件配置 extra_context → context.<key> 逐项写入。"""
    (tmp_path / "agents").mkdir(parents=True)
    updates = _run({"extra_context": {"tenant": "acme", "depth": 2}}, {})
    assert updates["context.tenant"] == "acme"
    assert updates["context.depth"] == 2


@pytest.mark.parametrize(
    "config",
    [{}, {"extra_context": {}}],
    ids=["absent", "empty_dict"],
)
def test_extra_context_absent_writes_nothing(config_root, tmp_path, config):
    """未声明/空 extra_context → 不产生任何额外 context.* 键。"""
    (tmp_path / "agents").mkdir(parents=True)
    updates = _run(config, {})
    assert "context.tenant" not in updates
    extra_keys = [k for k in updates if k not in {
        "context.system_prompt", "context.agent_name", "agent_level",
        "context.session_id", "context.task_id", "context.iteration",
        "context.is_tool_execution", "context.is_project",
    }]
    assert extra_keys == [], "无 extra_context 时不得出现计划外的 context.* 键"


# ── 11. 用户配置层不可用降级（plugin.py:137-138）──


def test_user_config_layer_unavailable_degrades_to_factory(config_root, tmp_path, monkeypatch):
    """user_space 导入/调用失败 → 记 debug 后仅走 factory 层，不中断加载。

    用户配置层是可选叠加面：其不可用（sidecar 无 user_space 模块、调用异常）
    不得让 agent yaml 解析整体失败——降级为「仅 factory」正是双根解析的兜底语义。
    """
    _write_agent(tmp_path / "agents", "factory_agent", "display_name: 出厂版\n")

    import sys
    import types as _types

    # 假 user_space：user_config_dir 抛异常（模拟调用期失败）
    fake = _types.ModuleType("user_space")

    def _boom() -> object:
        raise RuntimeError("用户根解析失败")

    fake.user_config_dir = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "user_space", fake)

    updates = _run({}, {"agent.id": "factory_agent"})

    assert updates["context.agent_name"] == "出厂版", "用户层异常不得阻断 factory 层命中"


def test_user_config_dir_none_skips_user_candidate(config_root, tmp_path, monkeypatch):
    """user_config_dir 返回 None（无用户根）→ 只留 factory 候选，正常命中。"""
    _write_agent(tmp_path / "agents", "solo_agent", "display_name: 单根版\n")

    import sys
    import types as _types

    fake = _types.ModuleType("user_space")
    fake.user_config_dir = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "user_space", fake)

    updates = _run({}, {"agent.id": "solo_agent"})

    assert updates["context.agent_name"] == "单根版"


# ── 12. 层级 agent 键直解析（BUG-32）──


def test_hierarchical_agent_key_resolved_by_path(config_root, tmp_path):
    """层级键（executor/generation/novel_writer_agent 形态）按注册表同构相对
    路径直解析。

    task_birth 把派发 target_id 完整写进 state['agent.id']（含分隔符）；
    文件名 basename 比对永不命中带分隔符的键（p.name 无 /）、config_id 兜底
    也只存裸 id——两级都落空 = 配置整体不装载（BUG-32 现场：tool_ids 断链 →
    LLM 零工具 → 写作任务无法落盘，600s 超时 failed）。
    """
    nested = tmp_path / "agents" / "executor" / "generation"
    nested.mkdir(parents=True)
    (nested / "novel_writer_agent.yaml").write_text(
        "display_name: 章节写作专家\n"
        "tool_ids: [file_read, file_write, task_evaluate]\n",
        encoding="utf-8",
    )
    updates = _run({}, {"agent.id": "executor/generation/novel_writer_agent"})
    assert updates["context.agent_name"] == "章节写作专家", "层级键必须直解析命中 yaml"
    assert updates["tool_ids"] == ["file_read", "file_write", "task_evaluate"], (
        "层级键 yaml 的 tool_ids 必须随配置注入（断链 = LLM 零工具）"
    )


def test_hierarchical_key_user_layer_takeover_wins(config_root, tmp_path, monkeypatch):
    """层级键双根序不变：用户层同路径文件接管生效，factory 同名不参与。"""
    user_root = tmp_path / "user-root"
    user_root.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    nested = tmp_path / "agents" / "executor" / "generation"
    nested.mkdir(parents=True)
    (nested / "writer.yaml").write_text("display_name: 出厂版\n", encoding="utf-8")
    user_nested = user_root / "config" / "agents" / "executor" / "generation"
    user_nested.mkdir(parents=True)
    (user_nested / "writer.yaml").write_text("display_name: 用户版\n", encoding="utf-8")

    updates = _run({}, {"agent.id": "executor/generation/writer"})
    assert updates["context.agent_name"] == "用户版", (
        "层级键也必须用户层优先（双根语义与裸键一致）"
    )


def test_hierarchical_key_traversal_rejected(config_root, tmp_path):
    """含 .. 段的键拒绝直解析（防注册表外穿越）；两级扫描也不命中 → 默认运行。"""
    (tmp_path / "agents").mkdir(parents=True)
    escaped = tmp_path / "outside"
    escaped.mkdir()
    (escaped / "evil.yaml").write_text("display_name: 越权\n", encoding="utf-8")

    updates = _run({}, {"agent.id": "../../outside/evil"})
    assert updates["context.agent_name"] != "越权", ".. 段不得穿越注册表根"
    assert "tool_ids" not in updates


def test_hierarchical_key_empty_segment_skips_direct(config_root, tmp_path):
    """含空段键（a//b 形态）不直解析：防路径歧义，两级扫描不命中 → 默认运行。"""
    (tmp_path / "agents").mkdir(parents=True)
    updates = _run({}, {"agent.id": "a//b"})
    assert "tool_ids" not in updates
    assert updates["context.agent_name"] == ""
