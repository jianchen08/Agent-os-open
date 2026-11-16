# @feature: FP-0.2.〇 世界内嵌谱系右移批次三 tick/长时段演化 | @vision: V1 可进化 | @ci: python-coverage
"""tick/长时段演化——世界周期推进的声明面与直达接口（世界内嵌批次三，调研
§7.6 真缺口②）。

- 包声明面：pack yaml 顶层可选键 `tick`（{steps: [{when?, effects}]}）——
  card_lint.lint_tick 形态单源（零 IO）+ 消费侧归一剔除（when 键域=唯一执法位
  fail-closed，条件求值不经 Validator）；
- POST /game/tick（body {session_id, ticks?}）：ticks 正整数缺省 1、上限钳制
  10；会话校验/单宿主租约与 game/operation 同闸；逐 tick × 逐 step 顺序结算
  （state.adjudicate 单源，cycle 算子编排侧解析为绝对值——Validator 契约零
  改动）；无声明包空转回执 `{applied: true, ticks, advanced: false}` 不 404；
- 回执 `{applied: true, ticks, advanced, state_delta?, steps_fired?}`；
- 审计 source=tick、actor=narrator 缺省、批标记行 summary 载 ticks 数与
  fired steps——沉淀回路输入自动可见。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Validator 单源真身（state_marker_parse 包内 state_contract；测试桩经它结算，
# 与生产 state.adjudicate 服务同一实现——禁第二套）。
_SHARED = MODES_DIR  # MODES_DIR = plugins/shared（三连 dirname 惯例，同族测试一致）
_STMP_DIR = os.path.join(MODES_DIR, "state_marker_parse")
for _p in (str(_SHARED), _STMP_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)
from state_contract import validate_and_apply  # noqa: E402

_PANEL_EP = "/ext/mode_roleplay"
_TICK = f"{_PANEL_EP}/game/tick"
_CONFIG = f"{_PANEL_EP}/game/config"
_ATTACH = f"{_PANEL_EP}/game/session/attach"
_MANIFEST = Path(MODES_DIR) / "mode_roleplay" / "plugin.json"
_SDK_DOC = Path(MODES_DIR) / "mode_roleplay" / "webview" / "GAME_HOST_SDK.md"
_STARDEW_PACK = Path(MODES_DIR) / "mode_roleplay" / "packs" / "stardew_social.yaml"

pytestmark = pytest.mark.unit

_ROW_SCHEMA = {
    "world": {
        "日期": {"type": "int", "min": 1, "max": 28, "default": 1},
        "季节": {"type": "enum", "values": ["春", "夏", "秋", "冬"], "default": "春"},
        "丰饶": {"type": "int", "min": 0, "max": 100, "default": 20},
    }
}

_SEASONS = ["春", "夏", "秋", "冬"]

_PACK_DECL = {
    "name": "演化测试包",
    "variables": _ROW_SCHEMA,
    "tick": {
        "steps": [
            {"effects": {"world.日期": "+1", "world.丰饶": "-1"}},
            {
                "when": {"key": "world.日期", "op": ">=", "value": 28},
                "effects": {"world.季节": {"cycle": _SEASONS}, "world.日期": "=1"},
            },
        ]
    },
}


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_roleplay_tick_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_yaml(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, allow_unicode=True)


class _StubPlugin:
    """未 bootstrap 测试环境的 capability 存根：get_capability 恒可取。"""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def get_capability(self, ns: str) -> Any:
        return self._handle


class _FakeToolExecutor:
    """tool-executor 桩：character_state.get/update + state.adjudicate 三工具。

    state.adjudicate 分支直调真身 validate_and_apply（与生产服务同一实现）；
    账本现值内存态 + update 捕获，测试断言落账面。
    """

    def __init__(self, values: dict[str, Any], captured: list[tuple[str, str, dict]]) -> None:
        self.values = values
        self.captured = captured
        self.updates: list[dict[str, Any]] = []
        self.fail_tools: set[str] = set()

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        assert method == "invoke", "capability handle 收剥前缀方法名（tool-executor.invoke）"
        name = str(params.get("tool_name") or "")
        plugin_id = str(params.get("plugin_id") or "")
        args = params.get("args") or {}
        self.captured.append((name, plugin_id, args))
        if name in self.fail_tools:
            raise RuntimeError(f"{name} 不可用")
        if name == "character_state.get":
            return {"success": True, "data": {"card_id": args.get("card_id"), "values": dict(self.values)}}
        if name == "state.adjudicate":
            # 生产服务 = 纯委托：current 由调用方传（tick 编排传 running，随
            # accepted 逐步更新）——桩同构，不用账本自持值（同 tick 内后续步
            # 必须见前序结算结果）。
            result = validate_and_apply(
                dict(args.get("current") or {}), args.get("changes") or {},
                args.get("schema"), args.get("hard_rules"),
            )
            return {"success": True, "data": {
                "accepted": result.accepted, "rejected": result.rejected, "audit": result.audit,
            }}
        if name == "character_state.update":
            self.updates.append(dict(args))
            self.values.update(args.get("changes") or {})
            return {"success": True, "data": {"card_id": args.get("card_id"), "applied": True}}
        return {"success": False, "error": f"未知工具: {name}"}


def _audit_rows(workspace: str) -> list[dict[str, Any]]:
    path = os.path.join(workspace, "rp_state_audit.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 会话行（含 schema）+ 双根卡/包 + tool-executor 桩。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path / "user_config"))
    module = _load_server()
    module.reset_providers()
    module._reset_game_channels()
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    module._OPS_ROWS = [{
        "pipeline_id": "pipe-1",
        "thread_id": "thread-1",
        "mode": "roleplay",
        "agent_id": "mode_roleplay/card_hero",
        "workspace": str(ws),
        "context.rp_state_schema": _ROW_SCHEMA,
        "context.rp_state_hard_rules": {},
    }]

    async def _state_rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return list(module._OPS_ROWS)

    module._set_provider("pipeline-state", _state_rows)
    captured: list[tuple[str, str, dict]] = []
    te = _FakeToolExecutor({"world.日期": 1, "world.季节": "春", "world.丰饶": 20}, captured)
    module.plugin = _StubPlugin(te)
    module.ops_te = te
    module.ops_captured = captured
    _write_yaml(
        os.path.join(str(module._user_cards_dir()), "card_hero.yaml"),
        {"name": "牧場主", "pack_ids": ["tick_pack"]},
    )
    _write_yaml(os.path.join(str(module._user_packs_dir()), "tick_pack.yaml"), dict(_PACK_DECL))
    return module


def _call(
    module: Any,
    path: str,
    method: str = "GET",
    raw_body: str = "",
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query)
    )


def _post(module: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    return _call(module, path, method="POST", raw_body=json.dumps(body))


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status_of(result: dict[str, Any]) -> int:
    return int(result["data"]["status"])


def _tick_body(**override: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"session_id": "pipe-1"}
    body.update(override)
    return body


def _attach(rp: Any, session_id: str = "pipe-1") -> None:
    _post(rp, _ATTACH, {"session_id": session_id, "host_id": "factory_web"})


# ── lint_tick：声明形态单源（零 IO）───────────────────────────────────────────


def test_lint_tick_valid_declaration_clean() -> None:
    """合法声明（when 条件 + cycle 算子 + 纯 op 串混排）= 空警告。"""
    module = _load_server()
    assert module.lint_tick(_PACK_DECL["tick"]) == []


def test_lint_tick_none_and_non_dict() -> None:
    """None（键缺席 = 无周期推进面）= 空；非映射 = 单条警告。"""
    module = _load_server()
    assert module.lint_tick(None) == []
    assert len(module.lint_tick(["steps"])) == 1
    assert "须为映射" in module.lint_tick(["steps"])[0]


@pytest.mark.parametrize(
    ("decl", "fragment"),
    [
        ({}, "steps"),
        ({"steps": "两步"}, "steps"),
        ({"steps": []}, "非空"),
        ({"steps": {}, "note": "x"}, "steps"),
        ({"steps": ["一步"]}, "条目"),
        ({"steps": [{"when": {"key": "world.日期", "op": ">=", "value": 28}}]}, "effects"),
        ({"steps": [{"effects": {}}]}, "effects"),
        ({"steps": [{"effects": ["+1"]}]}, "effects"),
        ({"steps": [{"effects": {"world.日期": 1}}]}, "op 串"),
        ({"steps": [{"effects": {"world.日期": ""}}]}, "op 串"),
        ({"steps": [{"effects": {"world.季节": {"cycle": []}}}]}, "cycle"),
        ({"steps": [{"effects": {"world.季节": {"cycle": "四季"}}}]}, "cycle"),
        ({"steps": [{"effects": {"world.季节": {"cycle": ["春"], "shift": 1}}}]}, "cycle"),
        (
            {"steps": [{"when": "日期触顶", "effects": {"world.日期": "+1"}}]},
            "when",
        ),
        (
            {"steps": [{"when": {"op": ">=", "value": 28}, "effects": {"world.日期": "+1"}}]},
            "key",
        ),
        (
            {"steps": [{"when": {"key": "world.日期", "op": "~", "value": 28}, "effects": {"world.日期": "+1"}}]},
            "op",
        ),
        (
            {"steps": [{"when": {"key": "world.日期", "op": ">="}, "effects": {"world.日期": "+1"}}]},
            "value",
        ),
        (
            {"steps": [{"effects": {"world.日期": "+1"}, "unless": "x"}]},
            "未知声明键",
        ),
    ],
)
def test_lint_tick_bad_forms_reported(decl: Any, fragment: str) -> None:
    """非法形态逐组报警（steps/条目/effects 形态/cycle 非空/when 结构/未知键）。"""
    module = _load_server()
    warnings = module.lint_tick(decl)
    assert warnings, f"非法形态须报警: {decl!r}"
    assert any(fragment in w for w in warnings), (fragment, warnings)


def test_lint_tick_when_ops_domain_single_source() -> None:
    """when op 合法域与 server 侧求值共用同一常量（单源禁第二套）。"""
    module = _load_server()
    assert set(module.TICK_WHEN_OPS) == {"==", "!=", ">=", "<"}


def test_lint_tick_when_unknown_key_reported() -> None:
    """when 规格外未知键 = 警告（作者笔形可见，不剔除——归一消费面剥除）。"""
    module = _load_server()
    decl = {"steps": [{"when": {"key": "world.日期", "op": "==", "value": 1, "mode": "soft"},
                       "effects": {"world.日期": "+1"}}]}
    warnings = module.lint_tick(decl)
    assert any("未知声明键" in w for w in warnings)


# ── game/config 透出 tick 声明 + when 键域 fail-closed（消费侧唯一执法位）─────


def test_config_exposes_tick_declaration(rp: Any) -> None:
    """会话绑定包 tick 声明经 game/config 透出（归一 steps 全条目含 when/cycle）。"""
    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))
    tick = out.get("tick")
    assert isinstance(tick, dict)
    steps = tick["steps"]
    assert len(steps) == 2
    assert steps[0]["effects"] == {"world.日期": "+1", "world.丰饶": "-1"}
    assert "when" not in steps[0], "when 缺席 = 恒真（归一后键缺席）"
    assert steps[1]["when"] == {"key": "world.日期", "op": ">=", "value": 28}
    assert steps[1]["effects"]["world.季节"] == {"cycle": _SEASONS}


def test_config_tick_when_key_outside_variables_dropped(rp: Any) -> None:
    """when.key 不在本包 variables 声明内 = 整步剔除（fail-closed 键域，消费侧
    唯一执法位——条件求值不经 Validator）；合法步保留。"""
    pack = dict(_PACK_DECL)
    pack["tick"] = {
        "steps": [
            {"when": {"key": "world.无此键", "op": "==", "value": 1}, "effects": {"world.日期": "+1"}},
            {"effects": {"world.丰饶": "+1"}},
        ]
    }
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert [s.get("when") for s in out["tick"]["steps"]] == [None]
    assert out["tick"]["steps"][0]["effects"] == {"world.丰饶": "+1"}


def test_config_tick_absent_when_undeclared(rp: Any) -> None:
    """无 tick 声明 = 键缺席（键缺席式，Tier 0 兼容态零新增键）。"""
    pack = {k: v for k, v in _PACK_DECL.items() if k != "tick"}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert "tick" not in out


def test_config_tick_later_pack_wins(rp: Any) -> None:
    """声明序后者非空清单整体覆盖（operations/choices 同构：双包后者胜）。"""
    _write_yaml(
        os.path.join(str(rp._user_packs_dir()), "tick_pack_two.yaml"),
        {
            "name": "第二包",
            "variables": {"world": {"日期": {"type": "int", "min": 1, "max": 9, "default": 1}}},
            "tick": {"steps": [{"effects": {"world.日期": "+1"}}]},
        },
    )
    _write_yaml(
        os.path.join(str(rp._user_cards_dir()), "card_hero.yaml"),
        {"name": "牧場主", "pack_ids": ["tick_pack", "tick_pack_two"]},
    )

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert len(out["tick"]["steps"]) == 1
    assert out["tick"]["steps"][0]["effects"] == {"world.日期": "+1"}


def test_config_tick_card_side_not_involved(rp: Any) -> None:
    """tick 是包级声明面：卡 extensions.tick 不透出（operations/choices 同构）。"""
    _write_yaml(
        os.path.join(str(rp._user_cards_dir()), "card_hero.yaml"),
        {
            "name": "牧場主",
            "pack_ids": ["tick_pack"],
            "extensions": {"tick": {"steps": [{"effects": {"world.日期": "+9"}}]}},
        },
    )

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert len(out["tick"]["steps"]) == 2, "卡侧声明不参与聚合（仍为包侧两步）"


@pytest.mark.parametrize(
    ("tick_decl", "expect_absent", "expect_len"),
    [
        ({"steps": ["一步", {"effects": {"world.日期": "+1"}}]}, False, 1),  # 非映射条目剔除
        ({"steps": [{"when": "日期触顶", "effects": {"world.日期": "+1"}}]}, True, 0),  # when 非映射
        ({"steps": [{"when": {"key": "world.日期", "op": "==", "value": 1}}]}, True, 0),  # effects 缺席
        ({"steps": [{"effects": {"world.日期": 3}}]}, True, 0),  # 效果值形态非法整步剔除
        ("abc", True, 0),  # tick 非映射整组剔除
        ({"steps": [{"note": "x", "effects": {"world.日期": "+1"}}]}, False, 1),  # 未知键只警告不剔除
        (
            {"steps": [{"when": {"key": "world.日期", "op": ">=", "value": 28, "mode": "soft"},
                        "effects": {"world.日期": "+1"}}]},
            False, 1,  # when 规格外键剥除（归一 {key, op, value}）
        ),
    ],
)
def test_config_tick_listing_normalization_forms(
    rp: Any, tick_decl: Any, expect_absent: bool, expect_len: int
) -> None:
    """消费侧归一剔除面逐形态：坏条目/坏 when/坏效果值剔除、未知键警告保留、
    非映射整组剔除（ui.lineage 剔除回落同构）。"""
    pack = dict(_PACK_DECL)
    pack["tick"] = tick_decl
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    if expect_absent:
        assert "tick" not in out
    else:
        assert len(out["tick"]["steps"]) == expect_len
        if isinstance(tick_decl, dict) and tick_decl["steps"] and isinstance(tick_decl["steps"][-1], dict) \
                and isinstance(tick_decl["steps"][-1].get("when"), dict):
            assert set(out["tick"]["steps"][0]["when"]) == {"key", "op", "value"}, (
                "when 归一只留 {key, op, value}（规格外键剥除）"
            )


def test_tick_helpers_branch_pins(rp: Any) -> None:
    """结算纯函数组分支钉（全等总函数，输入→输出直断）：
    != 双侧 / bool 有序不可判 / 字符串有序两侧 / 基线双缺 / cycle 环绕与重启 /
    cycle 基线双缺 = 步不可结算。"""
    flat, _skipped = rp.flatten_variables(_ROW_SCHEMA)
    running = {"world.日期": 5, "world.季节": "春", "world.丰饶": 20}
    assert rp._tick_when_met({"key": "world.日期", "op": "!=", "value": 3}, running, flat) is True
    assert rp._tick_when_met({"key": "world.日期", "op": "!=", "value": 5}, running, flat) is False
    assert rp._tick_when_met({"key": "world.丰饶", "op": ">=", "value": True}, running, flat) is False, (
        "bool 不参与有序比较（不可判跳过）"
    )
    # 字符串有序比较 = 码点序（非季相序：春 U+6625 > 夏 U+590F）——确定性总函数
    assert rp._tick_when_met({"key": "world.季节", "op": ">=", "value": "夏"}, running, flat) is True
    assert rp._tick_when_met({"key": "world.季节", "op": "<", "value": "夏"}, running, flat) is False
    assert rp._tick_when_met({"key": "world.无此键", "op": "==", "value": 1}, running, flat) is False, (
        "基线双缺 = 条件不可判"
    )
    assert rp._tick_baseline({}, "world.无此键", flat) == (None, False)
    assert rp._tick_cycle_next(["春", "冬"], "夏") == "春", "现值不在值域 = 从头循环（确定性重启）"
    assert rp._tick_cycle_next(["春", "冬"], "冬") == "春", "末位环绕回首位"
    step = {"effects": {"world.无此键": {"cycle": ["甲"]}}}
    assert rp._tick_step_changes(step, {}, flat) is None, "cycle 基线双缺 = 步不可结算（不发明状态）"


# ── 通道面：会话校验/租约/ticks 形态（fail-closed 400/409）────────────────────


def test_tick_route_get_method_404(rp: Any) -> None:
    """GET 打 tick 路由 = 404（POST-only，游戏域通道族同款方法错配语义）。"""
    result = _call(rp, _TICK, method="GET", query={"session_id": "pipe-1"})

    assert _status_of(result) == 404


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"session_id": ""},
        {"session_id": "坏 id/斜杠"},
        {"session_id": "pipe-x"},
    ],
)
def test_tick_session_validation_400(rp: Any, body: dict[str, Any]) -> None:
    """会话校验组（game/operation 同闸）：缺参/形态非法/不存在或非扮演 = 400。"""
    _attach(rp, "pipe-x")  # pipe-x 有租约但无会话行（非扮演）

    result = _post(rp, _TICK, body)

    assert _status_of(result) == 400
    reason = _payload(result)["error"]
    assert reason.strip(), "拒绝必须带原因（不许静默）"


def test_tick_requires_lease_409(rp: Any) -> None:
    """未 attach 直发 = 409（单宿主租约，game/operation 同闸）。"""
    result = _post(rp, _TICK, _tick_body())

    assert _status_of(result) == 409
    assert "租约" in _payload(result)["error"]


@pytest.mark.parametrize("ticks", [0, -1, 1.5, "三", True, [3]])
def test_tick_bad_ticks_400(rp: Any, ticks: Any) -> None:
    """ticks 形态 fail-closed：须为正整数（bool 是 int 子类显式排除）。"""
    _attach(rp)

    result = _post(rp, _TICK, _tick_body(ticks=ticks))

    assert _status_of(result) == 400
    assert "ticks" in _payload(result)["error"]


def test_tick_upper_limit_clamped(rp: Any) -> None:
    """ticks 超 = 上限钳制（防打爆）：请求 25 按上限 10 结算，回执回显钳后值。"""
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body(ticks=25)))

    assert out["applied"] is True
    assert out["ticks"] == 10, "钳制到上限 10"
    adjudicates = [c for c in rp.ops_captured if c[0] == "state.adjudicate"]
    assert len(adjudicates) == 10, "10 ticks × 恒真步 1 次 = 10 次结算（请求 25 钳制真实生效，条件步 10 tick 内不触顶零调用）"


# ── 空转/无契约：通用推进接口的诚实回执面─────────────────────────────────────


def test_tick_no_declaration_idle_receipt(rp: Any) -> None:
    """无声明包 = 空转回执不 404：{applied: true, ticks, advanced: false}，
    state_delta/steps_fired 键缺席，零结算调用零审计。"""
    pack = {k: v for k, v in _PACK_DECL.items() if k != "tick"}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body(ticks=3)))

    assert out == {"applied": True, "ticks": 3, "advanced": False}
    assert rp.ops_captured == [], "空转零结算调用"
    assert _audit_rows(str(rp._OPS_ROWS[0]["workspace"])) == [], "空转零审计行"


def test_tick_declared_but_schema_absent_400(rp: Any) -> None:
    """有声明但会话行无状态契约 = 无契约可结 400（fail-closed，兼容态透传会
    绕开 clamp——operation 同款拒绝口径）。"""
    rp._OPS_ROWS[0].pop("context.rp_state_schema")
    _attach(rp)

    result = _post(rp, _TICK, _tick_body())

    assert _status_of(result) == 400
    assert "契约" in _payload(result)["error"]
    assert rp.ops_captured == [], "拒绝零结算调用"


# ── 顺序结算：单 tick / 条件跳过 / cycle 换季 / 多 tick 跨档───────────────────


def test_tick_single_settlement_state_delta_and_steps_fired(rp: Any) -> None:
    """单 tick：恒真步结算（date+1/丰饶-1），条件步不满足跳过；回执三键齐。"""
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body()))

    assert out["applied"] is True
    assert out["ticks"] == 1
    assert out["advanced"] is True
    assert out["state_delta"] == {"world.日期": 2, "world.丰饶": 19}
    assert out["steps_fired"] == [0]
    assert rp.ops_te.updates == [
        {"card_id": "card_hero", "changes": {"world.日期": 2, "world.丰饶": 19}}
    ], "全部 tick 终了一次合并落账"


def test_tick_conditional_step_fires_on_threshold(rp: Any) -> None:
    """日期触顶 28：条件步开火——季节 cycle 推进一位 + 日期重置 1（引擎结算，
    非叙述约定）。"""
    rp.ops_te.values.update({"world.日期": 28, "world.季节": "春"})
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body()))

    assert out["advanced"] is True
    assert out["steps_fired"] == [0, 1]
    assert out["state_delta"] == {"world.日期": 1, "world.季节": "夏", "world.丰饶": 19}, (
        "恒真步先结算（+1 触顶 clamp 28、丰饶-1），条件步随后换季重置"
    )


def test_tick_conditional_step_skipped_when_unsatisfied(rp: Any) -> None:
    """条件不满足 = 步跳过（非阻断）：纯条件包整轮空转 advanced=false。"""
    pack = dict(_PACK_DECL)
    pack["tick"] = {"steps": [{"when": {"key": "world.日期", "op": "<", "value": 1},
                               "effects": {"world.丰饶": "+5"}}]}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body(ticks=2)))

    assert out["applied"] is True
    assert out["advanced"] is False
    assert "state_delta" not in out
    assert "steps_fired" not in out
    assert rp.ops_te.updates == [], "跳过步零落账"


def test_tick_when_baseline_falls_back_to_schema_default(rp: Any) -> None:
    """when 基线：账本现值缺席 → schema 声明 default（声明真值在引擎）。"""
    pack = dict(_PACK_DECL)
    pack["tick"] = {"steps": [{"when": {"key": "world.丰饶", "op": "==", "value": 20},
                               "effects": {"world.日期": "+1"}}]}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)
    rp.ops_te.values.pop("world.丰饶")
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body()))

    assert out["advanced"] is True, "default=20 与条件相等 → 步开火"
    assert out["state_delta"] == {"world.日期": 2}


def test_tick_when_mixed_type_ordered_comparison_unsatisfied(rp: Any) -> None:
    """≥/< 混型比较（数值 vs 字符串）= 条件不可判跳过（全等函数不抛）。"""
    pack = dict(_PACK_DECL)
    pack["tick"] = {"steps": [{"when": {"key": "world.日期", "op": ">=", "value": "月底"},
                               "effects": {"world.日期": "=1"}}]}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body()))

    assert out["advanced"] is False
    assert rp.ops_te.updates == []


@pytest.mark.parametrize(
    ("ticks_total", "expected_season"),
    [((27,), "夏"), ((54,), "秋"), ((81,), "冬"), ((108,), "春")],
)
def test_tick_multi_tick_season_rollover_cycle(
    rp: Any, ticks_total: tuple[int], expected_season: str
) -> None:
    """多 tick 顺序结算跨季：28 天×循环——每 27 tick 触顶换季（初值日期 1），
    春→夏→秋→冬→春 枚举循环是引擎事实；整年 108 tick 回到 春/1。"""
    _attach(rp)
    remaining = ticks_total[0]
    while remaining > 0:
        batch = min(remaining, 10)
        out = _payload(_post(rp, _TICK, _tick_body(ticks=batch)))
        remaining -= batch

    assert out["advanced"] is True
    assert out["state_delta"] == {"world.日期": 1, "world.季节": expected_season, "world.丰饶": 0}
    assert rp.ops_te.values["world.季节"] == expected_season
    assert rp.ops_te.values["world.日期"] == 1


def test_tick_multi_tick_steps_fire_in_declaration_order(rp: Any) -> None:
    """steps 声明序逐条判定：同 tick 内步序可见（先 +1 后触顶判定）。"""
    _attach(rp)
    rp.ops_te.values.update({"world.日期": 26})

    out = _payload(_post(rp, _TICK, _tick_body(ticks=3)))

    # tick1: 26→27（不触顶）；tick2: 27→28 → 触顶换季 夏/1；tick3: 1→2
    assert out["state_delta"] == {"world.日期": 2, "world.季节": "夏", "world.丰饶": 17}
    assert out["steps_fired"] == [0, 0, 1, 0]


# ── 传输面失败 = 502（非规则拒绝，operation 同口径）───────────────────────────


def test_tick_state_get_down_502(rp: Any) -> None:
    """现值读取服务不可用（character_state.get 抛出）= 502。"""
    rp.ops_te.fail_tools.add("character_state.get")
    _attach(rp)

    result = _post(rp, _TICK, _tick_body())

    assert _status_of(result) == 502


def test_tick_state_get_bad_envelope_502(rp: Any) -> None:
    """现值读取回执异常（success 非 true）= 502（不假结算）。"""
    _attach(rp)

    original_call = rp.ops_te.call

    async def _bad_get(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if params.get("tool_name") == "character_state.get":
            return {"success": False, "error": "账本缺失"}
        return await original_call(method, params, timeout)

    rp.ops_te.call = _bad_get  # type: ignore[method-assign]

    result = _post(rp, _TICK, _tick_body())

    assert _status_of(result) == 502


def test_tick_ledger_values_non_dict_degrades_to_defaults(rp: Any) -> None:
    """账本现值形态异常（values 非 dict）= 降级空表 → 相对 op 按声明 default
    基线结算（声明真值在引擎，非静默失败）。"""
    _attach(rp)

    original_call = rp.ops_te.call

    async def _no_values(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if params.get("tool_name") == "character_state.get":
            return {"success": True, "data": {"values": None}}
        return await original_call(method, params, timeout)

    rp.ops_te.call = _no_values  # type: ignore[method-assign]

    out = _payload(_post(rp, _TICK, _tick_body()))

    assert out["advanced"] is True
    assert out["state_delta"] == {"world.日期": 2, "world.丰饶": 19}, (
        "default=1/20 基线结算（引擎持声明真值）"
    )


def test_tick_adjudicate_bad_envelope_502(rp: Any) -> None:
    """结算服务回执异常（success 非 true）= 502（传输面，非规则拒绝）。"""
    _attach(rp)

    original_call = rp.ops_te.call

    async def _bad_adj(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if params.get("tool_name") == "state.adjudicate":
            return {"success": False, "error": "服务异常"}
        return await original_call(method, params, timeout)

    rp.ops_te.call = _bad_adj  # type: ignore[method-assign]

    result = _post(rp, _TICK, _tick_body())

    assert _status_of(result) == 502


def test_tick_adjudicate_malformed_payload_502(rp: Any) -> None:
    """结算回执形态非法（三账键缺失）= 502（契约面损坏不假报 advanced）。"""
    _attach(rp)

    original_call = rp.ops_te.call

    async def _half_adj(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if params.get("tool_name") == "state.adjudicate":
            return {"success": True, "data": {"accepted": {}}}
        return await original_call(method, params, timeout)

    rp.ops_te.call = _half_adj  # type: ignore[method-assign]

    result = _post(rp, _TICK, _tick_body())

    assert _status_of(result) == 502


def test_tick_adjudicate_service_down_502(rp: Any) -> None:
    """结算服务不可用（state.adjudicate 抛出）= 502。"""
    rp.ops_te.fail_tools.add("state.adjudicate")
    _attach(rp)

    result = _post(rp, _TICK, _tick_body())

    assert _status_of(result) == 502


def test_tick_ledger_update_down_502(rp: Any) -> None:
    """账本落账失败（character_state.update 不可用）= 502（不假报 advanced）。"""
    rp.ops_te.fail_tools.add("character_state.update")
    _attach(rp)

    result = _post(rp, _TICK, _tick_body())

    assert _status_of(result) == 502


# ── 审计：source=tick / actor=narrator 缺省 / 批标记行 summary────────────────


def test_tick_audit_rows_source_tick_narrator_actor(rp: Any) -> None:
    """效果行 source=tick、actor=narrator 缺省（世界演化非玩家/行为动作）；
    批标记行 summary 载 ticks 数与 fired steps。"""
    ws = str(rp._OPS_ROWS[0]["workspace"])
    rp.ops_te.values.update({"world.日期": 28, "world.季节": "春"})
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body(ticks=2)))

    rows = _audit_rows(ws)
    assert rows, "审计流水落盘"
    assert all(row["source"] == "tick" for row in rows)
    assert all(row["actor"] == "narrator" for row in rows)
    marker = [r for r in rows if r.get("key") == "tick"]
    assert len(marker) == 1, "一批一条批标记行"
    assert marker[0]["to"] == {"ticks": 2, "steps_fired": out["steps_fired"]}
    assert "ticks" in marker[0]["summary"]
    assert "steps_fired" in marker[0]["summary"]


def test_tick_cycle_baseline_missing_step_skipped(rp: Any) -> None:
    """cycle 基线双缺（键无现值且声明无 default）= 步不可结算跳过（端点级——
    编排循环不发明世界没有的状态；纯函数侧见 test_tick_helpers_branch_pins）。"""
    pack = dict(_PACK_DECL)
    pack["variables"] = {
        "world": {
            "日期": {"type": "int", "min": 1, "max": 28, "default": 1},
            "天气": {"type": "string"},  # 无 default，账本现值缺席
        }
    }
    pack["tick"] = {"steps": [{"effects": {"world.天气": {"cycle": ["晴", "雨"]}}}]}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)
    rp._OPS_ROWS[0]["context.rp_state_schema"] = pack["variables"]
    rp.ops_te.values.pop("world.丰饶", None)
    rp.ops_te.values.pop("world.季节", None)
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body()))

    assert out["applied"] is True
    assert out["advanced"] is False, "cycle 基线双缺 = 步跳过（不发明状态）"
    assert rp.ops_te.updates == [], "跳过步零落账"


def test_tick_rejected_step_audited_not_fired(rp: Any) -> None:
    """被拒 step：审计照记全流水（声明-schema 漂移可辨）、不入 steps_fired、
    合法步照常结算（引擎侧逐步结算无原子捆绑——漂移不劫持世界推进）。"""
    ws = str(rp._OPS_ROWS[0]["workspace"])
    pack = dict(_PACK_DECL)
    pack["tick"] = {"steps": [
        {"effects": {"world.无此键": "+1"}},  # 键域外 → Validator unknown_key 拒绝
        {"effects": {"world.日期": "+1"}},
    ]}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "tick_pack.yaml"), pack)
    _attach(rp)

    out = _payload(_post(rp, _TICK, _tick_body()))

    assert out["advanced"] is True
    assert out["steps_fired"] == [1], "被拒步不入 fired"
    assert out["state_delta"] == {"world.日期": 2}
    rows = _audit_rows(ws)
    assert any(row.get("reason") == "unknown_key" for row in rows), "拒绝行随流水可辨"
    assert any(row.get("layer") for row in rows), "拒绝行带 layer 分层标记"


def test_tick_repeat_calls_distinct_idems(rp: Any) -> None:
    """tick 是离散事件：连发两次 = 两批审计、批间独立 idem（不幂等去重）；
    批内各行共享同批 idem（同批重放不双记的扫描键）。"""
    ws = str(rp._OPS_ROWS[0]["workspace"])
    _attach(rp)

    _payload(_post(rp, _TICK, _tick_body()))
    _payload(_post(rp, _TICK, _tick_body()))

    rows = _audit_rows(ws)
    idems = {row["idem"] for row in rows}
    assert len(idems) == 2, "两次调用两批独立 idem"
    assert len(rows) > len(idems), "批内各行共享同批 idem"


def test_tick_settlement_uses_validator_single_source_service(rp: Any) -> None:
    """结算走 pipeline_state_marker_parse 的 state.adjudicate 服务（Validator
    单源；cycle 已由编排侧解析为绝对值——服务只见 op 串与 =值）。"""
    rp.ops_te.values.update({"world.日期": 28, "world.季节": "春"})
    _attach(rp)

    _payload(_post(rp, _TICK, _tick_body()))

    adjudicates = [c for c in rp.ops_captured if c[0] == "state.adjudicate"]
    assert len(adjudicates) == 2
    _, plugin_id, step0 = adjudicates[0]
    assert plugin_id == "pipeline_state_marker_parse"
    assert step0["schema"] == _ROW_SCHEMA, "schema 自会话行 context.rp_state_schema 同源"
    assert step0["changes"] == {"world.日期": "+1", "world.丰饶": "-1"}
    _, _, step1 = adjudicates[1]
    assert step1["changes"] == {"world.季节": "=夏", "world.日期": "=1"}, (
        "cycle 编排侧解析为绝对值，Validator 契约零改动"
    )


# ── 星露谷出厂示范：真实包 yaml + 真实 Validator 端到端小样（零 mock）─────────


def test_stardew_builtin_pack_declares_tick() -> None:
    """出厂示范声明在场：日期触顶 28 → 季节 cycle + 日期重置 1；lint 全绿。"""
    module = _load_server()
    with open(_STARDEW_PACK, encoding="utf-8") as fh:
        pack = yaml.safe_load(fh)
    tick = pack.get("tick")
    assert isinstance(tick, dict), "出厂示范 tick 声明在场"
    assert tick.get("steps"), "steps 非空"
    assert module.lint_tick(tick) == [], "出厂示范声明形态合法"
    rollover = [s for s in tick["steps"] if s.get("when")]
    assert rollover, "触顶换季条件步在场"
    assert rollover[0]["when"]["key"] == "world.日期"
    assert rollover[0]["effects"]["world.季节"] == {"cycle": _SEASONS}
    assert rollover[0]["effects"]["world.日期"] == "=1"


def test_stardew_tick_end_to_end_real_settlement(rp: Any) -> None:
    """真实包 yaml（出厂 stardew_social）+ 真实 validate_and_apply 端到端：
    换季翻页从叙述约定升为引擎结算。"""
    assert _STARDEW_PACK.is_file()
    stardew = yaml.safe_load(_STARDEW_PACK.read_text(encoding="utf-8"))
    rp._OPS_ROWS[0]["context.rp_state_schema"] = stardew["variables"]
    _write_yaml(
        os.path.join(str(rp._user_cards_dir()), "card_hero.yaml"),
        {"name": "牧場主", "pack_ids": ["stardew_social"]},
    )
    rp.ops_te.values.update({"world.日期": 1, "world.季节": "春"})
    _attach(rp)

    # 27 tick：春 1 → 28 触顶 → 夏 1（一季 = 27 tick，自日期 1 起）；10+10+7 分批
    out = _payload(_post(rp, _TICK, _tick_body(ticks=10)))
    assert out["state_delta"] == {"world.日期": 11}
    out = _payload(_post(rp, _TICK, _tick_body(ticks=10)))
    assert out["state_delta"] == {"world.日期": 21}
    out = _payload(_post(rp, _TICK, _tick_body(ticks=7)))
    assert out["state_delta"] == {"world.日期": 1, "world.季节": "夏"}
    assert rp.ops_te.values["world.季节"] == "夏"

    # 冬 28 再进一步：跨年回春（cycle 环绕）
    rp.ops_te.values.update({"world.日期": 28, "world.季节": "冬"})
    out = _payload(_post(rp, _TICK, _tick_body()))

    assert out["state_delta"] == {"world.日期": 1, "world.季节": "春"}


# ── manifest / SDK 文档对账───────────────────────────────────────────────────


def test_manifest_declares_tick_route() -> None:
    """plugin.json 登记 tick 路由（http_endpoints，POST）。"""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    routes = {r["path"]: r["method"] for r in manifest["http_endpoints"]}
    assert routes[_TICK] == "POST"


def test_sdk_doc_registers_tick_section() -> None:
    """GAME_HOST_SDK.md 增 tick 节：端点行（对账测试同一正则形态）+ 载荷形状
    + 空转/回执键语义登记。"""
    doc = _SDK_DOC.read_text(encoding="utf-8")
    assert f"| POST | {_TICK} |" in doc, "端点清单行在场（机械对账正则形态）"
    assert "advanced" in doc, "回执 advanced 键登记"
    assert "steps_fired" in doc, "回执 steps_fired 键登记"
    assert "state_delta" in doc, "回执 state_delta 键登记"
    assert "空转" in doc, "无声明包空转语义登记（不 404）"
