# @feature: FP-0.2.〇 酒馆沉淀回路 | @vision: V1 可进化 | @ci: python-coverage
"""沉淀回路端点契约测试（缺口批次三：§7.4 游玩流水→统计→候选→审核闸→固化）。

- GET data/precipitation/candidates：审计事件流水统计——命名动作（source=action）
  → 同状态键同向同幅的转移重复出现（≥2 次）聚为候选（可枚举性判据：幅度不一
  致/被拒/作弊旁路/簿记行不成例）；无流水 = 空列表；
- POST data/precipitation/promote：人工审核闸——显式调用才固化，候选规则体
  （标准 behaviors 形态 trigger/effect）写入用户层玩法包（双根装载同 id 接管），
  条目带来源标记（source=precipitated + 时间 + session_id）可单独识别回退；
  与作者声明 behavior 同 id 冲突 = 409（审核闸不覆写作者面），沉淀条目自身
  幂等更新；
- 消费面（零 LLM）：固化条目 = 标准 behaviors 形态，经既有双根装载
  （rp_packs）与既有求值器（state_marker_parse _match_behaviors）生效，端到端
  小样走真实依赖（真实流水文件→候选→固化→装载可见→求值器命中）。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SHARED = Path(MODES_DIR)
_AGENT_DIR = _SHARED / "agent_config_load"
_SMP_DIR = _SHARED / "state_marker_parse"

for _p in (str(_SHARED), str(_AGENT_DIR), str(_SMP_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from rp_packs import load_packs, merge_state_decl  # noqa: E402
from rp_state_audit import append_audit_rows  # noqa: E402

pytestmark = pytest.mark.unit

_EP = "/ext/mode_roleplay/data"
_CANDIDATES = f"{_EP}/precipitation/candidates"
_PROMOTE = f"{_EP}/precipitation/promote"
_PACK_ID = "xianxia_combat"


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_precipitation_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_state_marker_plugin() -> Any:
    """state_marker_parse plugin.py 装载（e2e 求值器消费面——真实 4a 通道）。"""
    mod_name = "precipitation_state_marker_parse_plugin"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _SMP_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 用户空间隔离 + pipeline-state provider 注入（ws 解析链）。"""
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path / "user_config"))
    module = _load_server()
    module.reset_providers()
    workspace = tmp_path / "ws"

    async def _state_rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "pipeline_id": "pipe-1",
                "thread_id": "thread-1",
                "mode": "roleplay",
                "workspace": str(workspace),
            }
        ]

    module._set_provider("pipeline-state", _state_rows)
    workspace.mkdir(parents=True, exist_ok=True)
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


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status_of(result: dict[str, Any]) -> int:
    return int(result["data"]["status"])


def _post(module: Any, path: str, body: Any) -> dict[str, Any]:
    raw = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return _call(module, path, method="POST", raw_body=raw)


def _action_event(
    seq: int,
    action: str,
    target: str,
    op: str,
    frm: Any,
    to: Any,
    **extra: Any,
) -> dict[str, Any]:
    """成形审计事件行（与 rp_state_audit 写面产物同构：信封 + 事件四件套）。"""
    row: dict[str, Any] = {
        "ts": "2026-10-15T00:00:00+00:00",
        "idem": f"pipe-1:{seq}",
        "source": "action",
        "key": target,
        "target": target,
        "op": op,
        "from": frm,
        "to": to,
        "reason": "accepted",
        "action": action,
        "actor": "player",
        "summary": f"player {action} {target}",
    }
    row.update(extra)
    return row


def _write_stream(ws: Path, turns: list[int]) -> None:
    """真实写面（append_audit_rows 单源）落两轮同款转移流水。"""
    for seq in turns:
        append_audit_rows(
            str(ws),
            [{
                "key": "player.气血",
                "op": "-",
                "from": 100,
                "to": 82,
                "reason": "accepted",
                "source": "action",
                "action": "出剑迎敌",
            }],
            f"pipe-1:{seq}",
            "state_marker",
        )


# ── 纯函数：流水 → 候选（可枚举性判据）────────────────────────────────────────


