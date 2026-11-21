# @feature: FP-0.2.二 mode_personal M1 执法测试 | @ci: python-coverage
"""mode_personal 模式工具并入执法测试（审查口径，方案 §2.5 四态 + R6 修正）。

七类口径各 ≥2 例：
①非任务管道三来源（主聊天/通道入站桥/扮演子管道形态）pim 可见——agent_config_load
  并入 + tool_schema 真插件链上面；
②任务管道三来源（task_submit/评估/review 形态）不可见 + 幻觉调用天然被拒
  （结构性：面内无此 id = LLM 永不见；内核 tool-surface 终审同源白名单）；
③零包外配置断言（启用前后 main yaml 及一切既有配置 diff=0）；
④模式正交（其他模式绑定会话 pim 仍在面）；
⑤inherit/未声明形态不并入（K10 空面语义不反转）；
⑥禁用包不进面（探针活注册表交集）；
⑦标记键出口断言（tool_ids.mode_source 独立键 + export_fields 声明 + 列表纯字符串）。

真依赖：出厂根真实 mode_personal 包（声明面）+ tool_schema 真插件（链上面）；
tool-surface capability 为外部依赖（内核注册表），用脚本化替身回放——与
test_tool_schema_drift_detection 同一替身形态。
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED = _REPO_ROOT / "plugins" / "shared"

for _d in [_REPO_ROOT, _SHARED]:
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from tests._pipeline_plugin_path import add_plugin_dir  # noqa: E402

add_plugin_dir("agent_config_load")
import plugin as agent_config_load_mod  # noqa: E402

add_plugin_dir("tool_schema")
import plugin as tool_schema_mod  # noqa: E402

import mode_keys  # noqa: E402

MODE_TOOL = "pim"
MODE_PKG = "mode_personal"


# ── 替身与夹具 ──


def _registry_caller(live: set[str], calls: list[tuple[str, dict]]):
    """tool-surface capability 替身：live = 活注册表工具集（脚本化过滤）。"""

    async def caller(method: str, params: dict, timeout=None):
        calls.append((method, params))
        schemas = [
            {"type": "function", "function": {"name": name, "parameters": {}}}
            for name in sorted(live)
            if name in set(params.get("tool_ids") or [])
        ]
        return {"schemas": schemas, "contracts": {}}

    return caller


def _ctx(state: dict[str, Any]):
    from _libs.pipeline_base.plugin import PluginContext

    return PluginContext(state=dict(state), config={})


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _wired_agent_yaml(tmp_path: Path, tool_ids_line: str = "tool_ids: [file_read]") -> None:
    """隔离 factory agent yaml（显式 list 形态；AGENTOS_CONFIG_ROOT 指向 tmp）。

    主控（agentos）+ 任务管道三来源的目标执行者各一份——执行者 yaml 缺席会让
    断言空转（cfg={} → 键不写），钉不住"目标 agent yaml 三态"语义。
    """
    agents = tmp_path / "factory" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    for name in ("agentos", "code_writer", "executor", "evaluator", "reviewer"):
        (agents / f"{name}.yaml").write_text(
            f"display_name: {name}\n{tool_ids_line}\n", encoding="utf-8"
        )


def _isolate_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.setenv("AGENTOS_USER_PLUGINS_DIR", str(tmp_path / "user-root" / "plugins"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "factory"))
    (tmp_path / "factory" / "agents").mkdir(parents=True, exist_ok=True)


def _load_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, live: set[str]
) -> tuple[Any, Any, list[tuple[str, dict]]]:
    """装配 agent_config_load + tool_schema 双插件链（同一注册表替身）。"""
    _isolate_env(tmp_path, monkeypatch)
    _wired_agent_yaml(tmp_path)
    calls: list[tuple[str, dict]] = []
    caller = _registry_caller(live, calls)
    acl = agent_config_load_mod.AgentConfigLoadPlugin(config={})
    acl.set_capability_caller(caller)
    ts = tool_schema_mod.ToolSchemaPlugin(config={})
    ts.set_capability_caller(caller)
    return acl, ts, calls


def _face_names(tool_schema: Any, state: dict[str, Any]) -> set[str]:
    """跑 tool_schema 真插件 → LLM 可见工具面（tool_schemas 的 function.name 集）。"""
    updates = _run(tool_schema.execute(_ctx(state))).state_updates
    return {
        (s.get("function") or {}).get("name") for s in updates.get("tool_schemas", [])
    }


# 非任务管道三来源（审查 Q2 实测枚举；扮演子管道 = state 仅 {agent.id}）
NON_TASK_SOURCES = [
    ("主聊天", {"agent.id": "agentos", "session_id": "s1", "pipeline_id": "p1"}),
    ("主聊天_重载形态", {"agent.id": "agentos", "session_id": "s2"}),
    ("通道入站桥", {"agent.id": "agentos", "channel": {"channel_id": "feishu", "chat_id": "c1"}}),
    ("通道入站桥_另一通道", {"agent.id": "agentos", "channel": {"channel_id": "wecom"}}),
    ("扮演子管道形态", {"agent.id": "agentos"}),
    ("附身会话", {"agent.id": "agentos", "execution_context": {"mode": "roleplay"}}),
]

# 任务管道三来源（审查 Q2 对照组：task_submit / 评估子管道 / review 出生 state 形态）
TASK_SOURCES = [
    (
        "task_submit_子任务",
        {"agent.id": "code_writer", "task.id": "pipe-child",
         "lineage.parent_pipeline_id": "pipe-parent", "lineage.root": "pipe-root"},
    ),
    (
        "task_submit_根任务",
        {"agent.id": "executor", "task.id": "pipe-root",
         "lineage.root": "pipe-root", "lineage.origin": {"kind": "chat"}},
    ),
    (
        "评估子管道",
        {"agent.id": "evaluator", "task.id": "pipe-eval",
         "lineage.parent_pipeline_id": "pipe-child", "isolation.level": "isolated"},
    ),
    (
        "review_形态",
        {"agent.id": "reviewer", "task.id": "pipe-review",
         "lineage.origin.root": "pipe-root", "lineage.origin.mode": "review"},
    ),
]


class TestNonTaskSourcesVisible:
    """①非任务管道三来源 pim 可见（并入 + 真插件链上面）。"""

    @pytest.mark.parametrize(("label", "state"), NON_TASK_SOURCES)
    def test_pim_on_face(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        state: dict[str, Any],
    ) -> None:
        acl, ts, calls = _load_chain(tmp_path, monkeypatch, live={"file_read", MODE_TOOL})

        merged = _run(acl.execute(_ctx(state))).state_updates
        face = _face_names(ts, dict(state, **{
            "tool_ids": merged["tool_ids"],
        }))

        assert merged["tool_ids"] == ["file_read", MODE_TOOL], label
        assert MODE_TOOL in face, label
        assert "file_read" in face, label

    def test_dual_run_rebuild_no_stale(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """①补充：同实例跨 run 重算（tool_ids 不持久化，每 run 重建无陈旧化）。"""
        acl, ts, _ = _load_chain(tmp_path, monkeypatch, live={"file_read", MODE_TOOL})
        state = {"agent.id": "agentos"}
        first = _run(acl.execute(_ctx(state))).state_updates
        second = _run(acl.execute(_ctx(state))).state_updates
        assert first["tool_ids"] == second["tool_ids"] == ["file_read", MODE_TOOL]


class TestTaskSourcesInvisible:
    """②任务管道三来源不可见 + 幻觉调用天然被拒。"""

    @pytest.mark.parametrize(("label", "state"), TASK_SOURCES)
    def test_pim_structurally_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        state: dict[str, Any],
    ) -> None:
        acl, ts, calls = _load_chain(tmp_path, monkeypatch, live={"file_read", MODE_TOOL})

        merged = _run(acl.execute(_ctx(state))).state_updates

        # 目标 agent yaml 未列 pim → tool_ids 恒等 yaml 三态（结构性不可见）
        assert "pim" not in merged.get("tool_ids", []), label
        assert "tool_ids.mode_source" not in merged, label
        assert calls == [], label  # 判据先行：任务管道零探针
        # 链上面核验：tool_schema 真插件产物无 pim（LLM 永不见 = 幻觉调用
        # unknown tool 天然被拒；内核 tool-surface 终审同源白名单）
        face = _face_names(ts, merged)
        assert MODE_TOOL not in face, label

    def test_hallucinated_call_unknown_tool_semantics(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """②幻觉调用面验证：任务管道 tool_ids 白名单内无 pim → 内核终审语义
        （白名单外工具不在 tool-surface 产物）= unknown tool 天然拒绝。"""
        acl, ts, _ = _load_chain(tmp_path, monkeypatch, live={"file_read", MODE_TOOL})
        state = dict(TASK_SOURCES[0][1])
        merged = _run(acl.execute(_ctx(state))).state_updates
        face = _face_names(ts, merged)
        # 白名单（yaml file_read）过滤后 pim 不在产物——即使注册表活的
        assert face == {"file_read"}

    def test_level_guard_zero_touch(self) -> None:
        """②纵深零改动断言：pim 非 TASK_CONTROL_TOOLS，level_guard 无需动
        （零新插件零新守卫；幻觉调用由面结构拒绝）。"""
        add_plugin_dir("level_guard")
        import plugin as level_guard_mod  # noqa: PLC0415

        assert "pim" not in level_guard_mod.TASK_CONTROL_TOOLS


class TestZeroOutOfPackageConfigDiff:
    """③零包外配置断言：启用（并入链真跑）前后既有配置 diff=0。"""

    def test_merge_never_mutates_real_configs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """并入链真跑一轮（真实 config/agents 装载）后，config/ 全树内容哈希
        零变化（加行制 = state 加行，运行时零写配置面）。"""
        def _config_digests() -> dict[str, str]:
            digests: dict[str, str] = {}
            config_root = _REPO_ROOT / "config"
            for p in sorted(config_root.rglob("*.yaml")):
                digests[str(p.relative_to(config_root))] = hashlib.sha256(
                    p.read_bytes()
                ).hexdigest()
            return digests

        # 用户层隔离（确定性），config 根重指真实仓配置（插件每 run 读 env）
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
        monkeypatch.setenv("AGENTOS_USER_PLUGINS_DIR", str(tmp_path / "user-root" / "plugins"))
        monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "config"))
        before = _config_digests()
        calls: list[tuple[str, dict]] = []
        acl = agent_config_load_mod.AgentConfigLoadPlugin(config={})
        acl.set_capability_caller(_registry_caller({"file_read", MODE_TOOL}, calls))
        _run(acl.execute(_ctx({"agent.id": "agentos"})))
        assert before == _config_digests()

    @pytest.mark.parametrize("yaml_path", [
        "config/agents/main/agentos.yaml",
        "config/agents/executor/general_agent.yaml",
    ])
    def test_whitelist_never_lists_mode_tools(self, yaml_path: str) -> None:
        """③main yaml 及执行者 yaml 白名单零包外改动（静态断言：不含 pim/
        mode_personal 并入痕迹——加行制 = state 加行，不落配置）。

        断言面为配置语义（键/条目行），剥注释后判定——main yaml 存在指向
        ADR 的指针注释（2026-11-19-KB 原生摄取管线改造：路由句在 mode_personal
        mode.yaml description），文档性提及不等于白名单并入。
        """
        target = _REPO_ROOT / yaml_path
        assert target.is_file(), yaml_path
        text = target.read_text(encoding="utf-8")
        config_only = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        assert "pim" not in config_only
        assert "mode_personal" not in config_only


class TestModeOrthogonality:
    """④模式正交：其他模式绑定会话 pim 仍在面（加行 = 增补非收窄）。"""

    @pytest.mark.parametrize(("label", "state"), [
        ("roleplay绑定", {"agent.id": "agentos", "execution_context": {"mode": "roleplay"}}),
        ("coding绑定", {"agent.id": "agentos", "execution_context": {"mode": "coding"}}),
        ("writing绑定", {"agent.id": "agentos", "execution_context": {"mode": "writing"}}),
    ])
    def test_pim_survives_mode_binding(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        state: dict[str, Any],
    ) -> None:
        acl, ts, _ = _load_chain(tmp_path, monkeypatch, live={"file_read", MODE_TOOL})
        merged = _run(acl.execute(_ctx(state))).state_updates
        assert MODE_TOOL in merged["tool_ids"], label
        face = _face_names(ts, merged)
        assert MODE_TOOL in face, label


class TestInheritAndUndeclaredForms:
    """⑤inherit/未声明形态不并入（K10 空面语义不反转）。"""

    @pytest.mark.parametrize(("label", "tool_ids_line", "note"), [
        ("inherit形态", "tool_ids: inherit", "继承基线全量面——凭空写入[模式工具]会反转白名单语义"),
        ("未声明形态", "display_name: 裸执行者", "未声明 = 不写键 = tool_schema K10 空面"),
    ])
    def test_non_list_form_untouched(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        tool_ids_line: str,
        note: str,
    ) -> None:
        _isolate_env(tmp_path, monkeypatch)
        agents = tmp_path / "factory" / "agents"
        (agents / "inheritor.yaml").write_text(
            f"display_name: 附身执行者\n{tool_ids_line}\n", encoding="utf-8"
        )
        acl, ts, calls = _load_chain(tmp_path, monkeypatch, live={"file_read", MODE_TOOL})
        del calls
        merged = _run(acl.execute(_ctx({"agent.id": "inheritor"}))).state_updates
        assert "tool_ids" not in merged, label
        assert "tool_ids.mode_source" not in merged, label
        # K10 链上面：tool_ids 缺键 → 工具面置空（绝不凭空出现 pim 白名单）
        face = _face_names(ts, {"agent.id": "inheritor"})
        assert face == set(), f"{label}: {note}"


class TestDisabledPackageNotOnFace:
    """⑥禁用包不进面（探针活注册表交集；G2 遮挡/未注册同解）。"""

    @pytest.mark.parametrize(("label", "live"), [
        ("包被禁用", {"file_read"}),
        ("G2遮挡pim", set()),
    ])
    def test_disabled_package_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        live: set[str],
    ) -> None:
        acl, ts, _ = _load_chain(tmp_path, monkeypatch, live=live)
        merged = _run(acl.execute(_ctx({"agent.id": "agentos"}))).state_updates
        assert merged["tool_ids"] == ["file_read"], label
        assert "tool_ids.mode_source" not in merged, label
        assert MODE_TOOL not in _face_names(ts, merged), label


class TestAuditMarkerExport:
    """⑦标记键出口断言（审查 R9：漏声明 = 审计面出口断链）。"""

    @pytest.mark.parametrize(("label", "state"), [
        ("主聊天", NON_TASK_SOURCES[0][1]),
        ("通道入站桥", NON_TASK_SOURCES[2][1]),
    ])
    def test_marker_key_shape_and_export(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        label: str,
        state: dict[str, Any],
    ) -> None:
        acl, _, _ = _load_chain(tmp_path, monkeypatch, live={"file_read", MODE_TOOL})
        merged = _run(acl.execute(_ctx(state))).state_updates
        # 独立 state 键（不内嵌 tool_ids 列表）+ 映射形态（并入 id → 包 id）
        assert isinstance(merged["tool_ids.mode_source"], dict), label
        assert merged["tool_ids.mode_source"] == {MODE_TOOL: MODE_PKG}, label
        # tool_ids 列表纯字符串（内核 as_str 过滤语义兼容，非字符串条目静默丢弃）
        assert all(isinstance(t, str) for t in merged["tool_ids"]), label

    def test_export_fields_declares_marker(self) -> None:
        """⑦manifest export_fields 声明标记键（routes.rs 出口测试同口径：
        任务域/键由写入方插件声明后出口）。"""
        import json

        manifest = json.loads(
            (_SHARED / "agent_config_load" / "plugin.json").read_text(encoding="utf-8")
        )
        assert "tool_ids.mode_source" in manifest["export_fields"]

    def test_declaration_face_single_source(self) -> None:
        """⑦声明面单源：出厂真实 mode_personal 声明 pim（并入源）且
        collect_mode_tool_ids 聚合产出一致（双根聚合与包声明零漂移）。"""
        declared = mode_keys.collect_mode_tool_ids()
        assert declared.get(MODE_TOOL) == MODE_PKG
