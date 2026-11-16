# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""B2 配置单一真值：执法面读 manifest 注入，设置页 PUT 后执法面随之。

问题（审查总册 P0-3 / 同型排查 2026-10-14 S1）：执法面曾读不存在的仓库级
cost_control.yaml（生效全默认 daily=10¹²≈不设限），前端设置页读写 manifest
config_files[id=cost] 声明的设置页真值 yaml（daily=10⁶），PUT 对执法零影响
——两端点数字互相矛盾。

修复后契约（本文件钉死）：
- 执法面配置 = manifest ``config_files[id=cost]`` 注入（内核每调用现算携带，
  SDK ``on_config_changed`` 感知；isolation 式 mtime 轮询不再需要）；
- 阈值刻度换算单点：设置页百分(0-100) → 执法面小数(0-1)；
- 默认表单点：constants = 设置页 ``_DEFAULT_COST_CONTROL`` 同值同语义
  （不再有 10¹² vs 10⁶ 两套默认）；
- 两端点一致：``/ext/cost_control/config``（展平执法面）与
  ``/ext/cost_control/config/cost-control``（设置页全文）限额四字段严格相等，
  阈值差固定 100 倍刻度（前端 CostConfigResponse 契约钉小数形态，不可改）；
- PUT → 执法面真实生效：写入小限额后 ``check_budget`` 真实拒绝（行为断言，
  非数字断言）；换配置只换限额对象、不重建 manager（用量账本保留）。

外部依赖替身：设置页 YAML 文件重定向到 tmp_path（真实仓库文件不落笔）；
内核每调用注入用 SDK 分发单点 ``_apply_call_config``（内核↔插件契约接缝）。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
while str(_PLUGIN_DIR) in sys.path:
    sys.path.remove(str(_PLUGIN_DIR))
sys.path.insert(0, str(_PLUGIN_DIR))

# 裸名逐出：constants/config/exceptions 等平铺名遍布各插件，车道共跑时被别的
# 插件占位会让平铺 import 打到错误实现。
for _bare in ("constants", "exceptions", "budget_manager", "tokenizer", "config"):
    sys.modules.pop(_bare, None)

from constants import CostControl  # noqa: E402

from config import CostControlConfig, load_cost_control_config  # noqa: E402

# 共享根（bootstrap_plugin 依赖的 db_read/http_json/project_root 平铺模块）
_SHARED_ROOT = str(_PLUGIN_DIR.parent)
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)


_MOD_NAME = "cost_config_single_source_server"


def _load_server() -> Any:
    """动态加载 cost_control/server.py（唯一模块名，隔离模块级单例）。"""
    if _MOD_NAME in sys.modules:
        del sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


def _run(coro: Any) -> Any:
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    assert result["success"], result
    resp = result["data"]
    return resp["status"], json.loads(base64.b64decode(resp["body"]))


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# 设置页表面形态（manifest config_files[id=cost] 注入 = yaml 内容），两组有区分度
# 输入：限额数量级不同、阈值不同、保护位不同。
_PAYLOAD_A: dict[str, Any] = {
    "enabled": True,
    "global_config": {
        "daily_token_limit": 8888,
        "monthly_token_limit": 9_000_000,
        "per_task_token_limit": 4000,
        "per_session_token_limit": 6000,
    },
    "alerts": {"warning_threshold": 50, "critical_threshold": 70, "exhausted_threshold": 95},
    "protection": {
        "auto_save_at_warning": False,
        "auto_pause_at_critical": False,
        "auto_stop_at_exhausted": False,
    },
}
_PAYLOAD_B: dict[str, Any] = {
    "enabled": True,
    "global_config": {
        "daily_token_limit": 123456,
        "monthly_token_limit": 77_000_000,
        "per_task_token_limit": 11111,
        "per_session_token_limit": 22222,
    },
    "alerts": {"warning_threshold": 60, "critical_threshold": 85, "exhausted_threshold": 100},
    "protection": {
        "auto_save_at_warning": True,
        "auto_pause_at_critical": True,
        "auto_stop_at_exhausted": True,
    },
}