def test_repeated_action_transition_becomes_candidate(rp: Any) -> None:
    """同款转移重复出现 ≥2 次 → 聚为一个候选（字段齐：actor/action/target/
    建议规则体/出现次数/样本轮次）。"""
    events = [
        _action_event(12, "出剑迎敌", "player.气血", "-", 100, 82),
        _action_event(15, "出剑迎敌", "player.气血", "-", 82, 64),
        _action_event(12, "出剑迎敌", "player.气血", "-", 64, 46),  # 同轮重放行
    ]
    out = rp.precipitation_candidates(events)
    assert len(out) == 1
    cand = out[0]
    assert cand["actor"] == "player"
    assert cand["action"] == "出剑迎敌"
    assert cand["target"] == "player.气血"
    assert cand["op"] == "-"
    assert cand["value"] == -18
    assert cand["count"] == 3
    assert cand["sample_turns"] == [12, 15]
    rule = cand["rule"]
    assert rule["effect"] == {"player.气血": "-18"}
    assert re.search(rule["trigger"], "出剑迎敌")
    assert rule["id"].startswith("precipitated:")


@pytest.mark.parametrize(
    "events",
    [
        # 单次出现：无重复不成例
        [_action_event(12, "出剑迎敌", "player.气血", "-", 100, 82)],
        # 同键同向但幅度漂移：转移不可枚举（§7.4 判据），永久留 LLM
        [
            _action_event(12, "出剑迎敌", "player.气血", "-", 100, 82),
            _action_event(15, "出剑迎敌", "player.气血", "-", 82, 52),
        ],
        # 非命名动作行（op 动词映射/簿记）：无 action→状态映射可言
        [
            _action_event(12, "increase", "player.气血", "+", 10, 28,
                          source="state_marker", actor="narrator"),
            _action_event(15, "increase", "player.气血", "+", 28, 46,
                          source="state_marker", actor="narrator"),
        ],
    ],
)
def test_non_enumerable_transitions_not_candidates(rp: Any, events: list) -> None:
    assert rp.precipitation_candidates(events) == []


def test_rejected_cheat_and_existing_behavior_rows_excluded(rp: Any) -> None:
    """被拒行（layer）/作弊旁路/既有 behavior 行不成例——只有真实结算的玩家
    动作转移入统计。"""
    events = [
        _action_event(12, "出剑迎敌", "player.气血", "-", 100, 82),
        _action_event(15, "出剑迎敌", "player.气血", "-", 82, 64),
        # 被拒：同款转移但被契约拒（layer 在场）
        _action_event(16, "出剑迎敌", "player.气血", "-", 64, -36,
                      layer="soft", reason="delta_exceeded"),
        # 作弊旁路
        _action_event(17, "出剑迎敌", "player.气血", "-", 64, 46,
                      source="cheat", reason="cheat_bypass", actor="cheat"),
        # 既有 behavior 行（已是规则，不再沉淀）
        _action_event(18, "increase", "player.灵力", "+", 10, 25,
                      source="behavior", behavior="调息回气", actor="调息回气"),
    ]
    out = rp.precipitation_candidates(events)
    assert len(out) == 1
    assert out[0]["count"] == 2
    assert out[0]["target"] == "player.气血"


def test_enum_absolute_set_transition_candidate(rp: Any) -> None:
    """枚举赋值转移（op "="）同值重复 → 候选，effect 为「=值」形态。"""
    events = [
        _action_event(12, "打坐调息", "player.伤势", "=", "轻伤", "无伤"),
        _action_event(15, "打坐调息", "player.伤势", "=", "重伤", "无伤"),
    ]
    out = rp.precipitation_candidates(events)
    assert len(out) == 1
    assert out[0]["op"] == "="
    assert out[0]["value"] == "无伤"
    assert out[0]["rule"]["effect"] == {"player.伤势": "=无伤"}


def test_candidates_deterministic_order_and_multi_action(rp: Any) -> None:
    """多动作各自成候选，输出按 (action, target) 稳定排序（面板呈现确定性）。"""
    events = [
        _action_event(12, "调息回气", "player.灵力", "+", 10, 20),
        _action_event(13, "出剑迎敌", "player.气血", "-", 100, 82),
        _action_event(15, "调息回气", "player.灵力", "+", 20, 30),
        _action_event(16, "出剑迎敌", "player.气血", "-", 82, 64),
    ]
    out = rp.precipitation_candidates(events)
    assert [(c["action"], c["target"]) for c in out] == [
        ("出剑迎敌", "player.气血"),
        ("调息回气", "player.灵力"),
    ]
    assert all(c["count"] == 2 for c in out)


