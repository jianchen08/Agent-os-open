# @feature: FP-0.2.〇 世界内嵌谱系右移批次二 自由操作→结构化命令语义面 | @vision: V1 可进化 | @ci: python-coverage
"""自由操作→结构化命令语义面（世界内嵌批次二，调研 §7.6 真缺口①）。

- 包声明面：pack yaml 顶层可选键 `operations`（{id, label, params_schema?,
  effects}）——card_lint.lint_operations 形态单源（零 IO）+ 消费侧归一剔除
  （非法条目整条剔除+warning，与 ui.lineage 剔除回落同构）；
- GET game/config 透出 operations 清单（包级声明面，卡侧不参与；声明序后者
  非空清单整体覆盖——choices 同构）；
- POST /game/operation（body {session_id, operation: {id, params}}）：
  Propose（id 须在绑定包清单 fail-closed 400 / params 按 params_schema 类型
  边界校验 fail-closed 400 / 会话校验与 game/turn 同闸 + 单宿主租约）→
  Adjudicate（effects 走 State Contract Validator 同源结算——state.adjudicate
  纯委托服务，与 behaviors 同通道：clamp/账本/审计 source=operation）→ 回执
  `{applied: true, state_delta}`；被拒 = `{applied: false, reason: <具体原因>}`
  （拒绝必须带原因，支柱④；玩家也是 Agent 同一校验，支柱⑦——不做 cheat
  旁路、原子结算无部分应用）；
- 审计：操作行进 rp_state_audit 流水（source=operation，actor=player，summary
  带操作 id）——沉淀回路输入自动可见。
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
_OPERATION = f"{_PANEL_EP}/game/operation"
_CONFIG = f"{_PANEL_EP}/game/config"
_ATTACH = f"{_PANEL_EP}/game/session/attach"
_MANIFEST = Path(MODES_DIR) / "mode_roleplay" / "plugin.json"
_SDK_DOC = Path(MODES_DIR) / "mode_roleplay" / "webview" / "GAME_HOST_SDK.md"

pytestmark = pytest.mark.unit

_ROW_SCHEMA = {
    "player": {"气血": {"type": "int", "min": 0, "max": 100, "default": 100}},
    "world": {"位置": {"type": "string", "default": "宗门"}},
}

_PACK_DECL = {
    "name": "操作测试包",
    "variables": _ROW_SCHEMA,
    "operations": [
        {"id": "strike", "label": "出剑", "effects": {"player.气血": "-18"}},
        {
            "id": "meditate",
            "label": "调息",
            "params_schema": {"时长": {"type": "int", "min": 1, "max": 12, "default": 3}},
            "effects": {"player.气血": "+8"},
        },
        {
            "id": "move_to",
            "label": "移动",
            "params_schema": {"目的地": {"type": "string"}},
            "effects": {"world.位置": "=黑风寨"},
        },
    ],
}


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_roleplay_operations_test", path)
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
            result = validate_and_apply(
                dict(self.values), args.get("changes") or {},
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
    """server 模块 + 会话行（含 schema 硬规则）+ 双根卡/包 + tool-executor 桩。"""
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
    te = _FakeToolExecutor({"player.气血": 100, "world.位置": "宗门"}, captured)
    module.plugin = _StubPlugin(te)
    module.ops_te = te
    module.ops_captured = captured
    _write_yaml(
        os.path.join(str(module._user_cards_dir()), "card_hero.yaml"),
        {"name": "侠客", "pack_ids": ["op_pack"]},
    )
    _write_yaml(os.path.join(str(module._user_packs_dir()), "op_pack.yaml"), dict(_PACK_DECL))
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


def _op_body(**override: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": "pipe-1",
        "operation": {"id": "strike", "params": {}},
    }
    body.update(override)
    return body


# ── lint_operations：声明形态单源（零 IO）─────────────────────────────────────


def test_lint_operations_valid_declaration_clean() -> None:
    """合法声明（含 params_schema 全字段）= 空警告。"""
    module = _load_server()
    warnings = module.lint_operations(_PACK_DECL["operations"])
    assert warnings == []


def test_lint_operations_none_and_non_list() -> None:
    """None（键缺席）= 空；非列表 = 单条警告。"""
    module = _load_server()
    assert module.lint_operations(None) == []
    assert module.lint_operations({"id": "strike"}) == ["operations 须为列表"]


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ({"label": "出剑", "effects": {"a": "+1"}}, "id"),
        ({"id": "Strike", "label": "出剑", "effects": {"a": "+1"}}, "id"),
        ({"id": "strike", "effects": {"a": "+1"}}, "label"),
        ({"id": "strike", "label": "  ", "effects": {"a": "+1"}}, "label"),
        ({"id": "strike", "label": "出剑"}, "effects"),
        ({"id": "strike", "label": "出剑", "effects": ["+1"]}, "effects"),
        ({"id": "strike", "label": "出剑", "effects": {}, "params_schema": []}, "params_schema"),
        (
            {"id": "strike", "label": "出剑", "effects": {"a": "+1"},
             "params_schema": {"时长": {"type": "text"}}},
            "type",
        ),
        (
            {"id": "strike", "label": "出剑", "effects": {"a": "+1"},
             "params_schema": {"时长": {"type": "int", "min": "低"}}},
            "min",
        ),
        (
            {"id": "strike", "label": "出剑", "effects": {"a": "+1"},
             "params_schema": {"时长": {"type": "int", "default": "三"}}},
            "default",
        ),
        (
            {"id": "strike", "label": "出剑", "effects": {"a": "+1"},
             "params_schema": {"时长": "int"}},
            "须为映射",
        ),
        (
            {"id": "strike", "label": "出剑", "effects": {"a": "+1"}, "note": "备注"},
            "未知声明键",
        ),
        ("strike", "条目"),
    ],
)
def test_lint_operations_bad_forms_reported(entry: Any, fragment: str) -> None:
    """非法形态逐组报警（id 登记名/label 非空/effects 映射/params_schema 子集形态）。"""
    module = _load_server()
    warnings = module.lint_operations([entry])
    assert len(warnings) == 1
    assert fragment in warnings[0]


def test_lint_operations_duplicate_id_reported() -> None:
    """同 id 重复 = 警告（登记名单一语义）。"""
    module = _load_server()
    entry = {"id": "strike", "label": "出剑", "effects": {"a": "+1"}}
    warnings = module.lint_operations([entry, dict(entry)])
    assert any("重复" in w for w in warnings)


# ── game/config 透出 operations 清单───────────────────────────────────────────


def test_config_exposes_operations_listing(rp: Any) -> None:
    """会话绑定包 operations 清单经 game/config 透出（归一全条目含 effects）。"""
    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))
    ops = out.get("operations")
    assert isinstance(ops, list)
    assert len(ops) == 3
    by_id = {o["id"]: o for o in ops}
    assert by_id["strike"]["label"] == "出剑"
    assert by_id["strike"]["effects"] == {"player.气血": "-18"}
    assert by_id["meditate"]["params_schema"]["时长"]["max"] == 12


def test_config_operations_bad_entry_dropped_valid_kept(rp: Any) -> None:
    """消费侧归一剔除：非法条目整条剔除 + 合法条目保留（fail-soft 不整组作废）。"""
    pack = dict(_PACK_DECL)
    pack["operations"] = [
        {"id": "Bad Id", "label": "坏", "effects": {}},  # id 形态非法 → 剔除
        {"id": "strike", "label": "出剑", "effects": {"player.气血": "-18"}},
    ]
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert [o["id"] for o in out["operations"]] == ["strike"]


def test_config_operations_non_list_dropped(rp: Any) -> None:
    """operations 非列表 = 声明整组剔除（消费侧归一 warning 留痕）→ 键缺席。"""
    pack = dict(_PACK_DECL)
    pack["operations"] = {"id": "strike"}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert "operations" not in out


def test_config_operations_non_dict_entry_dropped(rp: Any) -> None:
    """非映射条目剔除，映射条目保留（坏条目不整组作废）。"""
    pack = dict(_PACK_DECL)
    pack["operations"] = [
        "strike",
        {"id": "strike", "label": "出剑", "effects": {"player.气血": "-18"}},
    ]
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert [o["id"] for o in out["operations"]] == ["strike"]


def test_config_operations_bad_params_schema_entry_dropped(rp: Any) -> None:
    """params_schema 形态非法 = 整条剔除（严格口径：参数契约不可静默丢弃）。"""
    pack = dict(_PACK_DECL)
    pack["operations"] = [
        {"id": "bad_params", "label": "坏参", "effects": {}, "params_schema": []},
        {"id": "strike", "label": "出剑", "effects": {"player.气血": "-18"}},
    ]
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert [o["id"] for o in out["operations"]] == ["strike"]


def test_config_operations_card_side_not_involved(rp: Any) -> None:
    """operations 是包级声明面：卡 extensions.operations 不透出（choices 同构）。"""
    _write_yaml(
        os.path.join(str(rp._user_cards_dir()), "card_hero.yaml"),
        {
            "name": "侠客",
            "pack_ids": ["op_pack"],
            "extensions": {"operations": [{"id": "card_op", "label": "卡侧", "effects": {}}]},
        },
    )

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert all(o["id"] != "card_op" for o in out["operations"])


def test_config_operations_absent_when_undeclared(rp: Any) -> None:
    """无 operations 声明 = 键缺席（键缺席式，Tier 0 兼容态零新增键）。"""
    pack = {k: v for k, v in _PACK_DECL.items() if k != "operations"}
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert "operations" not in out


def test_config_operations_later_pack_wins(rp: Any) -> None:
    """声明序后者非空清单整体覆盖（choices 同构：双包后者胜）。"""
    _write_yaml(
        os.path.join(str(rp._user_packs_dir()), "op_pack_two.yaml"),
        {
            "name": "第二包",
            "operations": [{"id": "gather", "label": "采集", "effects": {"world.位置": "=集市"}}],
        },
    )
    _write_yaml(
        os.path.join(str(rp._user_cards_dir()), "card_hero.yaml"),
        {"name": "侠客", "pack_ids": ["op_pack", "op_pack_two"]},
    )

    out = _payload(_call(rp, _CONFIG, query={"session_id": "pipe-1"}))

    assert [o["id"] for o in out["operations"]] == ["gather"]


# ── Propose：fail-closed 400 面（会话校验/清单外/params 越界）──────────────────


def test_operation_requires_lease(rp: Any) -> None:
    """未 attach 直发 = 409（单宿主租约，game/turn 同闸）。"""
    result = _post(rp, _OPERATION, _op_body())

    assert _status_of(result) == 409
    assert "租约" in _payload(result)["error"]


def test_operation_route_get_method_404(rp: Any) -> None:
    """GET 打操作路由 = 404（POST-only，游戏域通道族同款方法错配语义）。"""
    result = _call(rp, _OPERATION, method="GET", query={"session_id": "pipe-1"})

    assert _status_of(result) == 404


def test_operation_unknown_or_non_roleplay_session_400(rp: Any) -> None:
    """会话绑定校验（game/turn 同闸）：不存在/非扮演会话 = 400。"""
    _post(rp, _ATTACH, {"session_id": "pipe-x", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, _op_body(session_id="pipe-x"))

    assert _status_of(result) == 400
    assert "会话不存在或非扮演会话" in _payload(result)["error"]


@pytest.mark.parametrize(
    "body",
    [
        {"session_id": "pipe-1"},
        {"session_id": "pipe-1", "operation": "strike"},
        {"session_id": "pipe-1", "operation": {}},
        {"session_id": "pipe-1", "operation": {"params": {}}},
    ],
)
def test_operation_body_shape_400(rp: Any, body: dict[str, Any]) -> None:
    """operation 载荷形态 fail-closed：须为映射且 id 非空。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, body)

    assert _status_of(result) == 400
    reason = _payload(result)["error"]
    assert reason.strip(), "拒绝必须带原因（不许静默）"