@pytest.fixture
def server(tmp_path: Path) -> Any:
    """server 模块 + 设置页 yaml 重定向 tmp（真实仓库文件不落笔）。"""
    mod = _load_server()
    mod._COST_CONTROL_YAML = tmp_path / "cost_control.yaml"
    yield mod
    mod._budget_manager = None


# ── 执法面播种：on_load 读 manifest 注入 ─────────────────────────────────


class TestOnLoadSeedsFromInjection:
    @pytest.mark.parametrize("payload", [_PAYLOAD_A, _PAYLOAD_B], ids=["payload-a", "payload-b"])
    def test_on_load_enforcement_follows_injected_config(self, server: Any, payload: dict[str, Any]) -> None:
        """on_load 以注入视图播种执法面（两组区分度输入）。"""
        server.plugin._injected_config = {"cost": payload}

        _run(server._on_load({}))

        assert server._budget_manager is not None
        gb = server._budget_manager.config.global_budget
        assert gb.daily_token_limit == payload["global_config"]["daily_token_limit"]
        assert gb.monthly_token_limit == payload["global_config"]["monthly_token_limit"]
        assert gb.per_task_token_limit == payload["global_config"]["per_task_token_limit"]
        assert gb.per_session_token_limit == payload["global_config"]["per_session_token_limit"]
        # 百分 → 小数换算单点
        assert server._budget_manager.config.alerts.warning_threshold == pytest.approx(
            payload["alerts"]["warning_threshold"] / 100
        )
        assert server._budget_manager.config.alerts.exhausted_threshold == pytest.approx(
            payload["alerts"]["exhausted_threshold"] / 100
        )
        assert server._budget_manager.config.protection.auto_save_at_warning is payload["protection"]["auto_save_at_warning"]

    def test_injected_view_reaches_flattened_endpoint(self, server: Any) -> None:
        """展平执法面端点返回注入值（不再是无 yaml 时的 10¹² 默认）。"""
        server.plugin._injected_config = {"cost": _PAYLOAD_A}
        _run(server._on_load({}))

        status, body = _decode(
            _run(server.http_handle(path="/ext/cost_control/config", method="GET"))
        )

        assert status == 200
        assert body["daily_token_limit"] == 8888
        assert body["monthly_token_limit"] == 9_000_000
        assert body["warning_threshold"] == pytest.approx(0.50)

    def test_missing_cost_namespace_falls_back_to_aligned_defaults(self, server: Any) -> None:
        """内核未携带 cost 命名空间 → 默认表（与设置页同值，非 10¹² 旧默认）。"""
        server.plugin._injected_config = {}
        _run(server._on_load({}))

        assert server._budget_manager is not None
        assert server._budget_manager.config.global_budget.daily_token_limit == 1_000_000
        assert server._budget_manager.config.alerts.warning_threshold == pytest.approx(0.70)

    def test_non_dict_injection_falls_back_to_defaults(self, server: Any) -> None:
        """注入视图整体非 dict（旧内核不携带）→ 默认表，on_load 不崩。

        经 server 模块自带的 load 构造期望值（平铺裸名多份 config 模块并存的
        车道环境下，跨模块 pydantic 模型相等不可靠，见 branch_gaps 逐出注释）。
        """
        server.plugin._injected_config = "not-a-dict"
        _run(server._on_load({}))

        assert server._budget_manager is not None
        assert server._budget_manager.config == server.load_cost_control_config(None)

    def test_config_change_hook_before_load_is_safe(self, server: Any) -> None:
        """on_config_changed 先于 on_load（manager 未装配）→ 只落单例不崩。"""
        _run(server.plugin._apply_call_config({"cost": _PAYLOAD_A}))

        from config import get_cost_control_config

        assert get_cost_control_config().global_budget.daily_token_limit == 8888
        assert server._budget_manager is None