def test_empty_or_malformed_stream_empty_candidates(rp: Any) -> None:
    """无流水/坏行 = 空列表（fail-soft，与审计读面同口径）。"""
    assert rp.precipitation_candidates([]) == []
    events: list[Any] = [None, "坏行", {"key": "x"}, 42,
                         _action_event(12, "出剑迎敌", "player.气血", "-", 100, 82)]
    assert rp.precipitation_candidates(events) == []


def test_missing_baseline_delta_not_candidate(rp: Any) -> None:
    """缺基线（from=None）幅度不可计算 → 不成例（不猜幅度）。"""
    events = [
        _action_event(12, "出剑迎敌", "player.气血", "-", None, 82),
        _action_event(15, "出剑迎敌", "player.气血", "-", None, 64),
    ]
    assert rp.precipitation_candidates(events) == []


@pytest.mark.parametrize(
    "rows",
    [
        # 命名动作但 op 不可枚举（非法变更动词）
        [
            _action_event(12, "出剑迎敌", "player.气血", "~", 100, 82),
            _action_event(15, "出剑迎敌", "player.气血", "~", 82, 64),
        ],
        # 命名动作但 action 是 op 动词簿记名（无动作→状态映射可言）
        [
            _action_event(12, "increase", "player.气血", "+", 10, 28),
            _action_event(15, "increase", "player.气血", "+", 28, 46),
        ],
        # 绝对赋值但值非标量（dict 不可作 effect op 串）
        [
            _action_event(12, "施加状态", "player.buffs", "=", "x", {"n": 1}),
            _action_event(15, "施加状态", "player.buffs", "=", "x", {"n": 2}),
        ],
    ],
)
def test_bad_op_bookkeeping_and_unscalar_set_rows_excluded(rp: Any, rows: list) -> None:
    assert rp.precipitation_candidates(rows) == []


def test_missing_idem_candidate_without_sample_turns(rp: Any) -> None:
    """idem 缺席：转移仍可成例（count 计数），但样本轮次如实为空（不猜轮）。"""
    events = [
        _action_event(0, "出剑迎敌", "player.气血", "-", 100, 82),
        _action_event(0, "出剑迎敌", "player.气血", "-", 82, 64),
    ]
    for row in events:
        row.pop("idem")
    out = rp.precipitation_candidates(events)
    assert len(out) == 1
    assert out[0]["count"] == 2
    assert out[0]["sample_turns"] == []


# ── GET /data/precipitation/candidates 端点 ──────────────────────────────────


def test_candidates_endpoint_requires_session(rp: Any) -> None:
    result = _call(rp, _CANDIDATES, query={})
    assert _status_of(result) == 400
    assert "session_id" in _body_json(result)["error"]


def test_candidates_endpoint_no_stream_empty_list(rp: Any) -> None:
    """会话在但无审计文件 = 空列表（200，fail-soft 非错误）。"""
    payload = _body_json(_call(rp, _CANDIDATES, query={"session_id": "pipe-1"}))
    assert payload == {"candidates": []}


def test_candidates_endpoint_unknown_session_empty_list(rp: Any) -> None:
    """未知会话（无 workspace 行）= 空列表（读面降级，与既定口径同）。"""
    payload = _body_json(_call(rp, _CANDIDATES, query={"session_id": "nope"}))
    assert payload == {"candidates": []}


def test_candidates_endpoint_reads_real_stream(rp: Any, tmp_path: Any) -> None:
    """真实流水文件（append_audit_rows 单源写面）→ 端点候选。"""
    _write_stream(tmp_path / "ws", [12, 15])
    payload = _body_json(_call(rp, _CANDIDATES, query={"session_id": "pipe-1"}))
    assert len(payload["candidates"]) == 1
    cand = payload["candidates"][0]
    assert cand["action"] == "出剑迎敌"
    assert cand["count"] == 2
    assert cand["rule"]["effect"] == {"player.气血": "-18"}


# ── POST /data/precipitation/promote 端点（人工审核闸）────────────────────────


def _promote_body(
    session_id: str = "pipe-1",
    pack_id: str = _PACK_ID,
    rule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "pack_id": pack_id,
        "rule": rule
        or {
            "id": "precipitated:出剑迎敌:player.气血",
            "trigger": "出剑迎敌",
            "effect": {"player.气血": "-18"},
        },
    }


