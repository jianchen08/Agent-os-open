# @feature: FP-0.2.二 模式物料独立管道步骤（2026-09-24 架构重构） | @ci: python-coverage
"""mode_material_inject 通用模式物料步骤行为契约。

自 context_build 抽出（context_build 回归纯上下文构建），本文件锁：

1. **激活判定**：mode 缺失/形态非法 = 零动作直通（不写任何键、服务零调用）；
2. **基础模式段**：带 mode + profile 取数 → 模式段注入 context.system_prompt
   （编排清单键 mode_X/<stem> + task_kinds、调度链/执行者池、口径段），
   tool_ids 按 material_scope.tool_ids 收窄（仅当 state 基线存在，只收窄不扩权）；
   专属 agent（卡键等）同样注入（main 路径限定已删除）；
3. **服务失败降级**：mode.get_profile 抛错/通道未接线/信封无效 → 基础段不注入
   不阻断，mode 回写不回滚；
4. **模式包组装器约定（架构核心）**：模式包 material.py::
   build_injection(state, pkg_dir) -> str——真实名 mode_<mode>.material 动态
   加载（sys.modules 防双实例），非空追加；无 material.py = 合法零追加；
   抛异常/缺出口 = warning 一次 + 跳过；
5. **mode 键回写**：解析成功即回写（值 = resolve_mode 归一形态），逐轮覆盖；
   无模式/形态非法不写键。

mode 键区分度输入 ≥2：utdemo（全物料模式）与 utplain（裸模式）两组 profile
+ 包目录内容互异，注入结果随之不同。

[来源: docs/working/模式体系落地设计_20260915.md §3.3②/§4.1/§4.2；
服务通道先例 plugins/shared/system/eval_harness/server.py::_fetch_mode_profile]
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[2]  # plugins/shared/

# 本插件目录 + 共享根置前，并逐出同名裸模块（tests/_pipeline_plugin_path
# 同款防线：同进程其他插件目录的测试可能已缓存裸名 plugin/mode_material）。
for _d in (_DIR, _SHARED):
    if str(_d) in sys.path:
        sys.path.remove(str(_d))
sys.path.insert(0, str(_DIR))
sys.path.insert(1, str(_SHARED))
for _m in ("plugin", "mode_material"):
    sys.modules.pop(_m, None)

import mode_material  # noqa: E402
import plugin as mode_material_inject_mod  # noqa: E402


def _ctx(state: dict[str, Any]):
    from pipeline.plugin import PluginContext

    return PluginContext(state=state, config={})


class FakeProfileService:
    """mode.get_profile 假服务（外部依赖 mock）：记录调用，可编程信封/异常。"""

    def __init__(
        self,
        profiles: dict[str, dict[str, Any]] | None = None,
        error: Exception | None = None,
        envelope: bool = False,
    ) -> None:
        self.profiles = profiles or {}
        self.error = error
        self.envelope = envelope
        self.calls: list[str] = []

    async def __call__(self, mode: str) -> Any:
        self.calls.append(mode)
        if self.error is not None:
            raise self.error
        profile = self.profiles[mode]
        return {"data": profile} if self.envelope else profile


def _run(
    service: Any, state: dict[str, Any], config: dict[str, Any] | None = None
) -> dict[str, Any]:
    p = mode_material_inject_mod.ModeMaterialInjectPlugin(
        config=config or {}, profile_fetcher=service
    )
    return asyncio.run(p.execute(_ctx(state))).state_updates


def _state(
    mode: str | None = "utdemo",
    prompt: str = "基线提示词",
    tool_ids: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """构造激活态 state：context_build 已落库的 system_prompt/tool_ids 基座。"""
    state: dict[str, Any] = {"context.system_prompt": prompt}
    if mode is not None:
        state["execution_context"] = {"mode": mode}
    if tool_ids is not None:
        state["tool_ids"] = tool_ids
    state.update(extra)
    return state


def _merged_prompt(state: dict[str, Any], updates: dict[str, Any]) -> str:
    """引擎合并语义下的最终 system_prompt（updates 覆盖 state，缺键回落）。"""
    return str({**state, **updates}.get("context.system_prompt", "") or "")


def _write_mode_package(
    modes_dir: Path,
    mode: str,
    *,
    pipelines: dict[str, str] | None = None,
    rules: dict[str, str] | None = None,
    material_py: str | None = None,
) -> Path:
    """造一个用户副本模式包（plugin.json 为包标记；pipelines/rules/material 按需）。"""
    pkg = modes_dir / f"mode_{mode}"
    (pkg / "pipelines").mkdir(parents=True, exist_ok=True)
    (pkg / "rules").mkdir(parents=True, exist_ok=True)
    (pkg / "plugin.json").write_text('{"id": "mode_x"}', encoding="utf-8")
    for stem, text in (pipelines or {}).items():
        (pkg / "pipelines" / f"{stem}.yaml").write_text(text, encoding="utf-8")
    for name, text in (rules or {}).items():
        (pkg / "rules" / name).write_text(text, encoding="utf-8")
    if material_py is not None:
        (pkg / "material.py").write_text(material_py, encoding="utf-8")
    return pkg


@pytest.fixture
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Path]]:
    """隔离根：用户根/配置根钉 tmp；返回 modes 目录坐标。

    会话级 sys.modules 组装器缓存（mode_*.material，防双实例契约）逐测试清理，
    防上一用例的假包物料泄漏进下一用例。
    """
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path))
    yield {"root": tmp_path, "modes": tmp_path / "plugins" / "modes"}
    stale = [k for k in sys.modules if k.startswith("mode_") and k.endswith(".material")]
    for k in stale:
        del sys.modules[k]


_UTDEMO_PROFILE: dict[str, Any] = {
    "mode": "utdemo",
    "name": "演示模式",
    "chain": {
        "entry": "main",
        "expected_path": ["main", "orchestrator/demo_orchestrator"],
        "executor_pool": ["executor/demo", "executor/general_agent"],
    },
    "material_scope": {"tool_ids": ["file_read", "file_write", "task_submit"]},
}

_UTPLAIN_PROFILE: dict[str, Any] = {
    "mode": "utplain",
    "name": "裸模式",
    "chain": {"entry": "main", "expected_path": ["main"]},
}


# ── 1. 激活判定：mode 缺失/非法 = 直通零写入 ────────────────────────────


class TestActivationGate:
    def test_no_mode_key_is_zero_write_passthrough(self, env) -> None:
        """无 mode 键 = 直通零写入：updates 为空字典，服务零调用。"""
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, {"context.system_prompt": "基线", "tool_ids": ["file_read"]})

        assert updates == {}, f"未激活须零写入，实际: {updates}"
        assert service.calls == []

    @pytest.mark.parametrize(
        "ec", [{"mode": "Coding!"}, {"mode": 42}, "not-a-dict", {"nomode": 1}]
    )
    def test_malformed_mode_is_zero_write(self, env, ec: Any) -> None:
        """非法 mode 形态（大写/符号/非字符串/非 dict 上下文）一律不写不取数。"""
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, _state(mode=None, **{"execution_context": ec}))

        assert updates == {}, f"ec={ec!r}"
        assert service.calls == []


# ── 2. 基础模式段注入 + 工具面收窄 ──────────────────────────────────────


class TestBaseSectionInjection:
    def test_full_material_injects_and_narrows(self, env) -> None:
        """带 mode + 全物料包：模式段三件齐活，tool_ids 收窄为基线∩模式（保基线序）。"""
        _write_mode_package(
            env["modes"],
            "utdemo",
            pipelines={"fix_flow": "task_kinds: [缺陷修复, 测试修复]\nloop_bodies: []\n"},
            rules={"口径.md": "演示口径：一切输出带验证证据。"},
        )
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE}, envelope=True)
        baseline = ["file_read", "web_search", "task_submit", "file_write"]

        updates = _run(service, _state(tool_ids=baseline))

        prompt = updates["context.system_prompt"]
        assert "## 模式物料（mode=utdemo）" in prompt
        assert prompt.startswith("基线提示词"), "基线提示词必须保留（追加非替换）"
        assert "`mode_utdemo/fix_flow`" in prompt, "编排清单用约定键 mode_X/<stem>"
        assert "缺陷修复" in prompt, "文件头 task_kinds 进路由指引"
        assert "演示口径：一切输出带验证证据。" in prompt, "包内 rules 口径段注入"
        assert "main → orchestrator/demo_orchestrator" in prompt, "调度链（偏好非强制）"
        assert "executor/demo" in prompt, "执行者池进路由指引"
        assert "模式工具面收窄" in prompt
        narrowed = updates["tool_ids"]
        assert narrowed == ["file_read", "task_submit", "file_write"], (
            "收窄 = 基线 ∩ 模式声明，保基线序"
        )
        mode_ids = set(_UTDEMO_PROFILE["material_scope"]["tool_ids"])
        # 性质断言：收窄结果是基线与模式声明的公共子集（只收窄不扩权）
        assert set(narrowed) <= set(baseline), "收窄结果 ⊆ 基线"
        assert set(narrowed) <= mode_ids, "收窄结果 ⊆ 模式声明"
        assert service.calls == ["utdemo"]

    def test_bare_mode_placeholders_and_no_narrowing(self, env) -> None:
        """裸模式（无包目录 + 未细化 tool_ids）：占位注明 + 基线工具面不动。"""
        service = FakeProfileService({"utplain": _UTPLAIN_PROFILE})
        state = _state(mode="utplain", tool_ids=["file_read", "web_search"])

        updates = _run(service, state)

        prompt = _merged_prompt(state, updates)
        assert "## 模式物料（mode=utplain）" in prompt
        assert "暂未携带专属编排" in prompt, "无包目录 = 编排清单空，注明走共享编排"
        assert "暂未携带口径规则" in prompt, "无 rules = 口径段占位注明"
        assert "未细化 tool_ids" in prompt, "material_scope 未细化 = 如实注明不收窄"
        assert "tool_ids" not in updates, "不收窄 = 基线键不动（不回声）"

    def test_specialist_agent_also_injected(self, env) -> None:
        """main 路径限定已删除：专属 agent（模式卡键）绑定执行时同样注入。"""
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(
            service,
            _state(tool_ids=["file_read"], agent_id="mode_utdemo/card_x"),
        )

        assert "## 模式物料（mode=utdemo）" in updates["context.system_prompt"]
        assert updates["mode"] == "utdemo"


class TestDegradation:
    def test_profile_service_failure_degrades_without_blocking(self, env) -> None:
        """mode.get_profile 抛错 = 基础段降级不注入：不阻断，mode 回写不回滚。"""
        service = FakeProfileService(error=RuntimeError("mode sidecar down"))
        state = _state(tool_ids=["file_read"])

        updates = _run(service, state)

        assert "模式物料" not in _merged_prompt(state, updates)
        assert updates["mode"] == "utdemo", "回写是解析路径产物，不随注入成败翻转"

    def test_unwired_fetcher_degrades(self, env) -> None:
        """取数通道未接线（构造未注入 fetcher）= 同样降级不注入。"""
        state = _state()
        p = mode_material_inject_mod.ModeMaterialInjectPlugin(config={})
        updates = asyncio.run(p.execute(_ctx(state))).state_updates
        assert "模式物料" not in _merged_prompt(state, updates)
        assert updates["mode"] == "utdemo"

    def test_invalid_profile_envelope_degrades(self, env) -> None:
        """服务返回无效信封（缺 mode 键）= ValueError → 降级不注入。"""
        service = FakeProfileService({"utdemo": {"unexpected": 1}})
        state = _state(tool_ids=["file_read"])

        updates = _run(service, state)

        assert "模式物料" not in _merged_prompt(state, updates)
        assert "tool_ids" not in updates


class TestNarrowingBoundaries:
    def test_no_baseline_tool_ids_never_expands(self, env) -> None:
        """基线缺 tool_ids（含 inherit 继承全量面）：模式声明不套用，不写 tool_ids 键。"""
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, _state())  # state 无 tool_ids 键

        assert "tool_ids" not in updates, "不得把模式清单当基线写入（只收窄不扩权）"
        assert "不套用" in updates["context.system_prompt"]

    def test_disjoint_intersection_writes_explicit_empty(self, env) -> None:
        """交集为空 = 显式空表（键存在即写，下游按零工具消费）。"""
        profile = {
            "mode": "utdemo",
            "name": "演示模式",
            "material_scope": {"tool_ids": ["memory_search"]},
        }
        service = FakeProfileService({"utdemo": profile})

        updates = _run(service, _state(tool_ids=["file_read"]))

        assert updates["tool_ids"] == []

    def test_tool_scope_non_list_tool_ids_treated_as_unrefined(self) -> None:
        """material_scope.tool_ids 形态不符（非列表）= 视同未细化，不收窄。"""
        profile = {"material_scope": {"tool_ids": "file_read"}}
        note, narrowed = mode_material.tool_surface_note(profile, ["file_read"])
        assert narrowed is None
        assert "未细化" in note


# ── 3. 包目录取数纯函数 ────────────────────────────────────────────────


class TestPackageMaterialSources:
    def test_find_package_dir_dual_root(self, env) -> None:
        """用户副本优先；用户缺失回落出厂种子；两侧皆无 = None。"""
        user_pkg = _write_mode_package(env["modes"], "utdemo")
        assert mode_material.find_package_dir("utdemo") == user_pkg
        factory = mode_material.find_package_dir("coding")  # 出厂种子 mode_coding
        assert factory is not None, "用户缺失应回落出厂种子"
        assert factory.name == "mode_coding"
        assert mode_material.find_package_dir("utabsent") is None

    def test_find_package_dir_user_layer_unavailable_falls_to_factory(
        self, env, monkeypatch
    ) -> None:
        """用户插件层解析失败（user_space 不可得）→ 仅出厂根，不抛出。"""
        import types

        def _boom() -> None:
            raise RuntimeError("user layer down")

        broken = types.ModuleType("user_space")
        broken.user_plugins_dir = _boom
        monkeypatch.setitem(sys.modules, "user_space", broken)
        pkg = mode_material.find_package_dir("coding")
        assert pkg is not None, "用户层不可得应仍能回落出厂种子"
        assert pkg.name == "mode_coding"

    def test_bare_package_without_convention_dirs(self, env) -> None:
        """包存在但无 pipelines//rules/ 约定目录：清单空、口径占位、工具面按 profile。"""
        bare_pkg = env["modes"] / "mode_utbare"
        bare_pkg.mkdir(parents=True)  # 仅 plugin.json 包标记，无约定子目录
        (bare_pkg / "plugin.json").write_text('{"id": "mode_utbare"}', encoding="utf-8")
        service = FakeProfileService(
            {"utbare": dict(_UTPLAIN_PROFILE, mode="utbare", name="素包模式")}
        )

        updates = _run(service, _state(mode="utbare", tool_ids=["file_read"]))

        prompt = updates["context.system_prompt"]
        assert "## 模式物料（mode=utbare）" in prompt
        assert "暂未携带专属编排" in prompt
        assert "暂未携带口径规则" in prompt
        assert "tool_ids" not in updates, "模式未声明收窄 → 基线键不动"

    def test_read_rules_skips_unreadable_file(self, env) -> None:
        """rules/ 下不可读项（目录冒名 .md）跳过不中断，可读项照常拼接。"""
        pkg = _write_mode_package(env["modes"], "utdemo", rules={"口径.md": "有效口径。"})
        (pkg / "rules" / "broken.md").mkdir()  # 目录冒名 .md → read_text 抛 OSError
        text = mode_material.read_rules_text(pkg)
        assert text == "有效口径。"

    def test_read_orchestrations_parses_and_skips_broken(self, env) -> None:
        """编排清单：键=mode_X/<stem>、task_kinds 缺省空表；坏文件跳过不中断。"""
        _write_mode_package(
            env["modes"],
            "utdemo",
            pipelines={
                "a_flow": "task_kinds: [调研, 报告]\nloop_bodies: []\n",
                "b_flow": "loop_bodies: []\n",
                "c_flow": "task_kinds: [截断\n",
            },
        )
        pkg = mode_material.find_package_dir("utdemo")
        orch = mode_material.read_orchestrations(pkg, "utdemo")
        assert [o["key"] for o in orch] == [
            "mode_utdemo/a_flow",
            "mode_utdemo/b_flow",
        ], "坏文件跳过，好文件按名序出清单"
        assert orch[0]["task_kinds"] == ["调研", "报告"]
        assert orch[1]["task_kinds"] == []

    def test_unwrap_mode_profile_envelope_shapes(self) -> None:
        """信封两形态都容忍（{"data": {...}} / 本体）；无效形态抛 ValueError。"""
        profile = {"mode": "utdemo", "name": "x"}
        assert mode_material.unwrap_mode_profile({"data": profile}) == profile
        assert mode_material.unwrap_mode_profile(profile) == profile
        with pytest.raises(ValueError, match="无效 profile"):
            mode_material.unwrap_mode_profile({"unexpected": 1})
        with pytest.raises(ValueError, match="无效 profile"):
            mode_material.unwrap_mode_profile("garbage")