# ── 验收主断言：设置页 PUT → 执法面随之 → 两端点一致 ────────────────────


class TestSettingsPutReachesEnforcement:
    def test_put_updates_enforcement_and_both_endpoints_agree(self, server: Any) -> None:
        """PUT 新配置后：设置页端点回新值、展平执法面端点随之、限额严格相等。"""
        server.plugin._injected_config = {"cost": _PAYLOAD_A}
        _run(server._on_load({}))

        updated = json.loads(json.dumps(_PAYLOAD_B))
        updated["global_config"]["daily_token_limit"] = 654321
        status, echo = _decode(
            _run(
                server.http_handle(
                    path="/ext/cost_control/config/cost-control",
                    method="PUT",
                    raw_body=_b64(json.dumps(updated)),
                )
            )
        )
        assert status == 200
        assert echo["global_config"]["daily_token_limit"] == 654321

        # 内核下一次调用携带新注入视图（mtime 变更 → full_config 重读 → SDK 分发单点）
        _run(server.plugin._apply_call_config({"cost": updated}))

        _, settings_view = _decode(
            _run(server.http_handle(path="/ext/cost_control/config/cost-control", method="GET"))
        )
        _, enforcement_view = _decode(
            _run(server.http_handle(path="/ext/cost_control/config", method="GET"))
        )

        # 两端点限额四字段严格相等（历史矛盾点：10⁶ vs 10¹²）
        assert settings_view["global_config"]["daily_token_limit"] == 654321
        assert enforcement_view["daily_token_limit"] == 654321
        assert enforcement_view["monthly_token_limit"] == settings_view["global_config"]["monthly_token_limit"]
        assert enforcement_view["per_task_token_limit"] == settings_view["global_config"]["per_task_token_limit"]
        assert enforcement_view["per_session_token_limit"] == settings_view["global_config"]["per_session_token_limit"]
        # 阈值：设置页百分 ↔ 执法面小数，固定 100 倍刻度换算
        assert enforcement_view["warning_threshold"] == pytest.approx(
            settings_view["alerts"]["warning_threshold"] / 100
        )
        assert enforcement_view["critical_threshold"] == pytest.approx(
            settings_view["alerts"]["critical_threshold"] / 100
        )
        assert enforcement_view["auto_stop_at_exhausted"] is settings_view["protection"]["auto_stop_at_exhausted"]

    @pytest.mark.parametrize("tiny_limit", [1000, 2000], ids=["limit-1000", "limit-2000"])
    def test_put_enforcement_actually_rejects(self, server: Any, tiny_limit: int) -> None:
        """行为断言：PUT 小限额后 check_budget 真实拒绝（两组区分度边界）。"""
        server.plugin._injected_config = {"cost": _PAYLOAD_A}
        _run(server._on_load({}))

        updated = json.loads(json.dumps(_PAYLOAD_B))
        updated["global_config"]["daily_token_limit"] = tiny_limit
        _run(
            server.http_handle(
                path="/ext/cost_control/config/cost-control",
                method="PUT",
                raw_body=_b64(json.dumps(updated)),
            )
        )
        _run(server.plugin._apply_call_config({"cost": updated}))

        result = _run(server.cost_control_check_budget(estimated_tokens=tiny_limit + 1))
        assert result["allowed"] is False
        assert result["error"]["details"]["quota_type"] == "daily"
        # 边界内仍放行（限额值本身生效，而非一刀切拒绝）
        assert _run(server.cost_control_check_budget(estimated_tokens=tiny_limit))["allowed"] is True

    def test_config_swap_preserves_usage_ledger(self, server: Any) -> None:
        """换配置只换限额对象、不重建 manager——已记账用量在 PUT 后保留。"""
        server.plugin._injected_config = {"cost": _PAYLOAD_A}
        _run(server._on_load({}))
        _run(server.cost_control_record_usage(tokens=500, model="test-model"))

        _run(server.plugin._apply_call_config({"cost": _PAYLOAD_B}))

        assert server._budget_manager is not None
        stats = server._budget_manager.get_usage_statistics()
        assert stats["global"]["daily_tokens"] == 500
        assert stats["global"]["daily_limit"] == _PAYLOAD_B["global_config"]["daily_token_limit"]

    def test_two_endpoints_agree_on_pure_defaults(self, server: Any) -> None:
        """无注入且无 yaml 文件：两端点同落对齐后的默认表（历史 10¹² vs 10⁶ 矛盾点）。"""
        server.plugin._injected_config = {}
        _run(server._on_load({}))

        _, settings_view = _decode(
            _run(server.http_handle(path="/ext/cost_control/config/cost-control", method="GET"))
        )
        _, enforcement_view = _decode(
            _run(server.http_handle(path="/ext/cost_control/config", method="GET"))
        )

        assert settings_view["global_config"]["daily_token_limit"] == enforcement_view["daily_token_limit"]
        assert settings_view["global_config"]["monthly_token_limit"] == enforcement_view["monthly_token_limit"]
        assert enforcement_view["warning_threshold"] == pytest.approx(
            settings_view["alerts"]["warning_threshold"] / 100
        )


