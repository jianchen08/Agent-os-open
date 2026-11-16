# @feature: FP-0.2.二 内部模块 manifest（trigger REST 状态三字段可见性） | @ci: python-coverage
"""回归：触发器状态三字段读面可见性（提案 P-30 / 三清单 D26）。

三清单判定（D26）：triggers_ext 读面在位（pages + stats 端点），真缺 =
三字段（是否在跑 / 下次何时 / 最近结果）可见性。本文件锁 REST 读面契约：

- 「最近触发」``last_fire_time``：检查循环三处触发路径均写
  ``metadata["last_fire_time"]``，REST list 序列化须平铺到条目顶层
  （TableWidget 列取值 row[col.key] 不支持嵌套路径，metadata 深键不可列）；
- 手动触发（POST /trigger，列表行操作主路径）同款记录 last_fire_time——
  检查循环记、手动触发不记会使可见列在主路径操作后显示陈旧值；
- 「下次触发」``scheduled_at``：to_state_dict 既有顶层字段，锁定其随
  SCHEDULED 类型出现在 list 条目（数据流既有，防回归）；
- 既有端点契约零破坏（只增不删）：list 条目既有键原样保留。

测试走 handle_http_dispatch 端到端（与 test_http_api_ownership 同款风格），
触发器经 TriggerManager 单例直接播种。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/triggers_ext/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from triggers.types import TriggerConfig, TriggerStatus, TriggerType  # noqa: E402


def _load_module(mod_name: str, filename: str, evict_bare: tuple[str, ...] = ()) -> Any:
    """按唯一模块名装载插件内模块（逐出同名裸名缓存，防跨插件串扰）。"""
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
    """解包 http.handle ToolExecutionResult 信封 → (status, json_body)。"""
    assert result["success"], f"协议错误信封必须 success:true：{result}"
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


_HEADERS = {
    "x-agentos-tenant": "t-a",
    "x-agentos-user": "u-a",
    "x-agentos-role": "user",
}


@pytest.fixture
def api() -> Any:
    """装载 http_api 模块 + 清场触发器单例注册表（进程内共享，逐测试隔离）。"""
    module = _load_module(
        "triggers_ext_http_api_status_visibility", "http_api.py", ("tool", "http_api", "server")
    )
    mgr = module.get_trigger_manager()
    mgr._triggers.clear()
    yield module
    mgr._triggers.clear()


def _seed(
    module: Any,
    trigger_id: str,
    *,
    trigger_type: TriggerType = TriggerType.EVENT,
    scheduled_at: datetime.datetime | None = None,
    metadata: dict[str, Any] | None = None,
) -> TriggerConfig:
    """播种触发器（无事件名/非 ACTIVE 到期路径：测试窗口零自动触发）。"""
    cfg = TriggerConfig(
        trigger_id=trigger_id,
        name=f"seed-{trigger_id}",
        trigger_type=trigger_type,
        event_name="no_such_event_in_unit_test",
        scheduled_at=scheduled_at,
        max_fires=5,
        message="seed",
        pipeline_id=f"pipe-{trigger_id}",
    )
    if metadata is not None:
        cfg.metadata = dict(metadata)
    cfg.metadata.setdefault("user_id", "u-a")
    cfg.metadata.setdefault("tenant_id", "t-a")
    mgr = module.get_trigger_manager()
    mgr.register(cfg)
    if cfg.status is not TriggerStatus.ACTIVE:
        cfg.status = TriggerStatus.ACTIVE
    return cfg


def _dispatch(
    module: Any,
    method: str,
    suffix: str,
    headers: dict[str, str] | None = None,
    raw_body: str = "",
) -> dict[str, Any]:
    """http.handle 分发入口（端到端路径）。"""
    return _run(
        module.handle_http_dispatch(
            f"{module.PREFIX}{suffix}", method, raw_body, {}, headers or _HEADERS
        )
    )


def _list_items(module: Any) -> list[dict[str, Any]]:
    status, body = _decode_http(_dispatch(module, "GET", ""))
    assert status == 200
    return body["items"]


def _single_item(module: Any, trigger_id: str) -> dict[str, Any]:
    items = _list_items(module)
    assert len(items) == 1, f"应恰见 1 条触发器，实得 {len(items)}"
    assert items[0]["trigger_id"] == trigger_id
    return items[0]


# ══ 「最近结果」：last_fire_time 平铺可见 ═════════════════════════


class TestLastFireTimeVisibility:
    """metadata.last_fire_time → REST list 条目顶层平铺（TableWidget 可列）。"""

    def test_list_item_exposes_last_fire_time_flat(self, api: Any) -> None:
        """检查循环写入的 metadata.last_fire_time 平铺为条目顶层键。"""
        fired_at = "2026-11-04T10:00:00+00:00"
        _seed(
            api,
            "trigger_event_p30lastrun",
            metadata={"last_fire_time": fired_at},
        )
        item = _single_item(api, "trigger_event_p30lastrun")
        assert item["last_fire_time"] == fired_at

    def test_missing_last_fire_time_serializes_null(self, api: Any) -> None:
        """未触发过（metadata 缺 last_fire_time）→ 顶层键存在且为 None。"""
        _seed(api, "trigger_event_p30neverfire")
        item = _single_item(api, "trigger_event_p30neverfire")
        assert "last_fire_time" in item
        assert item["last_fire_time"] is None

    def test_manual_fire_records_last_fire_time(self, api: Any) -> None:
        """手动触发（POST /trigger，行操作主路径）后 last_fire_time 非空。

        检查循环三处触发路径均记 last_fire_time；手动触发不记会使新列在
        主路径操作后显示陈旧值（可见性诚实性）。
        """
        cfg = _seed(api, "trigger_event_p30manual")
        assert cfg.metadata.get("last_fire_time") is None

        status, _ = _decode_http(
            _dispatch(api, "POST", f"/{cfg.trigger_id}/trigger")
        )
        assert status == 200

        item = _single_item(api, "trigger_event_p30manual")
        assert item["last_fire_time"] is not None


# ══ 「下次何时」：scheduled_at 数据流锁定 ═════════════════════════


class TestScheduledAtVisibility:
    """scheduled_at 随 SCHEDULED 类型出现在 list 条目顶层（既有数据流防回归）。"""

    def test_scheduled_at_present_for_scheduled_type(self, api: Any) -> None:
        """SCHEDULED 触发器的 scheduled_at 顶层键在位且值正确。"""
        when = datetime.datetime(2026, 11, 5, 8, 30, 0, tzinfo=datetime.UTC)
        _seed(
            api,
            "trigger_scheduled_p30next",
            trigger_type=TriggerType.SCHEDULED,
            scheduled_at=when,
        )
        item = _single_item(api, "trigger_scheduled_p30next")
        assert item["scheduled_at"] == when.isoformat()

    def test_scheduled_at_null_for_event_type(self, api: Any) -> None:
        """EVENT 触发器无 scheduled_at（asdict None 原样）——列显示空值。"""
        _seed(api, "trigger_event_p30nosched")
        item = _single_item(api, "trigger_event_p30nosched")
        assert item["scheduled_at"] is None


# ══ 既有端点契约零破坏（只增不删） ═══════════════════════════════


class TestListContractUnbroken:
    """list 条目既有键原样保留（平铺只增不删）。"""

    def test_existing_item_keys_preserved(self, api: Any) -> None:
        _seed(api, "trigger_event_p30contract")
        item = _single_item(api, "trigger_event_p30contract")
        for key in (
            "trigger_id",
            "name",
            "trigger_type",
            "status",
            "pipeline_id",
            "message",
            "fire_count",
            "max_fires",
            "metadata",
        ):
            assert key in item, f"既有契约键 {key} 不得缺失"
        assert item["status"] == "active"
        assert item["trigger_type"] == "event"
        assert item["metadata"]["user_id"] == "u-a"