# ── 4. 模式包组装器约定（架构核心） ────────────────────────────────────


class TestPackageMaterialAssembler:
    """模式包 material.py::build_injection(state, pkg_dir) -> str 通用约定。"""

    _MARKER = "【假模式物料标记】"

    def _material_src(self, body: str) -> str:
        return f"def build_injection(state, pkg_dir):\n{body}\n"

    def test_material_appended_after_base_section(self, env) -> None:
        """带 material.py：标记文本以空行追加，且排在基础模式段之后。"""
        material_py = (
            "def build_injection(state, pkg_dir):\n"
            f'    return "{self._MARKER}:" + state["execution_context"]["mode"]\n'
        )
        _write_mode_package(env["modes"], "utdemo", material_py=material_py)
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, _state(tool_ids=["file_read"]))

        prompt = updates["context.system_prompt"]
        assert self._MARKER in prompt, "模式包物料标记须出现在注入块"
        assert f"{self._MARKER}:utdemo" in prompt, "组装器收到 state（可自行取用）"
        base_idx = prompt.index("## 模式物料")
        marker_idx = prompt.index(self._MARKER)
        assert base_idx < marker_idx, "模式包物料追加在基础模式段之后"
        assert "\n\n【假模式物料标记】" in prompt, "以空行衔接"

    def test_material_independent_of_profile_channel_degradation(self, env) -> None:
        """profile 通道未接线（基础段降级）不拖累组装器：标记仍注入。"""
        _write_mode_package(
            env["modes"],
            "utdemo",
            material_py=self._material_src(f'    return "{self._MARKER}"\n'),
        )

        updates = _run(None, _state(tool_ids=["file_read"]))  # fetcher=None

        assert "## 模式物料" not in updates["context.system_prompt"], "基础段降级"
        assert self._MARKER in updates["context.system_prompt"], "组装器照常注入"
        assert updates["mode"] == "utdemo"

    def test_material_empty_return_no_append(self, env) -> None:
        """组装器返回空串/纯空白 = 零追加（无则空串契约）。"""
        _write_mode_package(
            env["modes"], "utdemo", material_py=self._material_src("    return '  '\n")
        )
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, _state(tool_ids=["file_read"]))

        prompt = updates["context.system_prompt"]
        assert self._MARKER not in prompt
        assert "## 模式物料" in prompt, "基础段照常"

    def test_material_raises_degrades_with_warning(self, env, caplog) -> None:
        """组装器抛异常 = warning + 跳过不阻断：mode 回写与基础段不受牵连。"""
        _write_mode_package(
            env["modes"],
            "utdemo",
            material_py=self._material_src("    raise RuntimeError('boom')\n"),
        )
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        with caplog.at_level("WARNING"):
            updates = _run(service, _state(tool_ids=["file_read"]))

        assert self._MARKER not in updates["context.system_prompt"]
        assert "## 模式物料" in updates["context.system_prompt"], "基础段不受伤"
        assert updates["mode"] == "utdemo", "不阻断、回写不回滚"
        assert "组装失败" in caplog.text, "降级留 warning"

    def test_material_missing_file_is_legal_silence(self, env) -> None:
        """无 material.py = 包未带组装器（合法形态）：零追加、零告警路径。"""
        _write_mode_package(env["modes"], "utdemo")
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, _state(tool_ids=["file_read"]))

        assert "## 模式物料" in updates["context.system_prompt"]

    def test_material_missing_function_warns_once(self, env, caplog) -> None:
        """material.py 缺 build_injection 出口 = warning + 跳过。"""
        _write_mode_package(
            env["modes"],
            "utdemo",
            material_py="def other_name(state, pkg_dir):\n    return 'x'\n",
        )
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        with caplog.at_level("WARNING"):
            updates = _run(service, _state(tool_ids=["file_read"]))

        assert "## 模式物料" in updates["context.system_prompt"]
        assert "缺 build_injection" in caplog.text

    def test_material_broken_module_warns_and_recovers_next_round(
        self, env, caplog
    ) -> None:
        """material.py 语法坏 = warning + 跳过；坏模块不驻留 sys.modules（下轮可重试）。"""
        _write_mode_package(env["modes"], "utdemo", material_py="def broken(:\n")
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        with caplog.at_level("WARNING"):
            first = _run(service, _state(tool_ids=["file_read"]))
        assert "加载失败" in caplog.text
        assert "mode_utdemo.material" not in sys.modules, "坏模块不驻留"

        second = _run(service, _state(tool_ids=["file_read"]))
        assert first["mode"] == second["mode"] == "utdemo", "不阻断，可重复执行"

    def test_material_module_registered_by_real_name(self, env) -> None:
        """组装器按真实名 mode_<mode>.material 注册 sys.modules（防双实例）。"""
        _write_mode_package(
            env["modes"],
            "utassembler",
            material_py=self._material_src("    return '标记'\n"),
        )
        sys.modules.pop("mode_utassembler.material", None)
        try:
            _run(None, _state(mode="utassembler"))
            mod = sys.modules.get("mode_utassembler.material")
            assert mod is not None, "须按真实名注册"
            assert callable(mod.build_injection)
        finally:
            sys.modules.pop("mode_utassembler.material", None)

    def test_material_missing_package_dir_is_silence(self, env) -> None:
        """包目录不存在（裸模式）= 无处找组装器，静默零追加。"""
        service = FakeProfileService({"utplain": _UTPLAIN_PROFILE})
        updates = _run(service, _state(mode="utplain"))
        assert "暂未携带专属编排" in updates["context.system_prompt"]


