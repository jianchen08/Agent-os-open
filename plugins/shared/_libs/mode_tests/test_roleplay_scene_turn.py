# @feature: FP-0.2.〇 酒馆场景批编排（缺口批次四 §6 M2） | @vision: V1 可进化 | @ci: python-coverage
"""场景批一次调用编排契约测试（缺口批次四：支柱 3 按场景批处理，成本 N→1）。

- POST data/group/scene_turn：入参 {session_id, scene.background, participants,
  visibility?}——校验 fail-closed 400（缺参/会话不存在或非扮演会话/参与者不在
  卡库/可见性覆盖越界）；按参与者逐个走 agent 键展开（material 组装同路径：人设
  /文风/状态按卡注入），构造多角色单调用 prompt（每角色一份情境投影 = 认知迷雾：
  缺省全场景背景 + 自身状态 + 全体在场名单，visibility 覆盖收窄该角色可见名单）；
  一次 LLM 调用（tool-executor.invoke 显式 plugin_id=llm_service，模型取 llm.yaml
  defaults.chat）；产出按契约 fail-closed 解析为 N 份 {card_id, reaction,
  state_ops?}——单角色解析失败该份缺省并记因，不炸整轮；LLM 传输面失败 = 502。
- 端到端小样走真实依赖：真实双根卡库（出厂卡 + 用户层同 id 接管）+ 真实
  material 组装路径 + 真实解析函数，仅 LLM 调用（外部依赖）与 character_state
  服务 mock。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import sys
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_EP = "/ext/mode_roleplay"
_ROUTE = f"{_EP}/data/group/scene_turn"
_MANIFEST = os.path.join(MODES_DIR, "mode_roleplay", "plugin.json")

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_scene_turn_roleplay", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _StubPlugin:
    """未 bootstrap 测试环境的 capability 存根：get_capability 恒可取。"""

    def __init__(self, handle: Any) -> None:
        self._handle = handle

    def get_capability(self, ns: str) -> Any:
        return self._handle


class _FakeToolExecutor:
    """tool-executor capability 桩：按 tool_name 分发，捕获每次 invoke。"""

    def __init__(self, module: Any) -> None:
        self._module = module
        self.calls: list[dict[str, Any]] = []

    async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
        self.calls.append({"method": method, "params": params, "timeout": timeout})
        tool = params.get("tool_name")
        if tool == "llm.complete_stream":
            self._module.llm_calls += 1
            if self._module.llm_raises:
                raise RuntimeError("llm capability 不可用")
            return self._module.llm_envelope
        if tool == "character_state.get":
            if self._module.state_raises:
                raise RuntimeError("character_state 未初始化")
            card_id = (params.get("args") or {}).get("card_id")
            return {
                "success": True,
                "data": {"card_id": card_id, "schema": {}, "values": self._module.state_values},
            }
        return {"success": False, "error": f"未知工具: {tool}"}

    def llm_invocations(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["params"].get("tool_name") == "llm.complete_stream"]


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    """server 模块 + 用户空间隔离 + pipeline-state provider + tool-executor 桩 +
    用户空间 llm.yaml 缺省模型档（defaults.chat=test-model）。"""
    user_config = tmp_path / "user_config"
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(user_config))
    llm_dir = user_config / "plugins" / "llm"
    llm_dir.mkdir(parents=True)
    (llm_dir / "llm.yaml").write_text(
        "defaults:\n  chat: test-model\n", encoding="utf-8"
    )
    module = _load_server()
    module.reset_providers()
    module.llm_envelope = {
        "success": True,
        "data": {"status": "streamed", "text": "[]", "finish_reason": "stop"},
    }
    module.llm_raises = False
    module.llm_calls = 0
    module.state_raises = False
    module.state_values = {}

    fake_te = _FakeToolExecutor(module)
    module.plugin = _StubPlugin(fake_te)
    module.fake_te = fake_te

    async def _state_rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {"pipeline_id": "pipe-1", "thread_id": "thread-1", "mode": "roleplay"},
            {"pipeline_id": "pipe-other", "thread_id": "thread-2", "mode": "chat"},
        ]

    module._set_provider("pipeline-state", _state_rows)
    return module


def _call(module: Any, body: dict[str, Any]) -> dict[str, Any]:
    raw = base64.b64encode(json.dumps(body, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return asyncio.run(
        module.http_handle(path=_ROUTE, method="POST", raw_body=raw)
    )


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status_of(result: dict[str, Any]) -> int:
    return int(result["data"]["status"])


def _llm_text(entries: list[dict[str, Any]]) -> str:
    return json.dumps(entries, ensure_ascii=False)


def _valid_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": "pipe-1",
        "scene": {"background": "暴雨夜，众人在月光神殿的炉火边相遇。"},
        "participants": ["card_luna", "card_kael"],
    }
    body.update(overrides)
    return body


# ── 校验面（fail-closed 400）──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("body", "why"),
    [
        (_valid_body(session_id=""), "缺 session_id"),
        (_valid_body(session_id="pipe-404"), "未知会话"),
        (_valid_body(session_id="pipe-other"), "非扮演会话"),
        (_valid_body(scene=None), "scene 缺"),
        (_valid_body(scene="暴雨夜"), "scene 非映射"),
        (_valid_body(scene={}), "background 缺"),
        (_valid_body(scene={"background": "   "}), "background 空白"),
        (_valid_body(participants=[]), "participants 空"),
        (_valid_body(participants="card_luna"), "participants 非列表"),
        (_valid_body(participants=["card_luna", "ghost_card"]), "参与者不在卡库"),
    ],
)
def test_scene_turn_validation_400(rp: Any, body: dict[str, Any], why: str) -> None:
    result = _call(rp, body)
    assert _status_of(result) == 400, why
    payload = _body_json(result)
    assert "error" in payload
    assert rp.fake_te.llm_invocations() == [], why


@pytest.mark.parametrize(
    ("visibility", "why"),
    [
        ("card_luna", "visibility 非映射"),
        ({"ghost_card": ["card_kael"]}, "visibility 键不在参与者"),
        ({"card_luna": "card_kael"}, "visibility 值非列表"),
        ({"card_luna": ["ghost_card"]}, "visibility 值越界（非参与者）"),
    ],
)
def test_scene_turn_visibility_validation_400(
    rp: Any, visibility: Any, why: str
) -> None:
    result = _call(rp, _valid_body(visibility=visibility))
    assert _status_of(result) == 400, why
    assert rp.fake_te.llm_invocations() == [], why


# ── 端到端小样（真实卡库/真实 material 组装/真实解析，LLM mock）────────────────


def test_scene_turn_n1_single_call(rp: Any) -> None:
    """N=1 边界：单参与者单份反应，恰一次 LLM 调用，模型取 defaults.chat。"""
    rp.llm_envelope = {
        "success": True,
        "data": {
            "status": "streamed",
            "text": _llm_text([{"card_id": "card_luna", "reaction": "她抬起了提灯。"}]),
        },
    }
    result = _call(rp, _valid_body(participants=["card_luna"]))
    assert _status_of(result) == 200
    payload = _body_json(result)
    assert payload == {
        "reactions": [{"card_id": "card_luna", "reaction": "她抬起了提灯。"}]
    }
    llm_calls = rp.fake_te.llm_invocations()
    assert len(llm_calls) == 1
    params = llm_calls[0]["params"]
    assert params["plugin_id"] == "llm_service"
    args = params["args"]
    assert args["model"] == "test-model"
    assert isinstance(args["messages"], list)
    assert args["messages"]


def test_scene_turn_multi_participant_order_and_one_call(rp: Any) -> None:
    """N=3：一次 LLM 调用产出三份反应（成本 N→1），载荷按输入序对位 card_id。"""
    rp.llm_envelope = {
        "success": True,
        "data": {
            "status": "streamed",
            "text": _llm_text([
                {"card_id": "card_kael", "reaction": "剑客按剑不语。"},
                {"card_id": "card_luna", "reaction": "月语精灵举灯照路。"},
                {"card_id": "card_mira", "reaction": "未来笑着记录一切。"},
            ]),
        },
    }
    result = _call(
        rp,
        _valid_body(participants=["card_luna", "card_kael", "card_mira"]),
    )
    assert _status_of(result) == 200
    payload = _body_json(result)
    reactions = payload["reactions"]
    assert [r["card_id"] for r in reactions] == ["card_luna", "card_kael", "card_mira"]
    assert reactions[1]["reaction"] == "剑客按剑不语。"
    assert len(rp.fake_te.llm_invocations()) == 1


def test_scene_turn_prompt_carries_persona_style_and_scene(rp: Any) -> None:
    """多角色 prompt 走真实 material 组装路径：出厂卡人设/文风（mes_example 占位
    替换）随卡注入，场景背景与输出契约在場。"""
    rp.llm_envelope = {
        "success": True,
        "data": {"status": "streamed", "text": "[]"},
    }
    result = _call(rp, _valid_body())
    assert _status_of(result) == 200
    args = rp.fake_te.llm_invocations()[0]["params"]["args"]
    prompt = json.dumps(args["messages"], ensure_ascii=False)
    # 人设（出厂卡 luna description）与卡名
    assert "月精灵法师" in prompt
    assert "塞拉菲娜·月语" in prompt
    assert "凯尔·铁手" in prompt
    # 文风：mes_example 经 material 占位符替换（{{char}} 不残留）
    assert "{{char}}" not in prompt
    # 场景背景与在场名单
    assert "月光神殿的炉火边相遇" in prompt
    assert "card_luna" in prompt
    assert "card_kael" in prompt
    # 输出契约（严格 JSON 数组）与解析面所需的 card_id 键
    assert "card_id" in prompt
    assert "reaction" in prompt


def test_scene_turn_user_card_override_in_prompt(
    rp: Any, tmp_path: Any
) -> None:
    """双根接管：用户层同 id 卡进 prompt（_load_cards 接管语义沿既有路径）。"""
    user_cards = tmp_path / "user_config" / "agents" / "mode_roleplay"
    user_cards.mkdir(parents=True)
    (user_cards / "card_luna.yaml").write_text(
        "name: 用户版·月语\ndescription: 用户层接管后的设定文本\n",
        encoding="utf-8",
    )
    rp.llm_envelope = {"success": True, "data": {"status": "streamed", "text": "[]"}}
    result = _call(rp, _valid_body(participants=["card_luna"]))
    assert _status_of(result) == 200
    prompt = json.dumps(
        rp.fake_te.llm_invocations()[0]["params"]["args"]["messages"], ensure_ascii=False
    )
    assert "用户版·月语" in prompt
    assert "用户层接管后的设定文本" in prompt
    assert "塞拉菲娜·月语" not in prompt


def test_scene_turn_visibility_fog_narrows_perception(rp: Any) -> None:
    """认知迷雾覆盖：visibility={luna: [kael]} → luna 段不见 mira 的名字/设定，
    其余角色段仍见全名单（缺省=全场景背景+自身状态+全体在场）。"""
    rp.llm_envelope = {
        "success": True,
        "data": {
            "status": "streamed",
            "text": _llm_text([
                {"card_id": "card_luna", "reaction": "她只看见了剑客。"},
                {"card_id": "card_kael", "reaction": "剑客环视全场。"},
            ]),
        },
    }
    result = _call(
        rp,
        _valid_body(
            participants=["card_luna", "card_kael", "card_mira"],
            visibility={"card_luna": ["card_kael"]},
        ),
    )
    assert _status_of(result) == 200
    messages = rp.fake_te.llm_invocations()[0]["params"]["args"]["messages"]
    user_text = "\n".join(str(m.get("content") or "") for m in messages)
    sections = {
        part.split("\n", 1)[0]: part
        for part in user_text.split("## 角色 ")
        if part.strip()
    }
    luna_section = next(v for k, v in sections.items() if k.startswith("card_luna"))
    kael_section = next(v for k, v in sections.items() if k.startswith("card_kael"))
    # 迷雾：luna 段不含被遮蔽者（mira）的任何可辨信息
    assert "星野未来" not in luna_section
    assert "card_mira" not in luna_section
    # 缺省者：kael 段仍见全名单（mira 在场可感知）
    assert "card_mira" in kael_section
    # 全员场景背景恒在（情境投影基座）
    assert "月光神殿的炉火边相遇" in luna_section


def test_scene_turn_state_injection_and_fail_soft(rp: Any) -> None:
    """自身状态：character_state.get 成功 → 状态行进 prompt；服务失败（未初始化
    常态）→ 状态段缺席，整轮照常 200（fail-soft）。"""
    rp.llm_envelope = {
        "success": True,
        "data": {
            "status": "streamed",
            "text": _llm_text([{"card_id": "card_luna", "reaction": "应答"}]),
        },
    }
    rp.state_values = {"affinity": 3}
    ok = _call(rp, _valid_body(participants=["card_luna"]))
    assert _status_of(ok) == 200
    prompt_with_state = json.dumps(
        rp.fake_te.llm_invocations()[0]["params"]["args"]["messages"], ensure_ascii=False
    )
    assert "affinity: 3" in prompt_with_state

    rp.fake_te.calls.clear()
    rp.state_raises = True
    degraded = _call(rp, _valid_body(participants=["card_luna"]))
    assert _status_of(degraded) == 200
    prompt_without_state = json.dumps(
        rp.fake_te.llm_invocations()[0]["params"]["args"]["messages"], ensure_ascii=False
    )
    assert "affinity" not in prompt_without_state


# ── fail-closed 解析（单角色降级记因，不炸整轮）────────────────────────────────


def test_scene_turn_partial_parse_degrade(rp: Any) -> None:
    """LLM 只回了 2/3 角色：缺席者该份缺省并记因，其余照常，整轮 200。"""
    rp.llm_envelope = {
        "success": True,
        "data": {
            "status": "streamed",
            "text": _llm_text([{"card_id": "card_luna", "reaction": "应答"}]),
        },
    }
    result = _call(rp, _valid_body())
    assert _status_of(result) == 200
    reactions = _body_json(result)["reactions"]
    assert reactions[0] == {"card_id": "card_luna", "reaction": "应答"}
    missing = reactions[1]
    assert missing["card_id"] == "card_kael"
    assert "error" in missing
    assert missing["error"]
    assert "reaction" not in missing


def test_scene_turn_all_parse_fail_still_200(rp: Any) -> None:
    """LLM 文本整体不可解析：全部参与者缺省记因，整轮仍 200（载荷如实）。"""
    rp.llm_envelope = {
        "success": True,
        "data": {"status": "streamed", "text": "模型说：抱歉我不能输出 JSON"},
    }
    result = _call(rp, _valid_body())
    assert _status_of(result) == 200
    reactions = _body_json(result)["reactions"]
    assert [r["card_id"] for r in reactions] == ["card_luna", "card_kael"]
    assert all("error" in r and r["error"] for r in reactions)


_BARE = _llm_text([
    {"card_id": "card_luna", "reaction": "a"},
    {"card_id": "card_kael", "reaction": "b"},
])


@pytest.mark.parametrize(
    "text",
    [
        _BARE,
        "```json\n" + _BARE + "\n```",
        "前导说明\n" + _BARE + "\n尾注",
        '{"reactions": ' + _BARE + "}",
    ],
)
def test_scene_parse_accepts_json_shapes(rp: Any, text: str) -> None:
    """解析形态宽容：裸数组/围栏数组/前后杂文/{"reactions": [...]} 信封。"""
    rp.llm_envelope = {"success": True, "data": {"status": "streamed", "text": text}}
    result = _call(rp, _valid_body())
    assert _status_of(result) == 200
    reactions = _body_json(result)["reactions"]
    assert [r["card_id"] for r in reactions] == ["card_luna", "card_kael"]
    assert all(r.get("reaction") for r in reactions)


def test_scene_turn_state_ops_passthrough_and_shape_fail_closed(rp: Any) -> None:
    """state_ops：合法 dict 随份透传；非 dict 形态 → 该份整体缺省记因
    （fail-closed，不静默丢弃字段）。"""
    rp.llm_envelope = {
        "success": True,
        "data": {
            "status": "streamed",
            "text": _llm_text([
                {"card_id": "card_luna", "reaction": "应答", "state_ops": {"affinity": 4}},
                {"card_id": "card_kael", "reaction": "按剑", "state_ops": "怒"},
            ]),
        },
    }
    result = _call(rp, _valid_body())
    assert _status_of(result) == 200
    reactions = _body_json(result)["reactions"]
    assert reactions[0] == {
        "card_id": "card_luna",
        "reaction": "应答",
        "state_ops": {"affinity": 4},
    }
    assert reactions[1]["card_id"] == "card_kael"
    assert "error" in reactions[1]


# ── LLM 传输面 / 模型档（502 显式，不假成功）──────────────────────────────────


@pytest.mark.parametrize(
    ("envelope", "raises", "why"),
    [
        ({"success": False, "error": "模型超时"}, False, "信封 success=false"),
        (
            {"success": True, "data": {"status": "error", "text": None, "partial": {}}},
            False,
            "流中途错误（status=error）",
        ),
        ({"success": True, "data": {"status": "streamed", "text": ""}}, False, "空文本"),
        ({"success": True, "data": {}}, True, "capability 调用抛异常"),
    ],
)
def test_scene_turn_llm_transport_fail_502(
    rp: Any, envelope: dict[str, Any], raises: bool, why: str
) -> None:
    rp.llm_envelope = envelope
    rp.llm_raises = raises
    result = _call(rp, _valid_body())
    assert _status_of(result) == 502, why
    payload = _body_json(result)
    assert "error" in payload


def test_scene_turn_model_missing_502(rp: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """llm.yaml 缺省模型不可得（候选面全空）→ 502，不发起 LLM 调用。"""
    monkeypatch.setattr(rp, "_llm_yaml_candidates", lambda: [])
    result = _call(rp, _valid_body())
    assert _status_of(result) == 502
    assert "error" in _body_json(result)
    assert rp.fake_te.llm_invocations() == []


# ── 纯函数单测（组装/解析判据，parametrize 区分度输入）────────────────────────


def test_scene_reactions_parse_pure_function() -> None:
    module = _load_server()
    # 正常：输入序对位 + 重复条目取首个 + 未知卡忽略
    text = _llm_text([
        {"card_id": "b", "reaction": "rb"},
        {"card_id": "a", "reaction": "ra1"},
        {"card_id": "a", "reaction": "ra2"},
        {"card_id": "zzz", "reaction": "rz"},
    ])
    parsed = module._scene_reactions_parse(text, ["a", "b"])
    assert parsed == [
        {"card_id": "a", "reaction": "ra1"},
        {"card_id": "b", "reaction": "rb"},
    ]
    # 边界：reaction 空串/非字符串 = 该份记因
    bad = module._scene_reactions_parse(
        _llm_text([{"card_id": "a", "reaction": ""}, {"card_id": "b", "reaction": 3}]), ["a", "b"]
    )
    assert all("error" in entry for entry in bad)
    # 性质：解析输出与参与者一一对应（不多不少同序）
    for participants in (["a"], ["a", "b"], ["b", "a", "c"]):
        out = module._scene_reactions_parse("not json at all", participants)
        assert [e["card_id"] for e in out] == participants
        assert all(e.get("error") for e in out)


def test_scene_prompt_sections_and_fog_property() -> None:
    module = _load_server()
    cards = module._load_cards()
    by_id = {c["id"]: c for c in cards}
    chosen = [by_id["card_luna"], by_id["card_kael"], by_id["card_mira"]]
    system_text, user_text = module._scene_prompt(
        "背景文本",
        chosen,
        {"card_luna": ["card_kael"]},
        {"card_luna": "affinity: 3", "card_kael": "", "card_mira": ""},
    )
    # 输出契约在 system 段；情境投影每角色一份（背景随段注入 N 次）
    assert "card_id" in system_text
    assert "reaction" in system_text
    assert user_text.count("## 角色 ") == 3
    assert user_text.count("背景文本") == 3
    # 迷雾性质：被遮蔽者只缺席于被遮蔽段，不缺席全局
    luna_section = user_text.split("## 角色 card_luna", 1)[1].split("## 角色 ", 1)[0]
    assert "card_mira" not in luna_section
    assert "card_mira" in user_text
    # 状态行：有则入段，无则段内零状态行
    assert "affinity: 3" in luna_section
    kael_section = user_text.split("## 角色 card_kael", 1)[1].split("## 角色 ", 1)[0]
    assert "自身状态" not in kael_section


# ── 声明面对账（plugin.json 端点登记，文档漂移即红）────────────────────────────


def test_manifest_registers_scene_turn_route() -> None:
    with open(_MANIFEST, encoding="utf-8") as fh:
        manifest = json.load(fh)
    routes = [e["path"] for e in manifest["http_endpoints"]]
    assert f"{_EP}/data/group/scene_turn" in routes
    entry = next(e for e in manifest["http_endpoints"] if e["path"].endswith("/group/scene_turn"))
    assert entry["method"] == "POST"
    assert entry["route_id"] == "mode_roleplay_scene_turn"
