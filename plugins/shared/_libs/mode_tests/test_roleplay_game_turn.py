# @feature: FP-0.2.〇 RP批5 turn通道+宿主SDK+服务类型 | @vision: V1 可进化 | @ci: python-coverage
"""turn 通道契约 + 服务类型框架 + rpHost 单源化 + NPC 深聊（RP 批 5）。

- turn 端点（POST /game/turn）：服务类型框架（契约全集 main/npc_chat/barks/event；
  一期实开 main——玩家输入经 chat.send_message 注入分支薄代理进绑定 RP 会话，
  background 即时回执、叙事经既有 data/messages 轮询回流零新通道）；npc_chat
  一期消费形态 = mode.session 专属会话（不经 turn）；barks/event 后置占位
  400（无消费方不实现死代码）；
- 会话绑定校验（session 须为扮演会话行）+ 单宿主租约（未 attach=409）+
  user_id 从请求 token 解（chat 契约非空，缺失 401）；
- 游戏页 rpHost 桥对象单源化（页内数据面调用唯一出站点）+ turn 接线结构 +
  纯函数 node 直测（rpBuildTurnPayload / rpFilterNpcCards）；
- GAME_HOST_SDK.md 端点清单 vs plugin.json 机械对账（文档漂移即红）。
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

MODES_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_PANEL_EP = "/ext/mode_roleplay"
_GAME_HTML = Path(MODES_DIR) / "mode_roleplay" / "webview" / "game_host.html"
_SDK_DOC = Path(MODES_DIR) / "mode_roleplay" / "webview" / "GAME_HOST_SDK.md"
_MANIFEST = Path(MODES_DIR) / "mode_roleplay" / "plugin.json"

_TURN = f"{_PANEL_EP}/game/turn"

pytestmark = pytest.mark.unit


def _load_server() -> Any:
    """按唯一模块名装载 mode_roleplay server.py（防与其它模式测试模块互覆）。"""
    path = os.path.join(MODES_DIR, "mode_roleplay", "server.py")
    spec = importlib.util.spec_from_file_location("mode_game_turn_roleplay", path)
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


@pytest.fixture
def rp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, patch_shared: Any) -> Any:
    """server 模块 + 通道清空 + pipeline-state provider 注入 + chat capability 捕获桩。

    captured / fail 标志挂在 module 上（captured_chat / chat_raises），测试直读。
    """
    monkeypatch.setenv("AGENTOS_USER_CONFIG_DIR", str(tmp_path / "user_config"))
    module = _load_server()
    module.reset_providers()
    module._reset_game_channels()
    captured: dict[str, Any] = {}

    class _FakeChat:
        async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            captured.update({"method": method, "params": params, "timeout": timeout})
            if getattr(module, "chat_raises", False):
                raise RuntimeError("chat capability 不可用")
            return {"status": "dispatched", "pipeline_id": params.get("pipeline_id")}

    module.plugin = _StubPlugin(_FakeChat())
    patch_shared("plugin", module.plugin)  # 拆分后 rp_* 各持绑定，传播到消费点
    module.captured_chat = captured
    module.chat_raises = False

    async def _state_rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "pipeline_id": "pipe-1",
                "thread_id": "thread-1",
                "mode": "roleplay",
                "workspace": str(tmp_path / "ws"),
            }
        ]

    module._set_provider("pipeline-state", _state_rows)
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    return module


def _access_token(user_id: str = "u-5") -> str:
    """构造内核同形 access token（`base64url(payload).sig`，payload=
    `access:<uid>:<username>:<exp>:<jti>:<h>`；签名段本端点不校验）。"""
    payload = f"access:{user_id}:tester:9999999999:jti:h"
    raw = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{raw}.c2ln"


def _call(
    module: Any,
    path: str,
    method: str = "GET",
    raw_body: str = "",
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query, headers=headers)
    )


def _post(module: Any, path: str, body: dict[str, Any], **kw: Any) -> dict[str, Any]:
    return _call(module, path, method="POST", raw_body=json.dumps(body), **kw)


def _body_json(result: dict[str, Any]) -> dict[str, Any]:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status_of(result: dict[str, Any]) -> int:
    return int(result["data"]["status"])


def _turn_body(**override: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"session_id": "pipe-1", "text": "往北走"}
    body.update(override)
    return body


_AUTH = {"authorization": f"Bearer {_access_token('u-5')}"}


# ── turn：main 消费路径（chat.send_message 注入分支薄代理）──────────────────────


def test_turn_dispatches_to_bound_session(rp: Any) -> None:
    """attach 后 turn(main)：background 注入派发进绑定会话，user_id 从 token 解。"""
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})

    out = _body_json(_post(rp, _TURN, _turn_body(), headers=_AUTH))

    assert out["status"] == "dispatched"
    assert out["session_id"] == "pipe-1"
    assert out["service"] == "main"
    assert out["pipeline_id"] == "pipe-1"
    assert rp.captured_chat["method"] == "send_message"
    params = rp.captured_chat["params"]
    assert params["message"] == "往北走"
    assert params["pipeline_id"] == "pipe-1", "注入分支：消息投给已有管道"
    assert params["user_id"] == "u-5", "user_id 从请求 token 解（chat 契约非空）"
    assert params["background"] is True, "后台派发即时回执（叙事经轮询回流）"
    assert "create" not in params, "turn 不走创建分支（会话已存在）"


def test_turn_service_absent_defaults_main(rp: Any) -> None:
    """service 缺省 = main（桥契约缺省消费主循环；_turn_body 本就不带 service）。"""
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})

    out = _body_json(_post(rp, _TURN, _turn_body(text="看四周"), headers=_AUTH))

    assert out["service"] == "main"


def test_turn_narration_flows_via_existing_messages_endpoint(rp: Any) -> None:
    """零新回推通道：turn 后叙事读面 = 既有 data/messages 端点（契约锚：不新增
    回推端点，端点清单见 SDK 文档对账测试）。"""
    assert _status_of(
        _call(rp, f"{_PANEL_EP}/data/messages", query={"pipeline_id": "pipe-1"})
    ) == 200


# ── turn：fail-closed 拒绝面（绑定校验/租约/鉴权/占位）─────────────────────────


def test_turn_without_lease_conflict(rp: Any) -> None:
    """未 attach 直发 = 409（单宿主租约：服务点位=同租约内请求类型）。"""
    result = _post(rp, _TURN, _turn_body(), headers=_AUTH)

    assert _status_of(result) == 409
    assert "租约" in _body_json(result)["error"]
    assert rp.captured_chat == {}, "拒绝路径零派发"


def test_turn_unknown_or_non_roleplay_session_rejected(rp: Any) -> None:
    """会话绑定校验：非扮演会话行（不存在/他模式）= 400，不派发。"""
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-x", "host_id": "factory_web"})

    result = _post(rp, _TURN, _turn_body(session_id="pipe-x"), headers=_AUTH)

    assert _status_of(result) == 400
    assert "会话不存在或非扮演会话" in _body_json(result)["error"]


def test_turn_non_roleplay_mode_session_rejected(rp: Any) -> None:
    """行存在但 mode 非 roleplay = 同拒（游戏 turn 只进扮演会话）。"""
    async def _rows(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"pipeline_id": "pipe-1", "mode": "research"}]

    rp._set_provider("pipeline-state", _rows)
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _TURN, _turn_body(), headers=_AUTH)

    assert _status_of(result) == 400
    assert "非扮演会话" in _body_json(result)["error"]


@pytest.mark.parametrize("text", ["", "   ", None])
def test_turn_empty_text_400(rp: Any, text: Any) -> None:
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _TURN, _turn_body(text=text), headers=_AUTH)

    assert _status_of(result) == 400
    assert "text" in _body_json(result)["error"]


def test_turn_requires_user_token(rp: Any) -> None:
    """token 缺失/无效 = 401 且零派发（chat.send_message 契约 user_id 非空）。"""
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})

    garbage_shape = base64.urlsafe_b64encode("wrong:shape".encode()).decode().rstrip("=")
    for headers in (
        None,
        {"authorization": "Bearer not-a-token"},
        {"authorization": f"Bearer {garbage_shape}.sig"},  # 可解码但载荷非 access 六段
    ):
        result = _post(rp, _TURN, _turn_body(), headers=headers)
        assert _status_of(result) == 401
    assert rp.captured_chat == {}


@pytest.mark.parametrize(
    ("service", "fragment"),
    [
        ("npc_chat", "mode.session"),
        ("barks", "后置占位"),
        ("event", "后置占位"),
    ],
)
def test_turn_service_placeholders_rejected(rp: Any, service: str, fragment: str) -> None:
    """服务类型占位执法：一期不经 turn 通道的类型 = 400 可读指引（不实现死代码）。"""
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _TURN, _turn_body(service=service), headers=_AUTH)

    assert _status_of(result) == 400
    assert fragment in _body_json(result)["error"]
    assert rp.captured_chat == {}


def test_turn_unknown_service_400(rp: Any) -> None:
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})

    result = _post(rp, _TURN, _turn_body(service="moon"), headers=_AUTH)

    assert _status_of(result) == 400
    assert "非法 service" in _body_json(result)["error"]


def test_turn_capability_failure_502(rp: Any) -> None:
    """chat capability 失败 = 502 如实透传（不静默假成功）。"""
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})
    rp.chat_raises = True

    result = _post(rp, _TURN, _turn_body(), headers=_AUTH)

    assert _status_of(result) == 502
    assert "turn 派发失败" in _body_json(result)["error"]


def test_turn_unexpected_status_502(rp: Any, patch_shared: Any) -> None:
    """回执非 dispatched（如内核拒绝注入坐标）= 502，不吞。"""
    _post(rp, f"{_PANEL_EP}/game/session/attach", {"session_id": "pipe-1", "host_id": "factory_web"})

    class _Odd:
        async def call(self, method: str, params: dict[str, Any], timeout: float | None = None) -> Any:
            return {"status": "weird"}

    module = rp
    module.plugin = _StubPlugin(_Odd())
    patch_shared("plugin", module.plugin)  # 拆分后 rp_* 各持绑定，传播到消费点
    result = _post(module, _TURN, _turn_body(), headers=_AUTH)

    assert _status_of(result) == 502


def test_turn_wrong_method_404_and_missing_session_400(rp: Any) -> None:
    assert _status_of(_call(rp, _TURN, query={"session_id": "pipe-1"})) == 404
    assert _status_of(_post(rp, _TURN, {"text": "x"}, headers=_AUTH)) == 400


# ── 游戏页：rpHost 单源化 + turn 接线（结构断言 + 纯函数 node 直测）───────────────

_MARK = r"rp-engine:single-source-begin \*/([\s\S]*?)/\* rp-engine:single-source-end"


def _page_source() -> str:
    return _GAME_HTML.read_text(encoding="utf-8")


def _extract_engine(src: str) -> str:
    match = re.search(_MARK, src)
    assert match is not None, "引擎单源块标记缺失"
    return match.group(1)


def _run_node_case(script: str) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用（webview 无构建线）")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        proc = subprocess.run([node, path], capture_output=True, text=True, check=False)
    finally:
        Path(path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"node 断言失败: {proc.stderr or proc.stdout}"


def test_rp_host_bridge_is_single_fetch_site() -> None:
    """rpHost 单源化：页内 agentosFetch 唯一调用点在 rpHost.request 内，页内全部
    数据面调用走 rpHost.*（宿主 mod 可见的 API 面=此对象+REST 端点集）。"""
    src = _page_source()
    assert "var rpHost = {" in src
    call_sites = [
        line.strip() for line in src.splitlines()
        if "agentosFetch(" in line and "function agentosFetch" not in line
    ]
    assert call_sites == ["return agentosFetch(path, params);"], \
        f"页内只允许 rpHost.request 一处直接出站: {call_sites}"
    for method in (
        "request:",
        "sessions:",
        "messages:",
        "state:",
        "config:",
        "cards:",
        "attach:",
        "detach:",
        "pushSnapshot:",
        "turn:",
    ):
        assert method in src, f"rpHost 缺方法 {method}"
    for path in (
        "/data/sessions",
        "/data/messages",
        "/game/state",
        "/game/config",
        "/data/cards",
        "/game/session/attach",
        "/game/detach",
        "/game/snapshot",
        "/game/turn",
    ):
        assert path in src, f"rpHost 未覆盖端点 {path}"


def test_turn_wiring_structure() -> None:
    """turn 接线：发送按钮/输入区由租约门控（renderLease），发送与 choices 自动
    发送同走 rpBuildTurnPayload 闸 + rpHost.turn；npc 深聊走 mode.session 同载荷。"""
    src = _page_source()
    assert "function sendTurn(" in src
    assert "rpBuildTurnPayload(" in src
    assert "rpHost.turn(" in src
    assert "gh-send').addEventListener" in src
    assert "disabled = !canTurn" in src, "输入区/发送按钮由租约门控（未绑定禁用）"
    send_region = src.split("function sendTurn(", 1)[1].split("$('gh-send')", 1)[0]
    assert "pollOnce();" in send_region, "发送成功后立即拉一轮（叙事经既有轮询回流）"
    # choices 自动发送（一期直接发送，非仅填入——升级 4-Ⅲ 行为）
    flow_handler = src.split("gh-flow').addEventListener", 1)[1]
    assert "sendTurn(choice.text)" in flow_handler
    assert "选项仅填入不发送" in flow_handler, "未绑定=拒绝+可读提示"
    # npc_chat 深聊入口：mode.session 同载荷（B8）
    npc_handler = src.split("gh-npcs').addEventListener", 1)[1]
    assert "'mode.session'" in npc_handler
    assert "mode_roleplay/" in npc_handler
    assert "深聊·" in npc_handler
    assert "（开始）" in npc_handler


def test_build_turn_payload_contract() -> None:
    """rpBuildTurnPayload（node 直测）：绑定闸/空文本/页 service 闸/载荷形状。"""
    body = f"""
{_extract_engine(_page_source())}
function t(cond, tag, got) {{ if (!cond) {{ console.error(tag + ': ' + JSON.stringify(got)); process.exit(1); }} }}
var lease = {{host_id: 'factory_web'}};
var ok = rpBuildTurnPayload(lease, 'pipe-1', '  往北走 ', 'main');
t(ok.ok === true, '正例应过', ok);
t(ok.payload.session_id === 'pipe-1' && ok.payload.text === '往北走', '载荷=剥离空白后的文本', ok);
t(ok.payload.service === 'main', '载荷带 service=main', ok);
t(rpBuildTurnPayload(null, 'pipe-1', 'x', 'main').ok === false, '无租约拒绝', 1);
t(rpBuildTurnPayload(lease, null, 'x', 'main').ok === false, '无会话拒绝', 1);
t(rpBuildTurnPayload(lease, 'pipe-1', '   ', 'main').ok === false, '空文本拒绝', 1);
t(rpBuildTurnPayload(lease, 'pipe-1', '', 'main').ok === false, '空串拒绝', 1);
t(rpBuildTurnPayload(lease, 'pipe-1', 'x', 'npc_chat').ok === false, '页 service 闸：npc_chat 不经 turn', 1);
t(rpBuildTurnPayload(lease, 'pipe-1', 'x', 'barks').ok === false, '页 service 闸：barks 后置', 1);
t(rpBuildTurnPayload(lease, 'pipe-1', 'x', 'moon').ok === false, '未知 service 拒绝', 1);
t(rpBuildTurnPayload(lease, 'pipe-1', 'x').payload.service === 'main', 'service 缺省 main', 1);
"""
    _run_node_case(body)


def test_filter_npc_cards_contract() -> None:
    """rpFilterNpcCards（node 直测）：tags 含 npc 过滤 + name 回落 + 非法输入空表。"""
    body = f"""
{_extract_engine(_page_source())}
function t(cond, tag, got) {{ if (!cond) {{ console.error(tag + ': ' + JSON.stringify(got)); process.exit(1); }} }}
var cards = [
  {{id: 'npc_a', name: '艾拉', tags: ['npc', '商人'], avatar: '🧝'}},
  {{id: 'hero', name: '主角', tags: ['主角']}},
  {{id: 'npc_b', tags: 'npc'}},
  {{id: 'npc_c', name: '老三', tags: ['NPC']}},
  {{id: 'npc_d', tags: ['npc']}},
  null
];
var out = rpFilterNpcCards(cards);
t(out.length === 2, '只留 tags 数组含小写 npc 的卡（字符串 tags/大写 NPC 均剔）', out);
t(out[0].id === 'npc_a' && out[0].name === '艾拉' && out[0].avatar === '🧝', '透传 id/name/avatar', out);
t(out[1].id === 'npc_d' && out[1].name === 'npc_d', 'name 缺失回落 id', out);
t(rpFilterNpcCards(null).length === 0, '非数组=空表', 1);
t(rpFilterNpcCards([]).length === 0, '空卡库=空表', 1);
"""
    _run_node_case(body)


def test_page_still_free_of_eval_function_in_engine_block() -> None:
    """新增纯函数并入引擎单源块后仍守禁 eval/Function 硬约束（源码扫描）。"""
    code = re.sub(r"/\*[\s\S]*?\*/", "", _extract_engine(_page_source()))
    assert "eval(" not in code
    assert "Function(" not in code


# ── GAME_HOST_SDK.md：端点清单 vs plugin.json 机械对账（文档漂移即红）────────────


_DOC_ENDPOINT_RE = re.compile(r"^\|\s*(GET|POST)\s*\|\s*(/ext/mode_roleplay/\S+?)\s*\|", re.M)


def _doc_endpoints() -> set[tuple[str, str]]:
    assert _SDK_DOC.is_file(), "GAME_HOST_SDK.md 缺失（宿主 SDK 契约文档）"
    return {(m.group(1), m.group(2)) for m in _DOC_ENDPOINT_RE.finditer(_SDK_DOC.read_text(encoding="utf-8"))}


def _manifest_routes() -> dict[str, str]:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    return {r["path"]: r["method"] for r in manifest["http_endpoints"]}


def test_sdk_doc_endpoints_declared_in_manifest() -> None:
    """文档端点清单 ⊆ manifest 声明（同方法）——文档不指向不存在的端点。"""
    routes = _manifest_routes()
    doc = _doc_endpoints()
    assert len(doc) >= 9, f"SDK 文档端点清单过少（宿主面应至少覆盖 game 6 + data 3）: {sorted(doc)}"
    missing = [(m, p) for m, p in sorted(doc) if routes.get(p) != m]
    assert not missing, f"文档端点未在 manifest 声明或方法不符: {missing}"


def test_manifest_game_endpoints_documented() -> None:
    """manifest game/* 端点 ⊆ 文档清单——游戏通道扩端点忘更文档即红。"""
    doc = _doc_endpoints()
    undocumented = [
        (m, p) for p, m in sorted(_manifest_routes().items())
        if "/game/" in p and (m, p) not in doc
    ]
    assert not undocumented, f"game 通道端点未登记进 SDK 文档: {undocumented}"


def test_sdk_doc_registers_service_types_and_placeholder_triggers() -> None:
    """服务类型契约登记：四类型齐 + npc_chat 消费形态（mode.session）+
    barks/event 占位触发条件（后置池登记，非死代码）。"""
    doc = _SDK_DOC.read_text(encoding="utf-8")
    for token in ("main", "npc_chat", "barks", "event"):
        assert token in doc, f"SDK 文档缺服务类型 {token}"
    assert "mode.session" in doc, "npc_chat 一期消费形态（专属会话）须登记"
    assert "触发条件" in doc, "barks/event 占位须写明触发条件"


def test_sdk_doc_self_sufficiency_skeleton() -> None:
    """文档自足性骨架：载荷形状/时序（turn_id 单调/租约/回声防重入）/rpHost 签名/
    外部 mod 最小端点集与示例在场——外部开发者按文档可实现同协议宿主。"""
    doc = _SDK_DOC.read_text(encoding="utf-8")
    for token in (
        "turn_id",          # 时序：单调
        "租约",              # 时序：单宿主租约
        "lastSelfPush",     # 时序：自推回声防重入
        "rpBuildTurnPayload",
        "rpFilterNpcCards",
        "最小端点集",        # 外部 mod 实现面
        "window.agentos",   # 传输面
        "background",       # turn 即时回执语义
    ):
        assert token in doc, f"SDK 文档缺自足性要素: {token}"
    assert "```" in doc, "最小示例须为代码块"


# ── manifest 声明面 ────────────────────────────────────────────────────────────


def test_manifest_declares_turn_route_and_version_bumped() -> None:
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    routes = {r["path"]: r["method"] for r in manifest["http_endpoints"]}
    assert routes[_TURN] == "POST"
    turn_route = next(r for r in manifest["http_endpoints"] if r["path"] == _TURN)
    assert turn_route["auth"] == "user"
    assert turn_route["handler_capability"] == "http.handle"
    parts = manifest["version"].split(".")
    assert (int(parts[0]), int(parts[1])) >= (1, 13), "批 5 manifest bump（1.12.0 → 1.13.0）"