@pytest.mark.parametrize(
    "body",
    [
        {"pack_id": _PACK_ID, "rule": {"id": "a", "trigger": "a", "effect": {"x": "-1"}}},
        {"session_id": "pipe-1", "rule": {"id": "a", "trigger": "a", "effect": {"x": "-1"}}},
        {"session_id": "pipe-1", "pack_id": "非法/id!", "rule": {"id": "a", "trigger": "a", "effect": {"x": "-1"}}},
        {"session_id": "pipe-1", "pack_id": _PACK_ID, "rule": {"trigger": "a", "effect": {"x": "-1"}}},
        {"session_id": "pipe-1", "pack_id": _PACK_ID, "rule": {"id": "a", "trigger": "", "effect": {"x": "-1"}}},
        {"session_id": "pipe-1", "pack_id": _PACK_ID, "rule": {"id": "a", "trigger": "a", "effect": {}}},
        {"session_id": "pipe-1", "pack_id": _PACK_ID, "rule": {"id": "a", "trigger": "a", "effect": {"x": {"n": 1}}}},
        {"session_id": "pipe-1", "pack_id": _PACK_ID, "rule": {"id": "a", "trigger": "(坏regex", "effect": {"x": "-1"}}},
        {"session_id": "pipe-1", "pack_id": _PACK_ID},
    ],
)
def test_promote_fail_closed_validation(rp: Any, body: dict) -> None:
    result = _post(rp, _PROMOTE, body)
    assert _status_of(result) == 400
    assert "error" in _body_json(result)


def test_promote_invalid_body_400(rp: Any) -> None:
    assert _status_of(_post(rp, _PROMOTE, "not-json")) == 400


def test_promote_unknown_pack_404(rp: Any) -> None:
    result = _post(rp, _PROMOTE, _promote_body(pack_id="no_such_pack"))
    assert _status_of(result) == 404


def test_promote_writes_user_pack_takeover_builtin(rp: Any, tmp_path: Any) -> None:
    """审核闸固化：无用户层副本时整包承接出厂内容 + 追加沉淀条目（双根装载
    同 id 接管语义），条目带来源标记可单独识别。"""
    result = _post(rp, _PROMOTE, _promote_body())
    assert _status_of(result) == 200
    payload = _body_json(result)
    assert payload["status"] == "promoted"
    assert payload["pack_id"] == _PACK_ID
    user_file = tmp_path / "user_config" / "packs" / f"{_PACK_ID}.yaml"
    assert user_file.is_file()
    data = yaml.safe_load(user_file.read_text(encoding="utf-8"))
    # 出厂声明面保真（整包承接，不是只写 behaviors 的空壳）
    assert isinstance(data["variables"], dict)
    assert data["variables"]
    assert "剑气斩" in data["behaviors"]
    entry = data["behaviors"]["precipitated:出剑迎敌:player.气血"]
    assert entry["trigger"] == "出剑迎敌"
    assert entry["effect"] == {"player.气血": "-18"}
    assert entry["source"] == "precipitated"
    assert entry["precipitated_at"]
    assert entry["precipitated_from"] == "pipe-1"


def test_promote_appends_to_existing_user_pack(rp: Any, tmp_path: Any) -> None:
    """已有用户层副本：原地追加，不覆写用户既有编辑。"""
    user_file = tmp_path / "user_config" / "packs" / f"{_PACK_ID}.yaml"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(
        (Path(MODES_DIR) / "mode_roleplay" / "packs" / f"{_PACK_ID}.yaml")
        .read_text(encoding="utf-8")
    )
    base["behaviors"]["我的自定义行为"] = {"trigger": "自定义", "effect": {"player.灵力": "+1"}}
    user_file.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    assert _status_of(_post(rp, _PROMOTE, _promote_body())) == 200
    data = yaml.safe_load(user_file.read_text(encoding="utf-8"))
    assert "我的自定义行为" in data["behaviors"]
    assert "precipitated:出剑迎敌:player.气血" in data["behaviors"]