# ── 默认表单点：constants = 设置页 _DEFAULT_COST_CONTROL 同值 ────────────


class TestDefaultTableAlignment:
    def test_config_defaults_equal_settings_default_table(self, server: Any) -> None:
        """执法面默认模型 ≡ 设置页缺省回落表（限额同值、阈值 100 倍刻度同语义）。"""
        settings_default = server._DEFAULT_COST_CONTROL
        cfg = CostControlConfig()

        assert cfg.global_budget.daily_token_limit == settings_default["global_config"]["daily_token_limit"]
        assert cfg.global_budget.monthly_token_limit == settings_default["global_config"]["monthly_token_limit"]
        assert cfg.global_budget.per_task_token_limit == settings_default["global_config"]["per_task_token_limit"]
        assert cfg.global_budget.per_session_token_limit == settings_default["global_config"]["per_session_token_limit"]
        assert cfg.alerts.warning_threshold * 100 == settings_default["alerts"]["warning_threshold"]
        assert cfg.alerts.critical_threshold * 100 == settings_default["alerts"]["critical_threshold"]
        assert cfg.alerts.exhausted_threshold * 100 == settings_default["alerts"]["exhausted_threshold"]
        assert cfg.protection.auto_save_at_warning is settings_default["protection"]["auto_save_at_warning"]
        assert cfg.protection.auto_pause_at_critical is settings_default["protection"]["auto_pause_at_critical"]
        assert cfg.protection.auto_stop_at_exhausted is settings_default["protection"]["auto_stop_at_exhausted"]

    def test_constants_are_the_settings_scale_single_table(self) -> None:
        """constants 默认表与设置页字面值同源（不再有 10¹² 旧默认）。"""
        assert CostControl.DAILY_TOKEN_LIMIT == 1_000_000
        assert CostControl.MONTHLY_TOKEN_LIMIT == 30_000_000
        assert CostControl.PER_TASK_TOKEN_LIMIT == 200_000
        assert CostControl.PER_SESSION_TOKEN_LIMIT == 500_000
        assert CostControl.WARNING_THRESHOLD_PERCENT == 70
        assert CostControl.CRITICAL_THRESHOLD_PERCENT == 90
        assert CostControl.EXHAUSTED_THRESHOLD_PERCENT == 100

    @pytest.mark.parametrize(
        ("injected", "label"),
        [(None, "none"), ({}, "empty-dict"), ({"alerts": {}}, "partial-empty")],
    )
    def test_load_without_injection_falls_back_to_defaults(
        self, injected: dict[str, Any] | None, label: str
    ) -> None:
        """注入缺失/空/全缺省段 → 默认模型（load 纯函数面，无 yaml 读取）。"""
        cfg = load_cost_control_config(injected)
        assert cfg == CostControlConfig(), label
