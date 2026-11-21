# @feature: FP-0.2.二 security_check 审批管理端点 | @ci: python-coverage
"""管道级免审批管理端点测试（撤销/查询/白名单/审计）。

security_check server.py http.handle 新增路由：
1. GET  /permission_mode/pipeline → 覆盖表条目；
2. DELETE /permission_mode/pipeline → 撤销条目（幂等）并落审计；
3. GET  /whitelist → 永久白名单条目；
4. POST /whitelist/remove → 移除条目（幂等）并落审计；
5. GET  /audit → 按 pipeline_id/session_id 过滤台账。
响应为内核 dispatcher 信封（base64 body），断言解包后的 JSON。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

pytestmark = pytest.mark.unit


def _load_server_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "security_check_server_approval_endpoints", str(Path(_THIS_DIR) / "server.py")
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    assert resp["success"] is True
    return json.loads(base64.b64decode(resp["data"]["body"]))


def _b64_body(data: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(data).encode("utf-8")).decode("ascii")


@pytest.fixture
def server_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    mod = _load_server_module()
    plugin_mod = sys.modules["plugin"]
    monkeypatch.setattr(plugin_mod, "_PERMISSION_MODES_FILE", str(tmp_path / "permission_modes.json"))
    plugin_mod._PIPELINE_PERMISSION_MODES.clear()
    plugin_mod._PERMANENT_WHITELIST.clear()
    yield mod
    plugin_mod._PIPELINE_PERMISSION_MODES.clear()
    plugin_mod._PERMANENT_WHITELIST.clear()


async def test_pipeline_override_list_and_revoke(server_mod: Any, tmp_path: Path) -> None:
    """GET 列出覆盖条目 → DELETE 撤销 → 再 GET 为空，且审计留痕。"""
    sys.modules["plugin"]._PIPELINE_PERMISSION_MODES["p1"] = "bypass"
    handle = server_mod.http_handle

    listed = _unwrap(await handle(path="/ext/pipeline_security_check/permission_mode/pipeline", method="GET"))
    assert listed == {"entries": {"p1": "bypass"}, "count": 1}

    revoked = _unwrap(
        await handle(
            path="/ext/pipeline_security_check/permission_mode/pipeline",
            method="DELETE",
            raw_body=_b64_body({"pipeline_id": "p1", "session_id": "s1", "user_id": "u1"}),
        )
    )
    assert revoked["revoked"] is True

    # 幂等：再次撤销不报错、revoked=False
    again = _unwrap(
        await handle(
            path="/ext/pipeline_security_check/permission_mode/pipeline",
            method="DELETE",
            raw_body=_b64_body({"pipeline_id": "p1"}),
        )
    )
    assert again["revoked"] is False

    empty = _unwrap(await handle(path="/ext/pipeline_security_check/permission_mode/pipeline", method="GET"))
    assert empty["entries"] == {}

    audit = _unwrap(
        await handle(
            path="/ext/pipeline_security_check/audit",
            method="GET",
            query={"pipeline_id": "p1"},
        )
    )
    events = audit["events"]
    assert len(events) == 1
    assert events[0]["event"] == "mode_switch"
    assert events[0]["action"] == "revoke"
    assert events[0]["source"] == "revoke_endpoint"


async def test_pipeline_override_revoke_requires_id(server_mod: Any) -> None:
    """缺 pipeline_id → 400。"""
    resp = await server_mod.http_handle(
        path="/ext/pipeline_security_check/permission_mode/pipeline",
        method="DELETE",
        raw_body=_b64_body({}),
    )
    assert resp["data"]["status"] == 400


async def test_whitelist_list_and_remove(server_mod: Any) -> None:
    """GET 列白名单 → POST 移除（幂等）+ 审计留痕。"""
    sys.modules["plugin"]._PERMANENT_WHITELIST.update({"bash:aaa", "bash:bbb"})
    handle = server_mod.http_handle

    listed = _unwrap(await handle(path="/ext/pipeline_security_check/whitelist", method="GET"))
    assert listed == {"entries": ["bash:aaa", "bash:bbb"], "count": 2}

    removed = _unwrap(
        await handle(
            path="/ext/pipeline_security_check/whitelist/remove",
            method="POST",
            raw_body=_b64_body({"signature": "bash:aaa", "pipeline_id": "p9"}),
        )
    )
    assert removed["removed"] is True

    again = _unwrap(
        await handle(
            path="/ext/pipeline_security_check/whitelist/remove",
            method="POST",
            raw_body=_b64_body({"signature": "bash:aaa"}),
        )
    )
    assert again["removed"] is False

    audit = _unwrap(
        await handle(
            path="/ext/pipeline_security_check/audit",
            method="GET",
            query={"pipeline_id": "p9"},
        )
    )
    assert [e["event"] for e in audit["events"]] == ["whitelist_remove"]