@pytest.mark.parametrize(
    ("rule_id", "expected_status"),
    [("剑气斩", 409), ("precipitated:出剑迎敌:player.气血", 200)],
    ids=["authored-conflict", "precipitated-upsert"],
)
def test_promote_collision_semantics(rp: Any, rule_id: str, expected_status: int) -> None:
    """作者声明 behavior 同 id = 409（审核闸不覆写作者面）；沉淀 id 自身 =
    幂等更新（200，条目仍唯一）。"""
    body = _promote_body(rule={"id": rule_id, "trigger": "出剑迎敌", "effect": {"player.气血": "-18"}})
    assert _status_of(_post(rp, _PROMOTE, body)) == expected_status
    assert _status_of(_post(rp, _PROMOTE, body)) == expected_status
    if expected_status == 200:
        data = yaml.safe_load(
            (Path(rp._user_packs_dir()) / f"{_PACK_ID}.yaml").read_text(encoding="utf-8")
        )
        assert sum(1 for key in data["behaviors"] if key == rule_id) == 1


def test_promote_user_authored_entry_conflict_409(rp: Any, tmp_path: Any) -> None:
    """用户副本内作者手写条目（非沉淀来源）同 id = 409 不覆写（出厂撞名同闸）。"""
    user_file = tmp_path / "user_config" / "packs" / f"{_PACK_ID}.yaml"
    user_file.parent.mkdir(parents=True, exist_ok=True)
    base = yaml.safe_load(
        (Path(MODES_DIR) / "mode_roleplay" / "packs" / f"{_PACK_ID}.yaml")
        .read_text(encoding="utf-8")
    )
    base["behaviors"]["我的自定义行为"] = {"trigger": "自定义", "effect": {"player.灵力": "+1"}}
    user_file.write_text(yaml.safe_dump(base, allow_unicode=True, sort_keys=False), encoding="utf-8")
    body = _promote_body(rule={"id": "我的自定义行为", "trigger": "自定义", "effect": {"player.灵力": "+2"}})
    result = _post(rp, _PROMOTE, body)
    assert _status_of(result) == 409
    data = yaml.safe_load(user_file.read_text(encoding="utf-8"))
    assert data["behaviors"]["我的自定义行为"]["effect"] == {"player.灵力": "+1"}


def test_promote_user_dir_unavailable_400(rp: Any, patch_shared: Any) -> None:
    """用户空间不可得（user_config_dir()=None）= 400 如实报错，不静默假成功。"""
    patch_shared("_user_packs_dir", lambda: None)  # 消费点在 rp_* 自持绑定
    result = _post(rp, _PROMOTE, _promote_body())
    assert _status_of(result) == 400
    assert "user_config_dir" in _body_json(result)["error"]


# ── 端到端小样：流水→候选→固化→装载可见→求值器命中（真实依赖）────────────────


def test_e2e_stream_candidates_promote_load_evaluator(rp: Any, tmp_path: Any) -> None:
    """§7.4 回路全程真实链：真实审计写面 → 端点候选 → 审核闸固化 → rp_packs
    双根装载可见 → state_marker_parse 求值器对叙述正文命中（零 LLM 消费面）。"""
    # 1. 游玩流水（真实 append_audit_rows）
    _write_stream(tmp_path / "ws", [12, 15])
    # 2. 复盘统计 → 候选
    payload = _body_json(_call(rp, _CANDIDATES, query={"session_id": "pipe-1"}))
    cand = payload["candidates"][0]
    # 3. 人工审核闸固化（面板把候选规则体显式提交）
    promoted = _body_json(
        _post(rp, _PROMOTE, _promote_body(rule=cand["rule"]))
    )
    assert promoted["status"] == "promoted"
    # 4. 装载可见：既有双根装载（用户层接管）合并出 behaviors
    packs, warnings = load_packs(
        [_PACK_ID],
        str(_SHARED / "mode_roleplay"),
        str(tmp_path / "user_config" / "packs"),
    )
    assert packs
    assert not warnings
    variables, behaviors, _merge_warnings = merge_state_decl(None, None, packs)
    # 出厂包带 ui/graph 呈现/引擎面键 → merge 静默忽略不入装载 state（2026-10-17
    # 起延迟键 warning 已解除）；断行为：variables 合并非空 + 固化条目装载可见。
    assert variables
    assert "precipitated:出剑迎敌:player.气血" in behaviors
    # 5. 零 LLM 消费：真实求值器（4a 通道）对叙述正文命中固化规则
    mod = _load_state_marker_plugin()
    pairs = mod.StateMarkerParsePlugin._match_behaviors(
        {"context.rp_state_behaviors": behaviors}, "你出剑迎敌，剑光一闪，正中要害。"
    )
    assert any(
        key == "player.气血" and value == "-18" and meta.get("behavior") == cand["rule"]["id"]
        for key, value, meta in pairs
    )