# ── 4b. 用户层物料目录注选传参（用户层双根扩展点） ────────────────────────


class TestUserLayerDirPassing:
    """组装器声明 books_dir/user_agents_dir 形参时收到真实用户目录。

    两参约定出口（build_injection(state, pkg_dir)）零打扰——TestPackageMaterial
    Assembler 全组用例即其回归（传参须 TypeError 红后再绿）。
    """

    _DIRS_SRC = (
        "def build_injection(state, pkg_dir, books_dir=None, user_agents_dir=None):\n"
        "    return f'{books_dir}|{user_agents_dir}'\n"
    )

    def test_assembler_receives_user_dirs(self, env) -> None:
        """声明形参的组装器：books_dir=<user_config>/lorebooks、
        user_agents_dir=<user_config>/agents（AGENTOS_USER_ROOT → config）。"""
        _write_mode_package(env["modes"], "utdemo", material_py=self._DIRS_SRC)
        expected = f"{env['root'] / 'config' / 'lorebooks'}|{env['root'] / 'config' / 'agents'}"

        updates = _run(None, _state(tool_ids=["file_read"]))

        assert expected in updates["context.system_prompt"]
        assert updates["mode"] == "utdemo"

    def test_user_space_unavailable_passes_none(self, env, monkeypatch) -> None:
        """user_config_dir 解析失败（降级）→ None 注入（组装器侧语义 = 仅包内）。

        user_plugins_dir 保真注入：包目录发现（mode_keys）依赖它，只降级
        user_config_dir 单点。
        """
        import types

        import user_space as real_space

        def _boom():
            raise RuntimeError("user layer down")

        broken = types.ModuleType("user_space")
        broken.user_plugins_dir = real_space.user_plugins_dir
        broken.user_config_dir = _boom
        monkeypatch.setitem(sys.modules, "user_space", broken)
        _write_mode_package(env["modes"], "utdemo", material_py=self._DIRS_SRC)

        updates = _run(None, _state(tool_ids=["file_read"]))

        assert "None|None" in updates["context.system_prompt"], "降级不阻断组装器"

    def test_unresolvable_user_root_passes_none(self, env, monkeypatch) -> None:
        """user_config_dir()=None（极端环境无 OS 数据目录）→ None 注入。"""
        import types

        import user_space as real_space

        empty = types.ModuleType("user_space")
        empty.user_plugins_dir = real_space.user_plugins_dir
        empty.user_config_dir = lambda: None
        monkeypatch.setitem(sys.modules, "user_space", empty)
        _write_mode_package(env["modes"], "utdemo", material_py=self._DIRS_SRC)

        updates = _run(None, _state(tool_ids=["file_read"]))

        assert "None|None" in updates["context.system_prompt"]