def test_operation_unlisted_id_rejected_with_reason(rp: Any) -> None:
    """清单外 id = 400（未声明=不可发，枚举声明制）+ 原因可读。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, _op_body(operation={"id": "fly_to_moon", "params": {}}))

    assert _status_of(result) == 400
    reason = _payload(result)["error"]
    assert reason.strip()
    assert "fly_to_moon" in reason
    assert "operations" in reason
    assert rp.ops_captured == [], "Propose 拒绝零结算调用"


def test_operation_param_out_of_range_rejected_with_reason(rp: Any) -> None:
    """params 越界 = 400 + 原因带字段名与边界（拒绝必须带原因）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(
        rp, _OPERATION,
        _op_body(operation={"id": "meditate", "params": {"时长": 99}}),
    )

    assert _status_of(result) == 400
    reason = _payload(result)["error"]
    assert "时长" in reason
    assert reason.strip()


@pytest.mark.parametrize(
    ("op_id", "params", "fragment"),
    [
        ("meditate", {"时长": "一炷香"}, "时长"),
        ("move_to", {}, "目的地"),
        ("move_to", {"目的地": "黑风寨", "速度": 3}, "速度"),
    ],
)
def test_operation_param_shape_rejected_with_reason(
    rp: Any, op_id: str, params: dict[str, Any], fragment: str
) -> None:
    """params 形态面：类型不符/必填缺席/未声明键 = 400 + 原因带定位。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, _op_body(operation={"id": op_id, "params": params}))

    assert _status_of(result) == 400
    reason = _payload(result)["error"]
    assert fragment in reason
    assert reason.strip()


def test_operation_param_default_fills_missing(rp: Any) -> None:
    """有 default 的字段缺席 = 按缺省补齐放行（params_schema default 语义）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, _op_body(operation={"id": "meditate", "params": {}}))

    out = _payload(result)
    assert out["applied"] is True


