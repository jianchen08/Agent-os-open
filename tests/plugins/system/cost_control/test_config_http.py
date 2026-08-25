# @feature: FP-0.2.可观测性 | @ci: python-coverage
"""cost_control 插件 YAML 配置读写端点测试（channel_api 自持承接）。

覆盖 /ext/cost_control/config/cost-control GET/PUT（原 /ext/channel_api/config/
cost-control，源 routes_config.py cost-control 段）：
1. GET 文件缺失 → 默认值（global_config/alerts/protection/enabled 嵌套形态对齐
   frontend CostControlConfigResponse）
2. GET 文件存在 → 原文语义（yaml.safe_load 全文）
3. PUT 全文覆写 → 落盘内容与响应回显一致
4. PUT 覆盖既有内容（写后读）
5. PUT 空 body / 空对象 / 非法 JSON → 400
6. 未匹配路由 → 404
7. BudgetManager 未初始化（on_load 未跑）时配置端点照常工作（先于初始化守卫分发）
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "cost_control"


def _load_server() -> Any:
    """动态加载 cost_control/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "cost_control_config_test_server",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cost_control_config_test_server"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(server: Any, path: str, method: str = "GET", raw_body: str = "") -> dict[str, Any]:
    return _run(server.http_handle(path=path, method=method, raw_body=raw_body))


def _decode(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


_SAMPLE_CONFIG = {
    "enabled": True,
    "global_config": {
        "daily_token_limit": 2000000,
        "monthly_token_limit": 40000000,
        "per_task_token_limit": 100000,
        "per_session_token_limit": 250000,
    },
    "alerts": {"warning_threshold": 80, "critical_threshold": 95, "exhausted_threshold": 100},
    "protection": {
        "auto_save_at_warning": False,
        "auto_pause_at_critical": True,
        "auto_stop_at_exhausted": True,
    },
}


# ── GET：默认值 / 文件内容 ────────────────────────────────────────────────


def test_get_returns_defaults_when_file_missing(server: Any, tmp_path: Path) -> None:
    server._COST_CONTROL_YAML = tmp_path / "cost_control.yaml"  # 不存在

    status, body = _decode(_call(server, "/ext/cost_control/config/cost-control"))

    assert status == 200
    assert body["enabled"] is True
    assert body["global_config"]["daily_token_limit"] == 1000000
    assert body["global_config"]["per_session_token_limit"] == 500000
    assert body["alerts"]["warning_threshold"] == 70
    assert body["protection"]["auto_stop_at_exhausted"] is True


def test_get_returns_file_content_when_exists(server: Any, tmp_path: Path) -> None:
    yaml_path = tmp_path / "cost_control.yaml"
    yaml_path.write_text(yaml.safe_dump(_SAMPLE_CONFIG, allow_unicode=True), encoding="utf-8")
    server._COST_CONTROL_YAML = yaml_path

    status, body = _decode(_call(server, "/ext/cost_control/config/cost-control"))

    assert status == 200
    assert body == _SAMPLE_CONFIG


def test_get_returns_defaults_for_non_dict_file(server: Any, tmp_path: Path) -> None:
    yaml_path = tmp_path / "cost_control.yaml"
    yaml_path.write_text("- just\n- a\n- list\n", encoding="utf-8")
    server._COST_CONTROL_YAML = yaml_path

    status, body = _decode(_call(server, "/ext/cost_control/config/cost-control"))

    assert status == 200
    assert body["global_config"]["daily_token_limit"] == 1000000  # 非 dict → 兜底默认


# ── PUT：全文覆写 ─────────────────────────────────────────────────────────


def test_put_writes_full_config(server: Any, tmp_path: Path) -> None:
    yaml_path = tmp_path / "cost_control.yaml"
    server._COST_CONTROL_YAML = yaml_path

    status, body = _decode(
        _call(server, "/ext/cost_control/config/cost-control", "PUT", _b64(json.dumps(_SAMPLE_CONFIG)))
    )

    assert status == 200
    assert body == _SAMPLE_CONFIG  # 响应回显写入数据（对齐原 save_cost_control_config）
    written = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    assert written == _SAMPLE_CONFIG


def test_put_overwrites_existing_content(server: Any, tmp_path: Path) -> None:
    yaml_path = tmp_path / "cost_control.yaml"
    yaml_path.write_text(yaml.safe_dump(_SAMPLE_CONFIG), encoding="utf-8")
    server._COST_CONTROL_YAML = yaml_path

    updated = dict(_SAMPLE_CONFIG)
    updated["alerts"] = {"warning_threshold": 60, "critical_threshold": 85, "exhausted_threshold": 100}
    status, body = _decode(
        _call(server, "/ext/cost_control/config/cost-control", "PUT", _b64(json.dumps(updated)))
    )

    assert status == 200
    assert body == updated
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8")) == updated


def test_put_plain_text_body_accepted(server: Any, tmp_path: Path) -> None:
    """非 base64 明文 JSON body（内核透传形态兼容）。"""
    yaml_path = tmp_path / "cost_control.yaml"
    server._COST_CONTROL_YAML = yaml_path

    status, _ = _decode(
        _call(server, "/ext/cost_control/config/cost-control", "PUT", json.dumps(_SAMPLE_CONFIG))
    )

    assert status == 200
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8")) == _SAMPLE_CONFIG


def test_put_empty_body_rejected(server: Any, tmp_path: Path) -> None:
    server._COST_CONTROL_YAML = tmp_path / "cost_control.yaml"

    status, body = _decode(_call(server, "/ext/cost_control/config/cost-control", "PUT"))

    assert status == 400
    assert "error" in body
    assert not (tmp_path / "cost_control.yaml").exists()


def test_put_invalid_json_rejected(server: Any, tmp_path: Path) -> None:
    server._COST_CONTROL_YAML = tmp_path / "cost_control.yaml"

    status, body = _decode(
        _call(server, "/ext/cost_control/config/cost-control", "PUT", _b64("{not json"))
    )

    assert status == 400
    assert "error" in body


# ── 分发层边界 ────────────────────────────────────────────────────────────


def test_unknown_route_404(server: Any) -> None:
    from budget_manager import BudgetManager

    from config import CostControlConfig

    server._budget_manager = BudgetManager(config=CostControlConfig())  # 过初始化守卫

    status, body = _decode(_call(server, "/ext/cost_control/config/nope"))

    assert status == 404
    assert body.get("error") == "not found"


def test_config_endpoints_work_without_budget_manager(server: Any, tmp_path: Path) -> None:
    """BudgetManager 未初始化（on_load 未跑）时配置读写照常（先于守卫分发）。"""
    server._budget_manager = None
    yaml_path = tmp_path / "cost_control.yaml"
    server._COST_CONTROL_YAML = yaml_path

    status, body = _decode(_call(server, "/ext/cost_control/config/cost-control"))
    assert status == 200
    assert body["enabled"] is True

    status, _ = _decode(
        _call(server, "/ext/cost_control/config/cost-control", "PUT", _b64(json.dumps(_SAMPLE_CONFIG)))
    )
    assert status == 200
    assert yaml.safe_load(yaml_path.read_text(encoding="utf-8")) == _SAMPLE_CONFIG

    # 预算端点仍按原语义 503（sidecar 未就绪）
    result = _call(server, "/ext/cost_control/budget/status")
    assert result["success"] is False
    assert result["data"]["status"] == 503
