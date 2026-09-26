# @feature: FP-0.2.二 模式体系测试补标 | @ci: python-coverage
"""六模式包能力闭包与残留容错面补测（写动作/降级文件之外的第三面）。

- on_load 惰性闭包族：_messages（limit 透传）/ _invoke / _task_list 经假能力
  句柄真实执行（_set_provider 直灌测法绕过了这层，须走 on_load 注册路径）
- 派发错误分诊（通道未注入/抛错/缺 id）在 godot/research/roleplay/writing
  各包的补位（coding 已有）
- planning projects 登记态/派生态双口径、tasks/discussions 路由
- research KB 检索（hindsight.recall 代理）结果形态矩阵
- godot addon HTTP 抓取矩阵（HTTPError/非 200/坏 JSON/快照缺失诚实形态）
- roleplay cards/lorebooks 物料鲁棒性（缺目录/非卡文件/损坏 yaml）
- coding reviews 信封非 dict/list 兜底
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import io
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

import pytest
import yaml

MODES_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MODES = ("mode_coding", "mode_godot", "mode_planning", "mode_research", "mode_roleplay", "mode_writing")

pytestmark = pytest.mark.unit

_loaded: dict[str, Any] = {}


def _fresh(mode: str):
    """按唯一模块名装载模式包 server.py 并复位 provider（防双实例串扰）。"""
    if mode not in _loaded:
        path = os.path.join(MODES_DIR, mode, "server.py")
        spec = importlib.util.spec_from_file_location(f"mode_cap_{mode}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _loaded[mode] = module
    module = _loaded[mode]
    module.reset_providers()
    return module


def _call(module, path: str, method: str = "GET", raw_body: str = "", query: dict | None = None) -> dict:
    return asyncio.run(
        module.http_handle(path=path, method=method, raw_body=raw_body, query=query or {})
    )


def _body_json(result: dict) -> dict:
    assert result["success"] is True
    data = result["data"]
    assert data["body_encoding"] == "base64"
    return json.loads(base64.b64decode(data["body"]).decode("utf-8"))


def _status(result: dict) -> int:
    return int(result["data"]["status"])


class _FakeCapHandle:
    """能力句柄替身：按方法名回放预置结果并记录调用（bind 后收剥前缀方法名）。"""

    def __init__(self, results: dict[str, Any]):
        self._results = results
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, params: dict | None = None, timeout: float | None = None):
        self.calls.append((method, params or {}))
        if method in self._results:
            value = self._results[method]
            if isinstance(value, Exception):
                raise value
            return value
        return []


def _install_capabilities(monkeypatch, module, results: dict[str, Any]) -> dict[str, _FakeCapHandle]:
    """monkeypatch get_capability → 三能力假句柄，跑 on_load 注册真实闭包。"""
    handles = {
        "pipeline-state": _FakeCapHandle({"list": results.get("state", [])}),
        "service-registry": _FakeCapHandle({"messages.list": results.get("messages", [])}),
        "tool-executor": _FakeCapHandle({"invoke": results.get("invoke", {"data": {}})}),
    }
    monkeypatch.setattr(module.plugin, "get_capability", lambda name: handles[name], raising=True)
    asyncio.run(module._on_load({}))
    return handles


# ── on_load 闭包族（六包同构，参数化） ────────────────────────────────────────────


@pytest.mark.parametrize("mode", MODES)
def test_on_load_messages_closure_limit_passthrough(mode, monkeypatch):
    """on_load 注册的 _messages 闭包：limit 非空透传 int，service-registry 取数。"""
    module = _fresh(mode)
    handles = _install_capabilities(monkeypatch, module, {"messages": [{"role": "user"}]})

    rows = asyncio.run(module._call_provider("messages", pipeline_id="p-1", limit=3))
    assert rows == [{"role": "user"}]
    method, params = handles["service-registry"].calls[-1]
    assert method == "messages.list"
    assert params == {"pipeline_id": "p-1", "limit": 3}


@pytest.mark.parametrize(
    "mode,action,body",
    [
        ("mode_coding", "/ext/mode_coding/data/actions/dispatch_issue", {"issue_text": "修 x"}),
        ("mode_godot", "/ext/mode_godot/data/actions/dispatch", {"instruction": "放大按钮"}),
        ("mode_planning", "/ext/mode_planning/data/actions/plan", {"goal": "规划博客"}),
        ("mode_research", "/ext/mode_research/data/actions/start", {"question": "q", "depth": "quick"}),
        # play possess-only / regenerate 410 退役（2026-09-25 会话化）：mode_roleplay 零派发面
        ("mode_writing", "/ext/mode_writing/data/actions/chapter_act", {"act": "continue"}),
    ],
)
def test_on_load_invoke_closure_write_actions_via_capability(mode, action, body, monkeypatch):
    """写动作经 on_load 注册的 _invoke 闭包走 tool-executor 能力 → 200 任务 id。"""
    module = _fresh(mode)
    extra: dict = {}
    module = _fresh("mode_planning")
    handles = _install_capabilities(
        monkeypatch,
        module,
        {
            "invoke": {
                "tasks": [
                    {"id": "t1", "title": "子任务一", "status": "completed"},
                    "garbage",
                ]
            }
        },
    )
    result = _call(
        module, "/ext/mode_planning/data/tasks", query={"parent_task_id": "parent-1"}
    )
    assert _status(result) == 200
    tasks = _body_json(result)["tasks"]
    assert [t["task_id"] for t in tasks] == ["t1"]
    method, params = handles["tool-executor"].calls[-1]
    assert method == "invoke"
    assert params["args"] == {"parent_task_id": "parent-1"}


def test_planning_discussions_route_with_garbage_rows(monkeypatch):
    """/data/discussions 走 _messages 闭包；非 dict 消息行跳过不崩。"""
    module = _fresh("mode_planning")
    _install_capabilities(
        monkeypatch,
        module,
        {"messages": ["garbage", {"role": "user", "content_preview": "正常行"}]},
    )
    result = _call(
        module, "/ext/mode_planning/data/discussions", query={"pipeline_id": "p-1"}
    )
    assert _status(result) == 200
    messages = _body_json(result)["messages"]
    assert [m["role"] for m in messages] == ["user"]


def test_planning_missing_parent_task_id_400():
    """/data/tasks 缺 parent_task_id → 400 缺参。"""
    module = _fresh("mode_planning")
    result = _call(module, "/ext/mode_planning/data/tasks")
    assert _status(result) == 400
    assert "parent_task_id" in _body_json(result)["error"]


def test_planning_write_error_shapes():
    """create_project/plan 错误分诊：空结果/错误透传/缺 id（缺 id 走 _extract_id 空）。"""
    module = _fresh("mode_planning")

    class _Once:
        def __init__(self, result):
            self.result = result

        async def __call__(self, payload):
            return self.result

    module._set_provider("tool-executor", _Once({"data": {}}))
    out = _body_json(
        _call(
            module, "/ext/mode_planning/data/actions/create_project",
            "POST", json.dumps({"goal": "g"}),
        )
    )
    assert "未返回结果" in out["error"]

    module._set_provider("tool-executor", _Once({"data": {"error": "后端炸了"}}))
    result = _call(
        module, "/ext/mode_planning/data/actions/create_project",
        "POST", json.dumps({"goal": "g"}),
    )
    assert _status(result) == 409 and "后端炸了" in _body_json(result)["error"]

    module._set_provider("tool-executor", _Once({"data": {"ok": True}}))
    out = _body_json(
        _call(
            module, "/ext/mode_planning/data/actions/create_project",
            "POST", json.dumps({"goal": "g"}),
        )
    )
    assert "未返回 project_id" in out["error"]

    out = _body_json(
        _call(module, "/ext/mode_planning/data/actions/plan", "POST", json.dumps({"goal": "g"}))
    )
    assert "未返回任务 id" in out["error"]


def test_planning_projects_registry_and_derived_dual_source(monkeypatch):
    """projects 双口径：登记 provider 在位 → 登记态；缺省 → pipeline-state 派生态。"""
    module = _fresh("mode_planning")

    async def _registry_rows():
        return [
            "garbage",
            {"id": "", "title": "无 id 行"},
            {"id": "p1", "title": "登记项目", "path": "/p1", "workflow_state": "s", "created": True},
        ]

    async def _state_rows():
        return [
            None,
            {"pipeline_id": "px", "mode": "planning", "task.parent_project_id": ""},
            {"pipeline_id": "py", "mode": "planning", "task.parent_project_id": "p1"},
        ]

    module._set_provider("projects", _registry_rows)
    module._set_provider("pipeline-state", _state_rows)
    result = _call(module, "/ext/mode_planning/data/projects")
    assert _status(result) == 200
    body = _body_json(result)
    assert body["source"] == "registry"
    ids = [p["project_id"] for p in body["projects"] if isinstance(p, dict)]
    assert "p1" in ids

    module.reset_providers()
    module._set_provider("pipeline-state", _state_rows)
    body = _body_json(_call(module, "/ext/mode_planning/data/projects"))
    assert body["source"] == "derived"


# ── research：KB 检索形态矩阵 + 派发错误分诊 ─────────────────────────────────────


class _OnceProvider:
    def __init__(self, result):
        self.result = result

    async def __call__(self, payload):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _sources(module: str, **kw):
    mod = _fresh("mode_research") if isinstance(module, str) else module
    return _call(mod, "/ext/mode_research/data/sources", query=kw)


def test_research_kb_search_result_shapes(monkeypatch):
    """KB 检索结果形态矩阵：报错降级/裸列表/垃圾信封/嵌套分数/空文本跳过。"""
    module = _fresh("mode_research")

    module._set_provider("tool-executor", _OnceProvider(RuntimeError("kb 炸了")))
    body = _body_json(_sources(module, q="信号"))
    assert body["kb_available"] is False and body["kb_results"] == []

    module._set_provider(
        "tool-executor",
        _OnceProvider(
            {
                "data": {
                    "error": "后端错",
                    "results": [{"content": "不应出现"}],
                }
            }
        ),
    )
    body = _body_json(_sources(module, q="信号"))
    assert body["kb_available"] is False

    module._set_provider(
        "tool-executor",
        _OnceProvider(
            {
                "data": {
                    "results": [
                        "garbage",
                        {"content": "   "},
                        {"text": "嵌套分数条目", "scores": {"final": 0.8}},
                        {"content": "直接分数条目", "score": 2},
                    ]
                }
            }
        ),
    )
    body = _body_json(_sources(module, q="信号"))
    assert body["kb_available"] is True
    by_content = {r["content"]: r["score"] for r in body["kb_results"]}
    assert by_content == {"嵌套分数条目": 0.8, "直接分数条目": 2}

    module._set_provider(
        "tool-executor", _OnceProvider({"data": ["garbage", {"content": "裸列表有效"}]})
    )
    body = _body_json(_sources(module, q="信号"))
    assert [r["content"] for r in body["kb_results"]] == ["裸列表有效"]

    module._set_provider("tool-executor", _OnceProvider({"data": "垃圾信封"}))
    body = _body_json(_sources(module, q="信号"))
    assert body["kb_available"] is True and body["kb_results"] == []


def test_research_dispatch_error_triage():
    """research 派发：通道未注入 409/抛错透传/缺任务 id。"""
    module = _fresh("mode_research")
    body = json.dumps({"question": "q", "depth": "quick"})

    # 派发阶段错误（_dispatch_task 返回 error）如实回 200 载荷（无假成功）
    result = _call(module, "/ext/mode_research/data/actions/start", "POST", body)
    assert _status(result) == 200 and "通道不可用" in _body_json(result)["error"]

    module._set_provider("tool-executor", _OnceProvider(RuntimeError("executor 炸了")))
    out = _body_json(_call(module, "/ext/mode_research/data/actions/start", "POST", body))
    assert "task_submit 调用失败" in out["error"] and "executor 炸了" in out["error"]

    module._set_provider("tool-executor", _OnceProvider({"data": {"ok": True}}))
    out = _body_json(_call(module, "/ext/mode_research/data/actions/start", "POST", body))
    assert "未返回任务 id" in out["error"]


# ── writing：派发错误分诊补位 ────────────────────────────────────────────────────


def test_writing_dispatch_error_triage():
    """writing 派发：抛错透传/缺任务 id。"""
    module = _fresh("mode_writing")
    body = json.dumps({"act": "continue"})

    module._set_provider("tool-executor", _OnceProvider(RuntimeError("executor 炸了")))
    out = _body_json(_call(module, "/ext/mode_writing/data/actions/chapter_act", "POST", body))
    assert "task_submit 调用失败" in out["error"]

    module._set_provider("tool-executor", _OnceProvider({"data": {"ok": True}}))
    out = _body_json(_call(module, "/ext/mode_writing/data/actions/chapter_act", "POST", body))
    assert "未返回任务 id" in out["error"]


# ── roleplay：物料鲁棒性 + 派发错误分诊 ──────────────────────────────────────────


def test_roleplay_load_cards_robustness(tmp_path):
    """卡目录：缺目录空集/非卡文件跳过/目录卡抛 OSError 跳过/非 dict 跳过/合法入列。

    成熟化 Wave B 双根签名：第二参显式传空目录隔离用户层（缺省经 user_space
    现场解析，环境相关——单测不依赖机器用户空间状态）。
    """
    module = _fresh("mode_roleplay")
    no_user = str(tmp_path / "no_user")
    assert module._load_cards(str(tmp_path / "nope"), no_user) == []

    agents = tmp_path / "agents"
    agents.mkdir()
    (agents / "note.txt").write_text("非卡文件", encoding="utf-8")
    (agents / "card_dir.yaml").mkdir()  # open 目录 → OSError → 跳过
    (agents / "card_str.yaml").write_text("- a\n- b", encoding="utf-8")  # 非 dict
    (agents / "card_ok.yaml").write_text(
        yaml.safe_dump({"name": "测试卡", "description": "描述", "personality": "温和"}),
        encoding="utf-8",
    )
    cards = module._load_cards(str(agents), no_user)
    assert [c["id"] for c in cards] == ["card_ok"]
    assert cards[0]["name"] == "测试卡"


def test_roleplay_load_lorebooks_robustness(tmp_path):
    """世界书目录：缺目录/非 yaml/OSError/非 dict/条目垃圾逐项容错。"""
    module = _fresh("mode_roleplay")
    assert module._load_lorebooks(str(tmp_path / "nope")) == []

    books_dir = tmp_path / "lorebooks"
    books_dir.mkdir()
    (books_dir / "note.txt").write_text("非 yaml", encoding="utf-8")
    (books_dir / "book_dir.yaml").mkdir()  # OSError
    (books_dir / "book_str.yaml").write_text("just-a-string", encoding="utf-8")  # 非 dict
    (books_dir / "book_ok.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "书一",
                "entries": [
                    "garbage",
                    {"keys": ["月光"], "secondary_keys": ["神殿"], "content": "月光神殿设定"},
                ],
            }
        ),
        encoding="utf-8",
    )
    books = module._load_lorebooks(str(books_dir))
    assert [b["id"] for b in books] == ["book_ok"]
    assert books[0]["entries"][0]["keys"] == ["月光"]


def test_roleplay_dispatch_retired_410():
    """regenerate 已收编进扮演会话的宿主消息操作 → 410 语义化退役（本插件
    零任务派发面，play possess-only）。"""
    module = _fresh("mode_roleplay")
    body = json.dumps({"pipeline_id": "p-rp1"})
    result = _call(module, "/ext/mode_roleplay/data/actions/regenerate", "POST", body)
    assert _status(result) == 410
    assert "收编" in _body_json(result)["error"]



# ── godot：addon HTTP 抓取矩阵 + 派发错误分诊 ────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch, responses: list):
    """按调用次序回放响应；元素为 _FakeResp/HTTPError/OSError 实例。"""
    calls = {"n": 0}

    def fake_urlopen(url, timeout=None):
        idx = calls["n"]
        calls["n"] += 1
        item = responses[idx] if idx < len(responses) else responses[-1]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen, raising=True)
    return calls


def test_godot_addon_json_matrix(monkeypatch):
    """_addon_json：200 字典/HTTPError→非200 None/坏 JSON None/OSError None。"""
    module = _fresh("mode_godot")

    _patch_urlopen(monkeypatch, [_FakeResp(200, b'{"ok": true}')])
    assert asyncio.run(module._addon_json("/health")) == {"ok": True}

    http_err = urllib.error.HTTPError(
        "http://x", 404, "nf", None, io.BytesIO(b"missing")
    )
    _patch_urlopen(monkeypatch, [http_err])
    assert asyncio.run(module._addon_json("/health")) is None

    _patch_urlopen(monkeypatch, [_FakeResp(200, b"not-json{")])
    assert asyncio.run(module._addon_json("/health")) is None

    _patch_urlopen(monkeypatch, [OSError("refused")])
    assert asyncio.run(module._addon_json("/health")) is None


def test_godot_scene_route_online_full_shape(monkeypatch):
    """在线全链：/health+/status+/context 三连 200；快照非 dict 项跳过。"""
    module = _fresh("mode_godot")
    responses = [
        _FakeResp(200, b'{"ok": true}'),
        _FakeResp(200, b'{"project": "demo"}'),
        _FakeResp(
            200,
            json.dumps(
                {
                    "scene_name": "main.tscn",
                    "selected_objects": ["Player"],
                    "selection_detail": [
                        "garbage",
                        {"name": "Player", "type": "CharacterBody2D", "path": "/root/Player"},
                    ],
                }
            ).encode("utf-8"),
        ),
    ]
    _patch_urlopen(monkeypatch, responses)
    body = _body_json(_call(module, "/ext/mode_godot/data/scene"))
    assert body["editor_online"] is True
    assert body["snapshot"]["project"] == "demo"
    assert body["snapshot"]["selected_objects"] == ["Player"]
    assert [n["name"] for n in body["snapshot"]["selection_detail"]] == ["Player"]


def test_godot_scene_route_online_but_snapshot_failed(monkeypatch):
    """探活通过但 /context 失败 → editor_online=true 且 snapshot=None（分开报）。"""
    module = _fresh("mode_godot")
    http_err = urllib.error.HTTPError(
        "http://x/context", 500, "ise", None, io.BytesIO(b"boom")
    )
    _patch_urlopen(
        monkeypatch,
        [_FakeResp(200, b'{"ok": true}'), http_err, http_err],
    )
    body = _body_json(_call(module, "/ext/mode_godot/data/scene"))
    assert body["editor_online"] is True
    assert body["snapshot"] is None
    assert "快照读取失败" in body["note"]


def test_godot_dispatch_error_triage():
    """godot 派发：抛错透传/缺任务 id（通道未注入面在 open_editor 侧已有）。"""
    module = _fresh("mode_godot")
    body = json.dumps({"instruction": "放大按钮"})

    module._set_provider("tool-executor", _OnceProvider(RuntimeError("executor 炸了")))
    out = _body_json(_call(module, "/ext/mode_godot/data/actions/dispatch", "POST", body))
    assert "task_submit 调用失败" in out["error"]

    module._set_provider("tool-executor", _OnceProvider({"data": {"ok": True}}))
    out = _body_json(_call(module, "/ext/mode_godot/data/actions/dispatch", "POST", body))
    assert "未返回任务 id" in out["error"]


# ── coding：reviews 信封兜底补位 ─────────────────────────────────────────────────


def test_coding_reviews_non_dict_non_list_envelope():
    """reviews 结果非 dict 非 list（裸标量）→ 空列表不崩。"""
    module = _fresh("mode_coding")

    async def _junk(payload):
        return "junk"

    module._set_provider("tool-executor", _junk)
    items = _body_json(_call(module, "/ext/mode_coding/data/reviews"))["items"]
    assert items == []


# ── messages 归一器兜底（godot/research/roleplay/writing） ────────────────────────


@pytest.mark.parametrize(
    "mode,route",
    [
        ("mode_godot", "/ext/mode_godot/data/messages"),
        ("mode_research", "/ext/mode_research/data/messages"),
        ("mode_roleplay", "/ext/mode_roleplay/data/messages"),
        ("mode_writing", "/ext/mode_writing/data/messages"),
    ],
)
def test_messages_normalizer_skips_non_dict_rows(mode, route):
    """messages 行含非 dict 元素 → 跳过不崩（四包各自 /data/messages 路由）。"""
    module = _fresh(mode)

    async def _msgs(pipeline_id, limit=None):
        return ["garbage", 42, {"role": "assistant", "content_preview": "正常行"}]

    module._set_provider("messages", _msgs)
    body = _body_json(_call(module, route, query={"pipeline_id": "p-1"}))
    assert [m["role"] for m in body["messages"]] == ["assistant"]


def test_godot_open_editor_raise_passthrough():
    """open_editor 通道抛错 → godot_run 调用失败如实透传。"""
    module = _fresh("mode_godot")
    module._set_provider("tool-executor", _OnceProvider(RuntimeError("宿主桥炸了")))
    out = _body_json(_call(module, "/ext/mode_godot/data/actions/open_editor", "POST", "{}"))
    assert "godot_run 调用失败" in out["error"] and "宿主桥炸了" in out["error"]


def test_planning_discussions_post_404():
    """discussions 只收 GET；POST → 404 信封。"""
    module = _fresh("mode_planning")
    result = _call(
        module, "/ext/mode_planning/data/discussions", "POST", raw_body="{}",
        query={"pipeline_id": "p-1"},
    )
    assert _status(result) == 404
