# @feature: FP-0.2.ACP额度池 R6网关托管 | @ci: python-coverage
"""server.py 接线测试（显式路径加载，逐出裸名 server 防跨插件劫持）。

裸名 ``import server`` 在多插件共跑车道会命中他人实现（R2 实证在案），
故按 child_task_guard 先例经 importlib 显式路径加载为独名模块。覆盖
http.handle 三路由分发、信封形态、404/422 边界翻译与 on_load 编排——
探活全打桩（外部 HTTP 边界，本文件零网络零 spawn；分支级行为在
test_acp_gateway 已覆盖；拉起面 2026-11-21 退役，start 端点语义=探活报告）。
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
from acp_gateway import GatewayConfig

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_MOD_NAME = "acp_quota_pool_server_test"


def _load_server() -> Any:
    """显式路径加载 server.py 为独名模块（先注册 sys.modules 再 exec）。"""
    sys.modules.pop(_MOD_NAME, None)
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    spec.loader.exec_module(module)
    return module


_SERVER = _load_server()


def _config() -> GatewayConfig:
    return GatewayConfig(
        base_url="http://127.0.0.1:9999",
        health_timeout_secs=0.5,
    )


def _patch_gateway(
    monkeypatch: pytest.MonkeyPatch,
    *,
    probe_result: tuple[bool, str] | None = None,
    ensure_result: dict[str, Any] | None = None,
    config_error: ValueError | None = None,
) -> dict[str, list[Any]]:
    """把 server 命名空间里的网关面全部换桩（外部边界替身），返回调用记录。"""
    calls: dict[str, list[Any]] = {"probe": [], "ensure": [], "read": []}

    def _read(_cfg: Any) -> GatewayConfig:
        calls["read"].append(1)
        if config_error is not None:
            raise config_error
        return _config()

    async def _probe(base_url: str, _timeout: float) -> tuple[bool, str]:
        calls["probe"].append(base_url)
        return probe_result or (True, "HTTP 200")

    async def _ensure(config: GatewayConfig) -> dict[str, Any]:
        calls["ensure"].append(config)
        return ensure_result or {"action": "none", "running": True, "detail": "HTTP 200"}

    monkeypatch.setattr(_SERVER, "read_gateway_config", _read)
    monkeypatch.setattr(_SERVER, "probe_gateway", _probe)
    monkeypatch.setattr(_SERVER, "ensure_gateway_running", _ensure)
    return calls


def _body(response: dict[str, Any]) -> tuple[int | None, Any]:
    """解 HttpHandleResponse 信封：base64 解包 + JSON 解析（HTML 路由除外）。"""
    data = response["data"]
    body = data["body"]
    if data.get("body_encoding") == "base64":
        body = base64.b64decode(body).decode("utf-8")
    if isinstance(body, str) and body.startswith("{"):
        body = json.loads(body)
    return data.get("status"), body


# ── http.handle 分发 ──────────────────────────────────────────────────────


def test_status_route_probes_and_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_gateway(monkeypatch, probe_result=(False, "URLError: refused"))

    response = asyncio.run(
        _SERVER.http_handle(path="/ext/acp_quota_pool/gateway/status", method="GET")
    )

    assert response["success"] is True
    status, body = _body(response)
    assert status == 200
    assert body["running"] is False
    assert body["base_url"] == "http://127.0.0.1:9999"
    assert body["management_url"] == "http://127.0.0.1:9999/management.html"
    assert calls["probe"] == ["http://127.0.0.1:9999"]  # status 只探不起
    assert calls["ensure"] == []


def test_start_route_reuses_ensure_and_carries_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    """start 端点保留（控制台页/契约面不破），语义 = 探活报告（拉起移交后
    网关不在时返回 handoff 指向 llm_provider_gateway）。"""
    calls = _patch_gateway(
        monkeypatch,
        ensure_result={"action": "handoff", "running": False, "detail": "网关不在"},
    )

    response = asyncio.run(
        _SERVER.http_handle(path="/ext/acp_quota_pool/gateway/start", method="POST")
    )

    _, body = _body(response)
    assert body["action"] == "handoff"
    assert body["running"] is False
    assert body["management_url"].endswith("/management.html")
    assert len(calls["ensure"]) == 1  # 与 on_load 同一段探活入口
    assert calls["probe"] == []


def test_console_page_route_serves_html() -> None:
    response = asyncio.run(
        _SERVER.http_handle(path="/ext/acp_quota_pool/page/console", method="GET")
    )

    status, body = _body(response)
    assert status == 200
    assert "网关控制台" in body
    assert "/ext/acp_quota_pool/gateway/status" in body


def test_unknown_route_404() -> None:
    response = asyncio.run(_SERVER.http_handle(path="/ext/acp_quota_pool/nope", method="GET"))

    status, body = _body(response)
    assert status == 404
    assert body["error"] == "not found"


def test_gateway_ensure_degrades_on_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gateway(monkeypatch, config_error=ValueError("gateway.base_url 须为 http(s) URL"))

    result = asyncio.run(_SERVER._gateway_ensure())

    assert result["action"] == "config_invalid"
    assert result["running"] is False
    assert result["management_url"] is None  # 配置坏时无 URL 可给（不硬凑）


def test_unexpected_error_translates_to_500(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(base_url: str, timeout: float) -> tuple[bool, str]:
        raise RuntimeError("探活器炸了")

    monkeypatch.setattr(_SERVER, "read_gateway_config", lambda _cfg: _config())
    monkeypatch.setattr(_SERVER, "probe_gateway", _boom)

    response = asyncio.run(
        _SERVER.http_handle(path="/ext/acp_quota_pool/gateway/status", method="GET")
    )

    assert response["success"] is False
    assert "探活器炸了" in response["error"]
    assert response["data"]["status"] == 500


def test_invalid_config_translates_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_gateway(monkeypatch, config_error=ValueError("gateway.base_url 须为 http(s) URL"))

    response = asyncio.run(
        _SERVER.http_handle(path="/ext/acp_quota_pool/gateway/status", method="GET")
    )

    status, body = _body(response)
    assert status == 422
    assert "base_url" in body["error"]


# ── on_load 编排（播种 + 网关托管，全桩）──────────────────────────────────


def test_on_load_seeds_then_ensures_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_gateway(monkeypatch, ensure_result={"action": "none", "running": True, "detail": "x"})
    seeded: list[tuple[Path | None, Path]] = []
    monkeypatch.setattr(_SERVER, "user_root", lambda: tmp_path)
    monkeypatch.setattr(
        _SERVER,
        "seed_with_degradation",
        lambda user_root, plugin_dir: seeded.append((user_root, plugin_dir)) or "seeded: 6 files",
    )

    asyncio.run(_SERVER._on_load({}))

    assert seeded == [(tmp_path, _PLUGIN_DIR)]  # 播种先行（既有行为不回退）
    assert len(calls["ensure"]) == 1  # 网关托管随后，消费同一段 ensure 入口
    assert calls["ensure"][0].base_url == "http://127.0.0.1:9999"
