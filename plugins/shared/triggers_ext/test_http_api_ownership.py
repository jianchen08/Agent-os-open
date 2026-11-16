# @feature: FP-0.2.二 内部模块 manifest（trigger REST 归属过滤契约） | @ci: python-coverage
"""回归：触发器 REST 管理面跨租户收口（X6-F1 / P-58）。

契约：http_api 各 handler 消费内核权威注入的身份头
（x-agentos-tenant / x-agentos-user / x-agentos-role，http_dispatcher 匿名
剥离伪造头，handler 可见即可信）做归属过滤：

- 读面（list/get/stats）：admin 角色全见（运维面）；其余已认证用户仅见属主
  本人触发器；缺身份头 → 空集 / 404（fail-closed，绝不返回全量）；
- 写面（update/delete/enable/disable/fire）：非属主且非 admin → 显式 403
  （协议错误信封，真实状态到达前端，不走 success:false 的 502 重放面）；
- 归属键 = 触发器 metadata 属主字段（user_id / tenant_id）；P-63 存量
  （metadata 缺 user_id）归属不可判 → 对非 admin 不可见不可操作；
- create：REST 创建路径把调用者身份写进 metadata 属主字段
  （user_id 已由工具落，tenant_id 由 REST 面补章），内核身份头权威优先，
  Bearer 自解析仅作头缺失时的回退。

测试走 handle_http_dispatch 端到端（与 X4-F1 test_routes_identity_filter
同款风格），触发器经 TriggerManager 单例直接播种。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/triggers_ext/
_SHARED_ROOT = os.path.abspath(os.path.join(str(_PLUGIN_DIR), ".."))
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from triggers.types import TriggerConfig, TriggerStatus, TriggerType  # noqa: E402


def _load_module(mod_name: str, filename: str, evict_bare: tuple[str, ...] = ()) -> Any:
    """按唯一模块名装载插件内模块（逐出同名裸名缓存，防跨插件串扰）。

    与 test_http_api_create 同款自防御：装载前把本插件目录晋升到 sys.path
    最前并逐出同名裸名缓存，使裸名 import（from tool import ...）确定性地
    解析到本目录。
    """
    import importlib.util

    here = str(_PLUGIN_DIR)
    while here in sys.path:
        sys.path.remove(here)
    sys.path.insert(0, here)

    if mod_name in sys.modules:
        del sys.modules[mod_name]
    for bare in evict_bare:
        sys.modules.pop(bare, None)
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _decode_http(result: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """解包 http.handle ToolExecutionResult 信封 → (status, json_body)。

    只解协议信封（success:true + data 携真实 HTTP 状态）；success:false 信封
    （既有 404 路径）由调用方单独断言，不进本函数。
    """
    assert result["success"], f"协议错误信封必须 success:true：{result}"
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


# ── 身份头构造（内核注入形态：键已小写）───────────────────────────


def _h(tenant: str = "t-a", user: str = "u-a", role: str = "user") -> dict[str, str]:
    return {"x-agentos-tenant": tenant, "x-agentos-user": user, "x-agentos-role": role}


def _admin_h() -> dict[str, str]:
    return _h(tenant="t-admin", user="u-admin", role="admin")


@pytest.fixture
def api() -> Any:
    """装载 http_api 模块 + 清场触发器单例注册表（进程内共享，逐测试隔离）。"""
    module = _load_module(
        "triggers_ext_http_api_ownership", "http_api.py", ("tool", "http_api", "server")
    )
    mgr = module.get_trigger_manager()
    mgr._triggers.clear()
    yield module
    mgr._triggers.clear()


def _seed(
    module: Any,
    trigger_id: str,
    *,
    user_id: str | None = None,
    tenant_id: str | None = None,
    status: TriggerStatus = TriggerStatus.ACTIVE,
) -> TriggerConfig:
    """播种触发器（EVENT 类型 + 无事件名：不依赖时间/事件通道，测试窗口零触发）。

    user_id/tenant_id 均 None = 模拟 P-63 存量（metadata 无属主字段）。
    """
    cfg = TriggerConfig(
        trigger_id=trigger_id,
        name=f"seed-{trigger_id}",
        trigger_type=TriggerType.EVENT,
        event_name="no_such_event_in_unit_test",
        max_fires=5,
        message="seed",
        pipeline_id=f"pipe-{trigger_id}",
    )
    if user_id is not None or tenant_id is not None:
        cfg.metadata = {"user_id": user_id or "", "tenant_id": tenant_id or ""}
    mgr = module.get_trigger_manager()
    mgr.register(cfg)
    if status is not TriggerStatus.ACTIVE:  # register 恒置 ACTIVE，其余状态测试显式改写
        cfg.status = status
    return cfg


def _dispatch(
    module: Any,
    method: str,
    suffix: str,
    headers: dict[str, str] | None = None,
    raw_body: str = "",
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """http.handle 分发入口（与内核 dispatch_user_input 同构的端到端路径）。"""
    return _run(
        module.handle_http_dispatch(
            f"{module.PREFIX}{suffix}", method, raw_body, query or {}, headers or {}
        )
    )


def _list_ids(module: Any, headers: dict[str, str] | None) -> list[str]:
    status, body = _decode_http(_dispatch(module, "GET", "", headers))
    assert status == 200
    return [item["trigger_id"] for item in body["items"]]


# ══ 读面：list ═══════════════════════════════════════════════════


class TestListVisibility:
    """GET /triggers：归属过滤矩阵（admin 全见 / 属主本人 / fail-closed）。"""

    def test_cross_tenant_trigger_hidden_from_list(self, api: Any) -> None:
        """租户 B 列表看不到租户 A 的触发器（X6 动态实证的静态收口）。"""
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        _seed(api, "trigger_event_bbbbbbbbbb", user_id="u-b", tenant_id="t-b")
        ids = _list_ids(api, _h(tenant="t-b", user="u-b"))
        assert ids == ["trigger_event_bbbbbbbbbb"]

    def test_owner_unknown_hidden_from_non_admin(self, api: Any) -> None:
        """P-63 存量（metadata 缺属主）对非 admin 不可见（fail-closed 少显示）。"""
        _seed(api, "trigger_event_legacy0001")  # 无属主字段
        ids = _list_ids(api, _h(tenant="t-a", user="u-a"))
        assert ids == []

    def test_admin_sees_all_including_owner_unknown(self, api: Any) -> None:
        """admin 角色全见（运维面，对照 approval pending 端点同款设计）。"""
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        _seed(api, "trigger_event_legacy0001")
        ids = _list_ids(api, _admin_h())
        assert sorted(ids) == ["trigger_event_aaaaaaaaaa", "trigger_event_legacy0001"]

    def test_missing_identity_headers_empty_set(self, api: Any) -> None:
        """缺身份头（非内核认证分发）→ 空集，绝不返回全量。"""
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        header_cases: tuple[dict[str, str] | None, ...] = ({}, None)
        for headers in header_cases:
            status, body = _decode_http(_dispatch(api, "GET", "", headers))
            assert status == 200
            assert body == {"items": [], "total": 0}

    def test_status_filter_applies_within_visible_set(self, api: Any) -> None:
        """status 过滤与归属过滤叠加（可见集内再收敛，不因过滤扩大可见面）。"""
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        _seed(
            api, "trigger_event_aaaaaaaaab",
            user_id="u-a", tenant_id="t-a", status=TriggerStatus.PENDING,
        )
        status, body = _decode_http(
            _dispatch(api, "GET", "", _h(), query={"status": "pending"})
        )
        assert status == 200
        assert [i["trigger_id"] for i in body["items"]] == ["trigger_event_aaaaaaaaab"]


class TestStatsVisibility:
    """GET /triggers/stats：计数只覆盖调用方可见集。"""

    def test_stats_counts_own_triggers_only(self, api: Any) -> None:
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        _seed(api, "trigger_event_bbbbbbbbbb", user_id="u-b", tenant_id="t-b")
        status, body = _decode_http(_dispatch(api, "GET", "/stats", _h(tenant="t-a", user="u-a")))
        assert status == 200
        assert body["total"] == 1
        assert body["by_status"] == {"active": 1}

    def test_stats_missing_identity_headers_zeroed(self, api: Any) -> None:
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        status, body = _decode_http(_dispatch(api, "GET", "/stats", {}))
        assert status == 200
        assert body["total"] == 0

    def test_stats_admin_counts_all(self, api: Any) -> None:
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        _seed(api, "trigger_event_bbbbbbbbbb", user_id="u-b", tenant_id="t-b")
        status, body = _decode_http(_dispatch(api, "GET", "/stats", _admin_h()))
        assert status == 200
        assert body["total"] == 2


# ══ 读面：get（by-id 不可见 = 404）═══════════════════════════════


class TestGetVisibility:
    """GET /triggers/{id}：可见集外 by-id 一律 404（不泄露存在性）。"""

    def test_cross_tenant_get_hidden_as_404(self, api: Any) -> None:
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        result = _dispatch(api, "GET", "/trigger_event_aaaaaaaaaa", _h(tenant="t-b", user="u-b"))
        status, body = _decode_http(result)
        assert status == 404
        assert "not found" in body["error"]["message"]

    def test_owner_unknown_get_hidden_for_non_admin(self, api: Any) -> None:
        _seed(api, "trigger_event_legacy0001")
        result = _dispatch(api, "GET", "/trigger_event_legacy0001", _h())
        status, _body = _decode_http(result)
        assert status == 404

    def test_owner_gets_own_trigger(self, api: Any) -> None:
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        status, body = _decode_http(
            _dispatch(api, "GET", "/trigger_event_aaaaaaaaaa", _h(tenant="t-a", user="u-a"))
        )
        assert status == 200
        assert body["trigger"]["trigger_id"] == "trigger_event_aaaaaaaaaa"

    def test_admin_gets_any_trigger(self, api: Any) -> None:
        _seed(api, "trigger_event_legacy0001")
        status, body = _decode_http(
            _dispatch(api, "GET", "/trigger_event_legacy0001", _admin_h())
        )
        assert status == 200
        assert body["trigger"]["trigger_id"] == "trigger_event_legacy0001"

    def test_truly_missing_get_stays_error_envelope(self, api: Any) -> None:
        """真不存在（404）走既有 success:false 信封——既有行为零改动锚。"""
        result = _dispatch(api, "GET", "/trigger_event_nosuch0000", _h())
        assert result["success"] is False


# ══ 写面：非属主且非 admin → 显式 403 ════════════════════════════


class TestWriteGuard:
    """update/delete/enable/disable/fire：越权显式拒绝且零副作用。"""

    def test_cross_tenant_update_denied_and_unmutated(self, api: Any) -> None:
        cfg = _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        result = _dispatch(
            api, "PUT", "/trigger_event_aaaaaaaaaa",
            _h(tenant="t-b", user="u-b"), raw_body=json.dumps({"max_count": 99}),
        )
        status, _body = _decode_http(result)
        assert status == 403
        assert cfg.max_fires == 5  # 拒绝先于变更

    def test_cross_tenant_delete_denied_and_unremoved(self, api: Any) -> None:
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        result = _dispatch(
            api, "DELETE", "/trigger_event_aaaaaaaaaa", _h(tenant="t-b", user="u-b")
        )
        status, _body = _decode_http(result)
        assert status == 403
        assert api.get_trigger_manager().get("trigger_event_aaaaaaaaaa") is not None

    def test_cross_tenant_enable_denied_and_unmutated(self, api: Any) -> None:
        cfg = _seed(
            api, "trigger_event_aaaaaaaaaa",
            user_id="u-a", tenant_id="t-a", status=TriggerStatus.PENDING,
        )
        result = _dispatch(api, "POST", "/trigger_event_aaaaaaaaaa/enable", _h(tenant="t-b", user="u-b"))
        status, _body = _decode_http(result)
        assert status == 403
        assert cfg.status is TriggerStatus.PENDING

    def test_cross_tenant_disable_denied_and_unmutated(self, api: Any) -> None:
        cfg = _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        result = _dispatch(api, "POST", "/trigger_event_aaaaaaaaaa/disable", _h(tenant="t-b", user="u-b"))
        status, _body = _decode_http(result)
        assert status == 403
        assert cfg.status is TriggerStatus.ACTIVE

    def test_cross_tenant_fire_denied_and_uncounted(self, api: Any) -> None:
        cfg = _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        result = _dispatch(api, "POST", "/trigger_event_aaaaaaaaaa/trigger", _h(tenant="t-b", user="u-b"))
        status, _body = _decode_http(result)
        assert status == 403
        assert cfg.fire_count == 0  # fire_manually 先计数后注入——拒绝必须先于计数

    def test_owner_unknown_write_denied_for_non_admin(self, api: Any) -> None:
        """P-63 存量归属不可判：非 admin 删除 fail-closed 拒绝。"""
        _seed(api, "trigger_event_legacy0001")
        result = _dispatch(api, "DELETE", "/trigger_event_legacy0001", _h())
        status, _body = _decode_http(result)
        assert status == 403
        assert api.get_trigger_manager().get("trigger_event_legacy0001") is not None

    def test_missing_identity_headers_write_denied(self, api: Any) -> None:
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        result = _dispatch(api, "DELETE", "/trigger_event_aaaaaaaaaa", {})
        status, _body = _decode_http(result)
        assert status == 403


class TestWriteAllowed:
    """放行面正控（防过滤过宽）：属主本人 + admin 运维面。"""

    def test_owner_updates_own_trigger(self, api: Any) -> None:
        cfg = _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        result = _dispatch(
            api, "PUT", "/trigger_event_aaaaaaaaaa",
            _h(tenant="t-a", user="u-a"), raw_body=json.dumps({"max_count": 9}),
        )
        status, body = _decode_http(result)
        assert status == 200
        assert body["trigger"]["max_fires"] == 9
        assert cfg.max_fires == 9

    def test_owner_deletes_own_trigger(self, api: Any) -> None:
        _seed(api, "trigger_event_aaaaaaaaaa", user_id="u-a", tenant_id="t-a")
        status, body = _decode_http(
            _dispatch(api, "DELETE", "/trigger_event_aaaaaaaaaa", _h(tenant="t-a", user="u-a"))
        )
        assert status == 200
        assert body["deleted"] is True
        assert api.get_trigger_manager().get("trigger_event_aaaaaaaaaa") is None

    def test_admin_updates_others_trigger(self, api: Any) -> None:
        cfg = _seed(api, "trigger_event_bbbbbbbbbb", user_id="u-b", tenant_id="t-b")
        result = _dispatch(
            api, "PUT", "/trigger_event_bbbbbbbbbb",
            _admin_h(), raw_body=json.dumps({"max_count": 7}),
        )
        status, body = _decode_http(result)
        assert status == 200
        assert cfg.max_fires == 7

    def test_admin_fires_others_trigger(self, api: Any) -> None:
        cfg = _seed(api, "trigger_event_bbbbbbbbbb", user_id="u-b", tenant_id="t-b")
        result = _dispatch(api, "POST", "/trigger_event_bbbbbbbbbb/trigger", _admin_h())
        status, body = _decode_http(result)
        assert status == 200
        assert body["fired"] is True
        assert cfg.fire_count == 1


# ══ create：属主字段落 metadata（X6-F1 补章契约）══════════════════


class TestCreateOwnershipStamp:
    """POST /triggers：调用者身份写进 metadata 属主字段，身份头权威优先。"""

    def _create(self, api: Any, headers: dict[str, str] | None) -> tuple[int, dict[str, Any]]:
        body = {
            "trigger_type": "delay",
            "message": "到点了",
            "delay_seconds": 90,
            "pipeline_id": "pipe-own",
            # 注入对抗输入：body 伪造 user_id 不得覆盖权威身份
            "user_id": "u-victim",
        }
        result = _dispatch(api, "POST", "", headers, raw_body=json.dumps(body))
        return _decode_http(result)

    def test_create_stamps_user_and_tenant_from_headers(self, api: Any) -> None:
        status, body = self._create(api, _h(tenant="t-a", user="u-a"))
        assert status == 200
        cfg = api.get_trigger_manager().get(body["trigger"]["trigger_id"])
        assert cfg is not None
        assert cfg.metadata["user_id"] == "u-a"  # 身份头权威，body 伪造值不落地
        assert cfg.metadata["tenant_id"] == "t-a"

    def test_create_stamped_trigger_visible_only_to_owner(self, api: Any) -> None:
        """新建触发器立即可被归属过滤消费（属主可见、他租户不可见）。"""
        _status, body = self._create(api, _h(tenant="t-a", user="u-a"))
        trigger_id = body["trigger"]["trigger_id"]
        assert trigger_id in _list_ids(api, _h(tenant="t-a", user="u-a"))
        assert trigger_id not in _list_ids(api, _h(tenant="t-b", user="u-b"))
        assert trigger_id in _list_ids(api, _admin_h())

    def test_create_bearer_fallback_stamps_user_without_tenant(self, api: Any) -> None:
        """身份头缺失（非内核分发路径）回退 Bearer 自解析：user 落章、tenant 缺省。"""
        import time as _time

        payload = f"access:u-bearer:tester:{int(_time.time()) + 3600}"
        token = base64.b64encode(payload.encode()).decode().rstrip("=")
        status, body = self._create(api, {"authorization": f"Bearer {token}"})
        assert status == 200
        cfg = api.get_trigger_manager().get(body["trigger"]["trigger_id"])
        assert cfg is not None
        assert cfg.metadata["user_id"] == "u-bearer"
        assert cfg.metadata.get("tenant_id", "") == ""

    def test_create_without_any_identity_maps_to_401(self, api: Any) -> None:
        """无身份头且无有效 Bearer → 401（既有契约保持）。"""
        result = _dispatch(api, "POST", "", {})
        status, _body = _decode_http(result)
        assert status == 401