def test_operation_params_absent_uses_defaults(rp: Any) -> None:
    """params 键整缺席（None 分支）= 同缺省补齐放行。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    out = _payload(_post(rp, _OPERATION, _op_body(operation={"id": "meditate"})))

    assert out["applied"] is True


def test_operation_params_non_mapping_400(rp: Any) -> None:
    """params 非映射 = 400（fail-closed 形态面）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, _op_body(operation={"id": "meditate", "params": "三息"}))

    assert _status_of(result) == 400
    assert "params 须为映射" in _payload(result)["error"]


def test_operation_param_min_bound_rejected_with_reason(rp: Any) -> None:
    """params 下界越界 = 400 + 原因带 min 边界（越界两侧区分度）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(
        rp, _OPERATION,
        _op_body(operation={"id": "meditate", "params": {"时长": 0}}),
    )

    assert _status_of(result) == 400
    reason = _payload(result)["error"]
    assert "时长" in reason
    assert "< min" in reason


def test_operation_bad_session_shape_400(rp: Any) -> None:
    """session_id 形态非法 = 400（_game_channel_id 同闸，与快照/turn 同款）。"""
    result = _post(rp, _OPERATION, _op_body(session_id="坏 id/斜杠"))

    assert _status_of(result) == 400
    assert "非法 session_id" in _payload(result)["error"]


def test_operation_state_get_down_502(rp: Any) -> None:
    """现值读取服务不可用（character_state.get 抛出）= 502。"""
    rp.ops_te.fail_tools.add("character_state.get")
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, _op_body())

    assert _status_of(result) == 502


def test_operation_state_get_bad_envelope_502(rp: Any) -> None:
    """现值读取回执异常（success 非 true）= 502（不假结算）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    original_call = rp.ops_te.call

    async def _bad_get(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if params.get("tool_name") == "character_state.get":
            return {"success": False, "error": "账本缺失"}
        return await original_call(method, params, timeout)

    rp.ops_te.call = _bad_get  # type: ignore[method-assign]

    result = _post(rp, _OPERATION, _op_body())

    assert _status_of(result) == 502


def test_operation_ledger_values_non_dict_degrades_to_no_baseline(rp: Any) -> None:
    """账本现值形态异常（values 非 dict）= 降级空表 → 无 default 声明的相对 op
    无基线被拒带原因（fail-closed 拒绝，非静默透传）。"""
    # 声明 int 字段无 default：现值缺失时相对 op 无基线（Validator default 回填
    # 不可用），no_baseline 拒绝路径可辨。
    rp._OPS_ROWS[0]["context.rp_state_schema"] = {
        "player": {"内力": {"type": "int", "min": 0, "max": 100}},
    }
    pack = dict(_PACK_DECL)
    pack["operations"] = [{"id": "channel", "label": "运功", "effects": {"player.内力": "+10"}}]
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    original_call = rp.ops_te.call

    async def _no_values(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if params.get("tool_name") == "character_state.get":
            return {"success": True, "data": {"values": None}}
        return await original_call(method, params, timeout)

    rp.ops_te.call = _no_values  # type: ignore[method-assign]

    out = _payload(_post(rp, _OPERATION, _op_body(operation={"id": "channel", "params": {}})))

    assert out["applied"] is False
    assert "no_baseline" in out["reason"]


def test_operation_adjudicate_bad_envelope_502(rp: Any) -> None:
    """结算服务回执异常（success 非 true）= 502（传输面，非规则拒绝）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    original_call = rp.ops_te.call

    async def _bad_adj(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if params.get("tool_name") == "state.adjudicate":
            return {"success": False, "error": "服务异常"}
        return await original_call(method, params, timeout)

    rp.ops_te.call = _bad_adj  # type: ignore[method-assign]

    result = _post(rp, _OPERATION, _op_body())

    assert _status_of(result) == 502


def test_operation_adjudicate_malformed_payload_502(rp: Any) -> None:
    """结算回执形态非法（三账键缺失）= 502（契约面损坏不假报 applied）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    original_call = rp.ops_te.call

    async def _half_adj(method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        if params.get("tool_name") == "state.adjudicate":
            return {"success": True, "data": {"accepted": {}}}
        return await original_call(method, params, timeout)

    rp.ops_te.call = _half_adj  # type: ignore[method-assign]

    result = _post(rp, _OPERATION, _op_body())

    assert _status_of(result) == 502


# ── Adjudicate：合法结算/拒绝回执/账本/审计────────────────────────────────────


def test_operation_legal_applies_with_state_delta(rp: Any) -> None:
    """合法操作：Validator 同源结算 → applied=true + state_delta + 账本落账。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    out = _payload(_post(rp, _OPERATION, _op_body()))

    assert out["applied"] is True
    assert out["operation"] == "strike"
    assert out["state_delta"] == {"player.气血": 82}
    assert rp.ops_te.updates == [
        {"card_id": "card_hero", "changes": {"player.气血": 82}}
    ], "accepted 经 character_state.update 落账（与 behaviors 同通道）"


def test_operation_clamped_at_boundary(rp: Any) -> None:
    """clamp：超限增量钳到声明边界（Validator 同源，clamp 非拒绝）。"""
    rp.ops_te.values["player.气血"] = 95
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    out = _payload(_post(rp, _OPERATION, _op_body(operation={"id": "meditate", "params": {"时长": 12}})))

    assert out["applied"] is True
    assert out["state_delta"] == {"player.气血": 100}, "95+8 钳到声明 max=100"


def test_operation_effects_rejected_receipt_with_reason(rp: Any) -> None:
    """效果被拒（键域不在 variables 声明内 fail-closed）= 回执 applied=false +
    具体原因；零落账；审计照记全流水（双账）。"""
    pack = dict(_PACK_DECL)
    pack["operations"] = [
        {"id": "corrupt", "label": "越权", "effects": {"npc.阿比.好感度": "+50"}},
    ]
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    out = _payload(_post(rp, _OPERATION, _op_body(operation={"id": "corrupt", "params": {}})))

    assert out["applied"] is False
    reason = out["reason"]
    assert isinstance(reason, str), "拒绝必须带原因（不许静默）"
    assert reason.strip(), "原因非空"
    assert "npc.阿比.好感度" in reason, "原因具体到键"
    assert rp.ops_te.updates == [], "被拒零落账"


def test_operation_absolute_rejection_atomic_no_partial_apply(rp: Any) -> None:
    """原子结算：效果组含被拒键 = 整体拒绝，合法键也不落账（玩家离散命令
    无部分应用）。"""
    pack = dict(_PACK_DECL)
    pack["operations"] = [
        {"id": "mixed", "label": "混合", "effects": {"player.气血": "+8", "world.无此键": "+1"}},
    ]
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    out = _payload(_post(rp, _OPERATION, _op_body(operation={"id": "mixed", "params": {}})))

    assert out["applied"] is False
    assert "world.无此键" in out["reason"]
    assert rp.ops_te.updates == [], "原子拒绝：合法键不落账"


def test_operation_schema_absent_failclosed_with_reason(rp: Any) -> None:
    """会话行无状态契约（context.rp_state_schema 缺席）= 无契约可结，回执拒绝
    带原因（fail-closed，不静默透传）。"""
    rp._OPS_ROWS[0].pop("context.rp_state_schema")
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    out = _payload(_post(rp, _OPERATION, _op_body()))

    assert out["applied"] is False
    assert out["reason"].strip()
    assert rp.ops_te.updates == []


def test_operation_adjudicate_service_down_502(rp: Any) -> None:
    """传输面失败（state.adjudicate 服务不可用）= 502 error 面（非规则拒绝）。"""
    rp.ops_te.fail_tools.add("state.adjudicate")
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, _op_body())

    assert _status_of(result) == 502


def test_operation_ledger_down_502(rp: Any) -> None:
    """账本落账失败（character_state.update 不可用）= 502（传输面，不假报 applied）。"""
    rp.ops_te.fail_tools.add("character_state.update")
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _OPERATION, _op_body())

    assert _status_of(result) == 502


def test_operation_uses_validator_single_source_service(rp: Any) -> None:
    """结算走 pipeline_state_marker_parse 的 state.adjudicate 服务（Validator
    单源，跨插件 tool-executor 显式 plugin_id 同通道）。"""
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})
    _payload(_post(rp, _OPERATION, _op_body()))

    adjudicates = [c for c in rp.ops_captured if c[0] == "state.adjudicate"]
    assert len(adjudicates) == 1
    _, plugin_id, args = adjudicates[0]
    assert plugin_id == "pipeline_state_marker_parse"
    assert args["schema"] == _ROW_SCHEMA, "schema 自会话行 context.rp_state_schema 同源"
    assert args["changes"] == {"player.气血": "-18"}, "effects 自包声明（请求不携 effects）"


# ── 审计：source=operation / actor=player / summary 带操作 id──────────────────


def test_operation_audit_rows_operation_source_player_actor(rp: Any) -> None:
    """审计行四件套：source=operation、actor=player、summary 带操作 id。"""
    ws = str(rp._OPS_ROWS[0]["workspace"])
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    _payload(_post(rp, _OPERATION, _op_body()))

    rows = _audit_rows(ws)
    assert rows, "审计流水落盘"
    for row in rows:
        assert row["source"] == "operation"
        assert row["actor"] == "player"
        assert row["action"] == "strike"
        assert "strike" in row["summary"]


def test_operation_rejection_still_audited(rp: Any) -> None:
    """被拒操作同样进审计流水（reason/layer 随行，双账惯例）。"""
    ws = str(rp._OPS_ROWS[0]["workspace"])
    pack = dict(_PACK_DECL)
    pack["operations"] = [{"id": "corrupt", "label": "越权", "effects": {"npc.阿比.好感度": "+50"}}]
    _write_yaml(os.path.join(str(rp._user_packs_dir()), "op_pack.yaml"), pack)
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    _payload(_post(rp, _OPERATION, _op_body(operation={"id": "corrupt", "params": {}})))

    rows = _audit_rows(ws)
    assert rows, "审计流水落盘"
    assert all(row["source"] == "operation" for row in rows)
    assert any(row.get("layer") for row in rows), "拒绝行带 layer 分层标记"
    assert any("corrupt" in row["summary"] for row in rows)


def test_operation_repeat_events_distinct_idems(rp: Any) -> None:
    """操作是离散事件：连发两次 = 两批审计落账（有意不幂等去重）。"""
    ws = str(rp._OPS_ROWS[0]["workspace"])
    rp.ops_te.values["player.气血"] = 100
    _post(rp, _ATTACH, {"session_id": "pipe-1", "host_id": "factory_web"})

    _payload(_post(rp, _OPERATION, _op_body()))
    _payload(_post(rp, _OPERATION, _op_body()))

    rows = _audit_rows(ws)
    assert len(rows) >= 2
    idems = {row["idem"] for row in rows}
    assert len(idems) == len(rows), "每次调用独立 idem"


# ── manifest / SDK 文档对账────────────────────────────────────────────────────


def test_manifest_declares_operation_route() -> None:
    """plugin.json 登记操作路由（http_endpoints，POST）。"""
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    routes = {r["path"]: r["method"] for r in manifest["http_endpoints"]}
    assert routes[_OPERATION] == "POST"


def test_sdk_doc_registers_operation_section() -> None:
    """GAME_HOST_SDK.md 增 operation 节：端点行（对账测试同一正则形态）+
    载荷形状 + 拒绝回执语义（拒绝必须带原因）。"""
    doc = _SDK_DOC.read_text(encoding="utf-8")
    assert f"| POST | {_OPERATION} |" in doc, "端点清单行在场（机械对账正则形态）"
    assert "applied" in doc, "回执 applied 键登记"
    assert "reason" in doc, "回执 reason 键登记（拒绝必须带原因）"
    assert "state_delta" in doc, "回执 state_delta 键登记"
