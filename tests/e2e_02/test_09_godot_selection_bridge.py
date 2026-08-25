# @feature: FP-0.2.三 宿主接入 | @vision: V3 可嵌入 | @ci: python-e2e
"""
E2E 测试：Godot 选中引用桥经真内核的推送链路。

回归背景：内核 dispatcher（http_dispatcher.rs dispatch_http）把请求体
base64 编码后传给插件 http.handle；godot_context 服务端曾直接
json.loads(raw_body)，导致 Godot 宿主插件的一切推送恒 400
"invalid json"，选中引用链路整体断裂（单测直调 handle_push 无法暴露）。

验证：
  1. POST /ext/pipeline_godot_context/selection（明文 JSON over HTTP，
     内核内部转 base64 给插件）→ 200 {"status": "ok"}
  2. GET 快照（user 认证）→ connected=true 且 items 可见
  3. 非法 JSON 体 → 400（fail-fast 不静默）
  4. 引用清理流：DELETE 清除 → 同签名心跳不恢复 → 重新点选恢复

运行前提：内核 9100 已启动（pipeline_godot_context 默认启用）、
Godot 编辑器无需运行（推送由测试直接模拟）。

数据清理：teardown 推 offline 恢复快照空态——插件快照是进程内状态，
不清会让后续 e2e 聊天任务的管道注入 <reference> 消息污染断言。
"""
import json
import urllib.error
import urllib.request

import pytest
from e2e_helpers import http_delete_auth, http_get_with_auth, http_post_json

pytestmark = [pytest.mark.e2e]

_SELECTION_URL = "http://127.0.0.1:9100/ext/pipeline_godot_context/selection"


def _selection_payload() -> dict:
    return {
        "type": "selection",
        "engine": "godot",
        "engine_version": "4.7.1.stable.official",
        "project": "e2e-godot-bridge",
        "scene": {"name": "DemoMain", "path": "res://demo_main.tscn", "root": "Node3D", "node_count": 3},
        "items": [{"name": "E2EPlayer", "type": "CharacterBody3D", "path": "/root/DemoMain/E2EPlayer"}],
        "signature": "E2EPlayer@/root/DemoMain/E2EPlayer",
        "ts": 1,
    }


@pytest.fixture
def restore_offline():
    """测试后推 offline 恢复插件快照空态。"""
    yield
    http_post_json(_SELECTION_URL, {"type": "offline", "items": [], "signature": "", "scene": {}})


def test_selection_push_accepted_and_snapshot_visible(auth_token, restore_offline):
    """明文 JSON 推送（内核转 base64 给插件）→ 200；快照在线且可见选中。"""
    status, body, _ = http_post_json(_SELECTION_URL, _selection_payload())
    assert status == 200, f"push 失败: {body}"
    assert body == {"status": "ok"}

    status, snap, _ = http_get_with_auth(_SELECTION_URL, auth_token)
    assert status == 200
    assert snap["connected"] is True
    assert snap["items"][0]["name"] == "E2EPlayer"
    assert snap["scene"]["path"] == "res://demo_main.tscn"


def test_invalid_json_body_rejected_400(restore_offline):
    """非法 JSON 体 → 400 invalid json（不静默吞）。"""
    req = urllib.request.Request(
        _SELECTION_URL, data=b"not-json", method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
        body = json.loads(e.read().decode("utf-8"))
        assert body == {"error": "invalid json"}
    assert status == 400


def test_selection_clear_dismiss_flow(auth_token, restore_offline):
    """引用清理流：DELETE 清除 → 同签名心跳不恢复 → 重新点选（selection）恢复。"""
    status, body, _ = http_post_json(_SELECTION_URL, _selection_payload())
    assert status == 200
    assert body == {"status": "ok"}

    # 清除（前端点击清理入口）
    status, body, _ = http_delete_auth(_SELECTION_URL, auth_token)
    assert status == 200, f"DELETE 失败: {body}"
    assert body == {"status": "ok", "cleared": True}
    _, snap, _ = http_get_with_auth(_SELECTION_URL, auth_token)
    assert snap["items"] == []
    assert snap["signature"] == ""

    # Godot 节点仍选中：5s 心跳（同签名）不把引用带回来
    status, _, _ = http_post_json(_SELECTION_URL, {**_selection_payload(), "type": "heartbeat"})
    assert status == 200
    _, snap, _ = http_get_with_auth(_SELECTION_URL, auth_token)
    assert snap["items"] == []
    assert snap["connected"] is True

    # 用户重新点选同节点（type=selection）→ 引用恢复
    status, _, _ = http_post_json(_SELECTION_URL, _selection_payload())
    assert status == 200
    _, snap, _ = http_get_with_auth(_SELECTION_URL, auth_token)
    assert snap["items"][0]["name"] == "E2EPlayer"
