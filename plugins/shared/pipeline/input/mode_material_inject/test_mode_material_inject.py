# @feature: FP-0.2.二 模式物料独立管道步骤（职责终局三件，设计 D10） | @ci: python-coverage
"""mode_material_inject 通用模式物料步骤行为契约（职责终局三件）。

三件 = mode 观测回写 + persona 接管 + 组装器物料 system prompt 尾追加
（2026-09-28 设计 D10：通用模式段与 tool_ids 模式级收窄退役，模式知识
唯一注入面 = prompt_build 的 {{mode_catalog}} 目录）。本文件锁：

1. **激活判定（优先序，设计 D6）**：显式 mode > agent 归属派生 > 空；
   空 = 零动作直通（不写任何键）；显式形态非法 = warning 直通；
2. **mode 键回写**：解析成功即回写（值 = resolve_mode 归一形态），逐轮覆盖，
   物料降级不回滚；无模式/形态非法不写键；
3. **persona 接管**：mode.yaml persona 声明 + 携带文本 →
   state.context.persona_text；无声明/无文本 = 零接管；
4. **模式包组装器约定（架构核心）**：模式包 material.py::
   build_injection(state, pkg_dir) -> str——真实名 mode_<mode>.material 动态
   加载（sys.modules 防双实例），非空追加；无声明 = 合法零追加；
   抛异常/缺出口 = warning 一次 + 跳过。

mode 键区分度输入 ≥2：utdemo 与 utplain 两组包目录内容互异，注入结果随之
不同。

[来源: docs/working/模式包工作模式设计_20260928.md D10/D7/D3]
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


def _run(state: dict[str, Any], config: dict[str, Any] | None = None) -> dict[str, Any]:
    p = mode_material_inject_mod.ModeMaterialInjectPlugin(config=config or {})
    return asyncio.run(p.execute(_ctx(state))).state_updates


def _state(
    mode: str | None = "utdemo",
    prompt: str = "基线提示词",
    **extra: Any,
) -> dict[str, Any]:
    """构造激活态 state：context_build 已落库的 system_prompt 基座。"""
    state: dict[str, Any] = {"context.system_prompt": prompt}
    if mode is not None:
        state["execution_context"] = {"mode": mode}
    state.update(extra)
    return state


def _merged_prompt(state: dict[str, Any], updates: dict[str, Any]) -> str:
    """引擎合并语义下的最终 system_prompt（updates 覆盖 state，缺键回落）。"""
    return str({**state, **updates}.get("context.system_prompt", "") or "")


def _write_mode_package(
    modes_dir: Path,
    mode: str,
    *,
    material_py: str | None = None,
    persona_yaml: str | None = None,
) -> Path:
    """造一个用户副本模式包（plugin.json 为包标记；material/persona 按需）。"""
    pkg = modes_dir / f"mode_{mode}"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "plugin.json").write_text('{"id": "mode_x"}', encoding="utf-8")
    if material_py is not None:
        # 声明驱动（设计 D3）：material.py 须配 mode.yaml material 声明才会被
        # 发现加载——工厂随文件同步落声明（组装器测试默认可用）。
        (pkg / "material.py").write_text(material_py, encoding="utf-8")
        (pkg / "mode.yaml").write_text(
            chr(10).join(['mode: x', 'name: x', 'material: material.py::build_injection', '']),
            encoding='utf-8',
        )
    elif persona_yaml is not None:
        (pkg / "mode.yaml").write_text(persona_yaml, encoding="utf-8")
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
    stale = [
        k
        for k in sys.modules
        if k.startswith("mode_") and (k.endswith(".material") or "._material_" in k)
    ]
    for k in stale:
        del sys.modules[k]


# ── 1. 激活判定：mode 缺失/非法 = 直通零写入 ────────────────────────────


class TestActivationGate:
    def test_no_mode_key_is_zero_write_passthrough(self, env) -> None:
        """无 mode 键 = 直通零写入：updates 为空字典。"""
        updates = _run({"context.system_prompt": "基线", "tool_ids": ["file_read"]})

        assert updates == {}, f"未激活须零写入，实际: {updates}"

    @pytest.mark.parametrize(
        "ec", [{"mode": "Coding!"}, {"mode": 42}, "not-a-dict", {"nomode": 1}]
    )
    def test_malformed_mode_is_zero_write(self, env, ec: Any) -> None:
        """非法 mode 形态（大写/符号/非字符串/非 dict 上下文）一律不写。"""
        updates = _run(_state(mode=None, **{"execution_context": ec}))

        assert updates == {}, f"ec={ec!r}"

    def test_specialist_agent_key_also_activates(self, env) -> None:
        """main 路径限定已删除：专属 agent（模式卡键）绑定执行时同样激活。"""
        updates = _run(
            _state(tool_ids=["file_read"], **{"agent.id": "mode_utdemo/card_x"}),
        )

        assert updates["mode"] == "utdemo"
        assert "tool_ids" not in updates, "工具面不收窄不回声（单真值=agent yaml）"


# ── 2. mode 键回写（观测链出口） ────────────────────────────────────────


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
        state = _state(mode=mode)

        updates = _run(state)

        assert updates["mode"] == mode, f"顶层 mode 键须回写解析结果，实际: {updates.get('mode')!r}"
        # 性质断言：回写值与 resolve_mode 同源（回写即解析回声，非独立常量）
        assert updates["mode"] == mode_material.resolve_mode(state)

    def test_mode_value_is_resolved_normal_form(self, env) -> None:
        """回写值 = resolve_mode 归一形态（首尾空白剥离），非原文透传。"""
        updates = _run(_state(mode="  utdemo  "))

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
        updates = _run(raw_state)

        assert updates == {}, f"无模式不得写 mode 键，实际: {updates}"

    def test_rerun_with_new_mode_overwrites_value(self, env) -> None:
        """同管道重复运行：新解析值覆盖旧值（逐轮回写，不先写先赢）。"""
        state = _state(mode="utdemo")
        first = _run(state)
        assert first["mode"] == "utdemo"

        merged = {**state, **first}
        rerun_state = {**merged, "execution_context": {"mode": "utplain"}}
        second = _run(rerun_state)

        assert second["mode"] == "utplain", "重跑须按新解析值覆盖"
        assert {**merged, **second}["mode"] == "utplain", "合并后 state.mode 为新值"

    def test_key_survives_material_degradation(self, env) -> None:
        """物料注入降级（组装器抛错）不回滚回写：mode 是解析路径产物，
        面板自动弹出不得随模式包故障丢失。"""
        _write_mode_package(
            env["modes"], "utdemo", material_py="def build_injection(state, pkg_dir):\n    raise RuntimeError('boom')\n"
        )
        state = _state()

        updates = _run(state)

        assert updates["mode"] == "utdemo"
        assert _merged_prompt(state, updates) == "基线提示词", "注入降级 = 基线不动"


# ── 2b. agent 归属派生（设计 D6：显式 mode > 派生 > 空） ──────────────────


class TestAgentAttributionDerivation:
    """execution_context.mode 缺席/空时按 state["agent.id"] 归属包派生。

    真值判定 = 包目录存在性（声明面表达），非前缀字符串——防凭空键伪造模式
    （mode_ghost 键造不出 ghost 模式）。派生结果与显式 mode 同路：回写 state.mode
    观测 + 激活 persona/组装器。
    """

    def test_agent_key_derives_when_package_exists(self, env) -> None:
        """agent 键 + 包存在 → 派生 mode 并同路回写（无显式 mode）。"""
        _write_mode_package(env["modes"], "utdemo")

        updates = _run({"context.system_prompt": "基线", "agent.id": "mode_utdemo/card_x"})

        assert updates == {"mode": "utdemo"}, "派生与显式同路：回写观测、零物料"

    def test_agent_key_without_package_derives_nothing(self, env) -> None:
        """agent 键 + 包不存在 → 零派生（前缀只是候选提取，真值在包目录）。"""
        updates = _run({"context.system_prompt": "基线", "agent.id": "mode_ghost/card_x"})

        assert updates == {}, "无包 = 凭空键伪造不出模式"

    @pytest.mark.parametrize(
        "agent_id",
        [
            "l2coder",                     # 系统裸键：非模式命名空间
            "main",                        # 主 agent
            "mode_Coding/x",               # 候选形态非法（大写）
            "mode_utdemo",                 # 缺 stem 分隔
            "mode_utdemo/card/x",          # 多段
            "mode_/x",                     # 空 mode 段
        ],
    )
    def test_non_mode_or_malformed_key_derives_nothing(self, env, agent_id: str) -> None:
        """非模式键/形态非法 → 零派生（parse_mode_agent_key 同口径拒绝）。"""
        assert mode_material.derive_mode_from_agent({"agent.id": agent_id}) == ""
        assert _run({"context.system_prompt": "基线", "agent.id": agent_id}) == {}

    def test_explicit_mode_wins_over_agent_attribution(self, env) -> None:
        """优先序：显式 mode > agent 归属派生（D6——主 agent 路由决策不被覆盖）。"""
        _write_mode_package(env["modes"], "utplain")
        state = {
            "context.system_prompt": "基线",
            "agent.id": "mode_utplain/card_x",
            "execution_context": {"mode": "utdemo"},
        }

        updates = _run(state)

        assert updates["mode"] == "utdemo", "显式在场即胜出，即便 agent 归属他包"

    @pytest.mark.parametrize("ec", [{"mode": ""}, {"other": 1}, {}])
    def test_empty_or_absent_explicit_mode_falls_back_to_derivation(
        self, env, ec: Any
    ) -> None:
        """显式 mode 空/缺席（非非法形态）→ 归属派生兜底。"""
        _write_mode_package(env["modes"], "utdemo")
        state = {
            "context.system_prompt": "基线",
            "agent.id": "mode_utdemo/card_x",
            "execution_context": ec,
        }

        updates = _run(state)

        assert updates == {"mode": "utdemo"}

    def test_malformed_explicit_mode_blocks_derivation(self, env) -> None:
        """显式 mode 形态非法 → warning 直通，不回落派生（在场意图不被静默替换）。"""
        _write_mode_package(env["modes"], "utdemo")

        updates = _run({
            "context.system_prompt": "基线",
            "agent.id": "mode_utdemo/card_x",
            "execution_context": {"mode": "Coding!"},
        })

        assert updates == {}

    def test_derivation_activates_persona_and_assembler(self, env) -> None:
        """派生后与显式同路：persona 接管 + 组装器物料均按归属包激活。"""
        pkg = env["modes"] / "mode_utdemo"
        pkg.mkdir(parents=True)
        (pkg / "plugin.json").write_text('{"id": "mode_utdemo"}', encoding="utf-8")
        (pkg / "mode.yaml").write_text(
            "mode: utdemo\nname: 派生包\n"
            "material: material.py::build_injection\n"
            "persona:\n  replace: true\n  from: roleplay_persona\n",
            encoding="utf-8",
        )
        (pkg / "material.py").write_text(
            "def build_injection(state, pkg_dir):\n    return '归属包物料'\n",
            encoding="utf-8",
        )
        state = {
            "context.system_prompt": "基线",
            "agent.id": "mode_utdemo/card_x",
            "execution_context": {"roleplay_persona": "老船长人设。"},
        }

        updates = _run(state)

        assert updates["mode"] == "utdemo"
        assert updates["context.persona_text"] == "老船长人设。"
        assert "归属包物料" in str(updates.get("context.system_prompt"))

    def test_missing_agent_id_key_derives_nothing(self, env) -> None:
        """agent.id 缺席/非字符串 → 零派生（自动模式/聊天直连语义不变）。"""
        assert mode_material.derive_mode_from_agent({}) == ""
        assert mode_material.derive_mode_from_agent({"agent.id": None}) == ""
        assert mode_material.derive_mode_from_agent({"agent.id": 42}) == ""

    def test_derivation_uses_dual_root_package_resolution(self, env) -> None:
        """派生的包解析走双根：用户副本赢（mode_keys 同语义），出厂回落可用。"""
        # 用户副本不存在时，出厂种子包（mode_coding）可派生
        assert mode_material.derive_mode_from_agent(
            {"agent.id": "mode_coding/code_writer"}
        ) == "coding"


# ── 3. 包目录取数纯函数 ─────────────────────────────────────────────────


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


# ── 4. 模式包组装器约定（架构核心） ────────────────────────────────────


class TestPackageMaterialAssembler:
    """模式包 material.py::build_injection(state, pkg_dir) -> str 通用约定。"""

    _MARKER = "【假模式物料标记】"

    def _material_src(self, body: str) -> str:
        return f"def build_injection(state, pkg_dir):\n{body}\n"

    def test_material_appended_after_baseline(self, env) -> None:
        """带 material.py：标记文本以空行追加在基线提示词之后（追加非替换）。"""
        material_py = (
            "def build_injection(state, pkg_dir):\n"
            f'    return "{self._MARKER}:" + state["execution_context"]["mode"]\n'
        )
        _write_mode_package(env["modes"], "utdemo", material_py=material_py)

        updates = _run(_state())

        prompt = _merged_prompt(_state(), updates)
        assert prompt.startswith("基线提示词"), "基线提示词必须保留（追加非替换）"
        assert f"{self._MARKER}:utdemo" in prompt, "组装器收到 state（可自行取用）"
        assert "\n\n【假模式物料标记】" in prompt, "以空行衔接"

    def test_material_discriminates_two_packages(self, env) -> None:
        """区分度第二组：不同包的组装器产出互异，注入随之不同。"""
        _write_mode_package(
            env["modes"], "utdemo", material_py=self._material_src('    return "物料A"\n')
        )
        _write_mode_package(
            env["modes"], "utplain", material_py=self._material_src('    return "物料B"\n')
        )

        demo_prompt = _merged_prompt(_state(mode="utdemo"), _run(_state(mode="utdemo")))
        plain_prompt = _merged_prompt(_state(mode="utplain"), _run(_state(mode="utplain")))

        assert "物料A" in demo_prompt and "物料B" not in demo_prompt
        assert "物料B" in plain_prompt and "物料A" not in plain_prompt

    def test_material_empty_return_no_append(self, env) -> None:
        """组装器返回空串/纯空白 = 零追加（无则空串契约）。"""
        _write_mode_package(
            env["modes"], "utdemo", material_py=self._material_src("    return '  '\n")
        )

        updates = _run(_state())

        prompt = _merged_prompt(_state(), updates)
        assert self._MARKER not in prompt
        assert prompt == "基线提示词", "零追加 = 基线原样"

    def test_material_raises_degrades_with_warning(self, env, caplog) -> None:
        """组装器抛异常 = warning + 跳过不阻断：mode 回写不受牵连。"""
        _write_mode_package(
            env["modes"],
            "utdemo",
            material_py=self._material_src("    raise RuntimeError('boom')\n"),
        )

        with caplog.at_level("WARNING"):
            updates = _run(_state())

        assert updates["mode"] == "utdemo", "不阻断、回写不回滚"
        assert "组装失败" in caplog.text, "降级留 warning"

    def test_material_missing_decl_is_legal_silence(self, env) -> None:
        """无 material.py = 包未带组装器（合法形态）：零追加、零告警路径。"""
        _write_mode_package(env["modes"], "utdemo")

        updates = _run(_state())

        assert updates == {"mode": "utdemo"}, "零追加，仅观测回写"

    def test_material_file_without_decl_is_not_loaded(self, env, caplog) -> None:
        """声明驱动负例：包内有 material.py 但 mode.yaml 无 material 声明 →
        不按约定名回退发现，零注入（设计 D3）。"""
        _write_mode_package(env["modes"], "utdecl")
        (env["modes"] / "mode_utdecl" / "material.py").write_text(
            "def build_injection(state, pkg_dir):\n    return '孤儿标记'\n",
            encoding="utf-8",
        )

        with caplog.at_level("WARNING"):
            updates = _run(_state(mode="utdecl"))

        assert "孤儿标记" not in str(updates), "无声明不发现"

    def test_material_decl_without_file_warns(self, env, caplog) -> None:
        """声明了 material 但文件缺失 = 声明与实物不符：warning + 零追加。"""
        _write_mode_package(env["modes"], "utghost")
        (env["modes"] / "mode_utghost" / "mode.yaml").write_text(
            "mode: utghost\nname: 幽灵\nmaterial: material.py::build_injection\n",
            encoding="utf-8",
        )

        with caplog.at_level("WARNING"):
            updates = _run(_state(mode="utghost"))

        assert "声明文件缺失" in caplog.text

    def test_material_missing_function_warns_once(self, env, caplog) -> None:
        """material.py 缺 build_injection 出口 = warning + 跳过。"""
        _write_mode_package(
            env["modes"],
            "utdemo",
            material_py="def other_name(state, pkg_dir):\n    return 'x'\n",
        )

        with caplog.at_level("WARNING"):
            updates = _run(_state())

        assert "缺声明出口 build_injection" in caplog.text

    def test_material_broken_module_warns_and_recovers_next_round(
        self, env, caplog
    ) -> None:
        """material.py 语法坏 = warning + 跳过；坏模块不驻留 sys.modules（下轮可重试）。"""
        _write_mode_package(env["modes"], "utdemo", material_py="def broken(:\n")

        with caplog.at_level("WARNING"):
            first = _run(_state())
        assert "加载失败" in caplog.text
        assert "mode_utdemo._material_material_py" not in sys.modules, "坏模块不驻留"

        second = _run(_state())
        assert first["mode"] == second["mode"] == "utdemo", "不阻断，可重复执行"

    def test_material_module_registered_by_decl_derived_name(self, env) -> None:
        """组装器按声明派生名 mode_<mode>._material_<file> 注册 sys.modules（防双实例）。"""
        _write_mode_package(
            env["modes"],
            "utassembler",
            material_py=self._material_src("    return '标记'\n"),
        )
        sys.modules.pop("mode_utassembler._material_material_py", None)
        try:
            _run(_state(mode="utassembler"))
            mod = sys.modules.get("mode_utassembler._material_material_py")
            assert mod is not None, "须按声明派生名注册"
            assert callable(mod.build_injection)
        finally:
            sys.modules.pop("mode_utassembler._material_material_py", None)

    def test_material_missing_package_dir_is_silence(self, env) -> None:
        """包目录不存在（裸模式）= 无处找组装器，静默零追加。"""
        updates = _run(_state(mode="utplain"))

        assert updates == {"mode": "utplain"}


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

        updates = _run(_state())

        assert expected in _merged_prompt(_state(), updates)
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

        updates = _run(_state())

        assert "None|None" in _merged_prompt(_state(), updates), "降级不阻断组装器"

    def test_unresolvable_user_root_passes_none(self, env, monkeypatch) -> None:
        """user_config_dir()=None（极端环境无 OS 数据目录）→ None 注入。"""
        import types

        import user_space as real_space

        empty = types.ModuleType("user_space")
        empty.user_plugins_dir = real_space.user_plugins_dir
        empty.user_config_dir = lambda: None
        monkeypatch.setitem(sys.modules, "user_space", empty)
        _write_mode_package(env["modes"], "utdemo", material_py=self._DIRS_SRC)

        updates = _run(_state())

        assert "None|None" in _merged_prompt(_state(), updates)


# ── 5. server.py 服务接缝（执行适配） ──────────────────────────────────


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


class TestServerSeam:
    def test_no_profile_fetch_channel_remains(self) -> None:
        """职责终局契约：server 不持模式 profile 取数通道（{{mode_catalog}}
        目录注入归 prompt_build，工具面归 agent yaml）。"""
        server = _load_server()
        assert not hasattr(server, "fetch_mode_profile"), (
            "模式 profile 取数通道已随批 I 退役，不得残留"
        )

    def test_singleton_rebuilds_after_unload(self) -> None:
        """单例纯配置构造；on_unload 复位缓存后重建新实例。"""
        server = _load_server()
        inst = server.get_instance()
        assert isinstance(inst, server.ModeMaterialInjectPlugin)
        assert server.get_instance() is inst, "lru_cache 幂等"

        asyncio.run(server._on_unload({}))
        second = server.get_instance()
        assert second is not inst, "on_unload 复位后应重建单例"

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


# ── 6. 人设接管（通用机制：mode.yaml persona 声明，设计 D7） ──────────────


class TestPersonaTakeover:
    def test_declared_takeover_resolves_carried_text(self, env) -> None:
        """persona.replace=true + execution_context[from] 有文本 → 返回文本。"""
        pkg = env["modes"] / "mode_utpers"
        pkg.mkdir(parents=True)
        (pkg / "plugin.json").write_text('{"id": "mode_utpers"}', encoding="utf-8")
        (pkg / "mode.yaml").write_text(
            "mode: utpers\nname: 人设接管\npersona:\n  replace: true\n  from: roleplay_persona\n",
            encoding="utf-8",
        )
        state = {"execution_context": {"roleplay_persona": "  一位沉默寡言的老船长。  "}}

        text = mode_material.resolve_persona_takeover(pkg, state)

        assert text == "一位沉默寡言的老船长。", "文本返回且去首尾空白"

    def test_no_decl_or_replace_false_returns_empty(self, env) -> None:
        """无 persona 声明 / replace 非 true → 空串（零接管）。"""
        pkg = env["modes"] / "mode_utnop"
        pkg.mkdir(parents=True)
        (pkg / "mode.yaml").write_text(
            "mode: utnop\nname: 无接管\n", encoding="utf-8"
        )
        state = {"execution_context": {"roleplay_persona": "文本"}}
        assert mode_material.resolve_persona_takeover(pkg, state) == ""
        (pkg / "mode.yaml").write_text(
            "mode: utnop\nname: 显式关闭\npersona:\n  replace: false\n  from: k\n",
            encoding="utf-8",
        )
        assert mode_material.resolve_persona_takeover(pkg, state) == ""

    def test_from_key_without_text_returns_empty(self, env) -> None:
        """声明接管但 from 键无文本（缺失/空白/非字符串）→ 空串。"""
        pkg = env["modes"] / "mode_utempty"
        pkg.mkdir(parents=True)
        (pkg / "mode.yaml").write_text(
            "mode: utempty\nname: 空文本\npersona:\n  replace: true\n  from: roleplay_persona\n",
            encoding="utf-8",
        )
        assert mode_material.resolve_persona_takeover(pkg, {"execution_context": {}}) == ""
        assert mode_material.resolve_persona_takeover(
            pkg, {"execution_context": {"roleplay_persona": "   "}}
        ) == ""
        assert mode_material.resolve_persona_takeover(pkg, {}) == ""

    def test_missing_pkg_or_broken_yaml_returns_empty(self, env) -> None:
        """包目录缺失 / mode.yaml 不可读 → 空串（降级不阻断）。"""
        assert mode_material.resolve_persona_takeover(None, {}) == ""
        assert mode_material.resolve_persona_takeover(env["modes"] / "mode_ghost", {}) == ""
        pkg = env["modes"] / "mode_utbad"
        pkg.mkdir(parents=True)
        (pkg / "mode.yaml").write_text("{a: [unclosed", encoding="utf-8")
        assert mode_material.resolve_persona_takeover(pkg, {}) == ""

    def test_plugin_writes_context_persona_text(self, env) -> None:
        """端到端：声明+文本在场 → 插件写 state.context.persona_text。"""
        pkg = env["modes"] / "mode_utpers"
        pkg.mkdir(parents=True)
        (pkg / "plugin.json").write_text('{"id": "mode_utpers"}', encoding="utf-8")
        (pkg / "mode.yaml").write_text(
            "mode: utpers\nname: 人设接管\npersona:\n  replace: true\n  from: roleplay_persona\n",
            encoding="utf-8",
        )
        state = _state(mode="utpers")
        state["execution_context"]["roleplay_persona"] = "老船长人设。"

        updates = _run(state)

        assert updates["context.persona_text"] == "老船长人设。"
        assert updates["mode"] == "utpers"

    def test_plugin_without_decl_writes_no_persona_key(self, env) -> None:
        """负态端到端：无 persona 声明（或 from 无文本）→ 不写
        context.persona_text（骨架缺省 persona 走 {{persona:}} 文件回落）。"""
        _write_mode_package(env["modes"], "utdemo")
        state = _state()
        state["execution_context"]["roleplay_persona"] = "无人设接管声明的文本"

        updates = _run(state)

        assert "context.persona_text" not in updates