# ── 5. mode 键回写（观测链出口） ────────────────────────────────────────


class TestModeStateKeyEcho:
    """state 顶层 mode 键回写。

    消费端契约 = GET /api/v1/pipelines/state 摘要的 mode 出口：前端模式面板
    自动弹出（modePanelAutoOpen）与管道视图模式徽标同源取数，无键零动作。
    回写走既有通道：input 插件 state_updates 平键合并（与 tool_ids/model_tier
    同形），出口可见性由 manifest export_fields/persistent_fields 声明。
    """

    @pytest.mark.parametrize("mode", ["utdemo", "utplain"])
    def test_explicit_mode_writes_top_level_key(self, env, mode: str) -> None:
        """显式模式 → state_updates 出现顶层 mode 键，值 = resolve_mode 结果。"""
        service = FakeProfileService({mode: dict(_UTPLAIN_PROFILE, mode=mode)})
        state = _state(mode=mode)

        updates = _run(service, state)

        assert updates["mode"] == mode, f"顶层 mode 键须回写解析结果，实际: {updates.get('mode')!r}"
        # 性质断言：回写值与 resolve_mode 同源（回写即解析回声，非独立常量）
        assert updates["mode"] == mode_material.resolve_mode(state)

    def test_mode_value_is_resolved_normal_form(self, env) -> None:
        """回写值 = resolve_mode 归一形态（首尾空白剥离），非原文透传。"""
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE}, envelope=True)

        updates = _run(service, _state(mode="  utdemo  "))

        assert updates["mode"] == "utdemo"

    @pytest.mark.parametrize(
        "raw_state",
        [
            {"context.system_prompt": "基线"},  # 无 execution_context（自动模式）
            {"context.system_prompt": "基线", "execution_context": {"other": 1}},
            {"context.system_prompt": "基线", "execution_context": {"mode": ""}},
        ],
    )
    def test_no_mode_writes_no_key(self, env, raw_state: dict[str, Any]) -> None:
        """自动/无模式 → 不写 mode 键（前端无键零动作，不造默认值）。"""
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, raw_state)

        assert updates == {}, f"无模式不得写 mode 键，实际: {updates}"

    def test_malformed_mode_writes_no_key(self, env) -> None:
        """形态非法（MODE_ID_RE 不放行）不出口：垃圾值不进观测面。"""
        service = FakeProfileService({"utdemo": _UTDEMO_PROFILE})

        updates = _run(service, {"execution_context": {"mode": "Coding!"}})

        assert updates == {}

    def test_rerun_with_new_mode_overwrites_value(self, env) -> None:
        """同管道重复运行：新解析值覆盖旧值（逐轮回写，不先写先赢）。"""
        service = FakeProfileService(
            {"utdemo": _UTDEMO_PROFILE, "utplain": _UTPLAIN_PROFILE}
        )
        state = _state(mode="utdemo")
        first = _run(service, state)
        assert first["mode"] == "utdemo"

        merged = {**state, **first}
        rerun_state = {**merged, "execution_context": {"mode": "utplain"}}
        second = _run(service, rerun_state)

        assert second["mode"] == "utplain", "重跑须按新解析值覆盖"
        assert {**merged, **second}["mode"] == "utplain", "合并后 state.mode 为新值"

    def test_key_survives_profile_fetch_degradation(self, env) -> None:
        """物料注入降级（取数失败）不回滚回写：mode 是解析路径产物，
        面板自动弹出不得随模式包故障丢失。"""
        service = FakeProfileService(error=RuntimeError("mode sidecar down"))
        state = _state()

        updates = _run(service, state)

        assert updates["mode"] == "utdemo"
        assert "模式物料" not in _merged_prompt(state, updates), "注入仍降级"


# ── 6. server.py 服务接缝（取数通道接线 + 执行适配） ───────────────────


def _load_server() -> Any:
    """以唯一裸名装载本插件 server.py（同 context_build 接缝测试先例）。"""
    mod_name = "mode_material_inject_server_seam_test"
    spec = importlib.util.spec_from_file_location(mod_name, str(_DIR / "server.py"))
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeCapabilityHandle:
    """tool-executor 句柄替身：记录 call(method, params)，回放脚本化返回。"""

    def __init__(self, result: Any = None) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append((method, params))
        return self.result


class TestServerSeam:
    def test_fetch_mode_profile_invoke_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """调用形状钉死：invoke + 显式 plugin_id + tool_name；信封原样透传。"""
        server = _load_server()
        envelope = {"data": {"mode": "utdemo", "name": "演示模式"}}
        handle = FakeCapabilityHandle(result=envelope)
        monkeypatch.setattr(server.plugin, "get_capability", lambda _name: handle)

        res = asyncio.run(server.fetch_mode_profile("utdemo"))

        assert res == envelope, "信封原样透传（解析归插件侧 unwrap_mode_profile）"
        assert handle.calls == [
            ("invoke", {"tool_name": "mode.get_profile",
                        "plugin_id": "mode_utdemo", "args": {}})
        ], "tool-executor 显式 plugin_id 通道（mode_<mode>）"

    def test_capability_absent_raises_key_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tool-executor 未注入 → KeyError 上抛（降级裁决在插件侧）。"""
        server = _load_server()

        def _raise(name: str) -> Any:
            raise KeyError(f"capability not injected: {name}")

        monkeypatch.setattr(server.plugin, "get_capability", _raise)
        with pytest.raises(KeyError):
            asyncio.run(server.fetch_mode_profile("utdemo"))

    def test_singleton_wires_fetcher_and_rebuilds_after_unload(self) -> None:
        """单例带 fetch_mode_profile 接线；on_unload 复位缓存后重建新实例。"""
        server = _load_server()
        inst = server.get_instance()
        assert isinstance(inst, server.ModeMaterialInjectPlugin)
        assert inst._profile_fetcher is server.fetch_mode_profile, (
            "get_instance 必须把 mode.get_profile 取数通道接进插件"
        )
        assert server.get_instance() is inst, "lru_cache 幂等"

        asyncio.run(server._on_unload({}))
        second = server.get_instance()
        assert second is not inst, "on_unload 复位后应重建单例"
        assert second._profile_fetcher is server.fetch_mode_profile

    def test_execute_adapter_returns_state_updates(self) -> None:
        """适配层：state 装配 → 插件 execute → 拆包扁平字典。"""
        server = _load_server()
        server.get_instance.cache_clear()

        out = asyncio.run(
            server.execute(state={"execution_context": {"mode": "utdemo"}})
        )

        assert isinstance(out, dict)
        assert isinstance(out["state_updates"], dict)
        assert out["state_updates"].get("mode") == "utdemo"

        out_idle = asyncio.run(server.execute(state={}))
        assert out_idle == {"state_updates": {}}, "未激活 = 零写入直通"
