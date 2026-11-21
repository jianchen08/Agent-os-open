# @feature: FP-LLM网关随包分发 | @ci: python-coverage
"""llm_provider_gateway 插件 manifest 契约与 server.py 接线测试。

manifest 面（静态断言）：
- provides namespace = ``llm_provider.gateway``（tool_prefix=gateway），methods
  与 services 背书一致（G2 provides_methods_unbacked 同口径）；
- config_files 声明两个命名空间且种子文件在盘、零个人资产、含 AUTH_DIR 令牌；
- 二进制配方脚本存在且含版本号 + SHA-256 pin 常量。

server 面（importlib 显式路径加载，逐出裸名 server 防跨插件劫持）：
- 注册工具名 = services 声明名（声明=实现）；
- complete_stream 三分支：网关不可达 → llm_failure 结构化失败；
  上游成功 → 聚合透传；api_key/api_base 参数覆盖面；
- status/start/stop 委托 manager。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PLUGIN_DIR = Path(__file__).resolve().parent.parent
_CONFIG_DIR = _REPO_ROOT / "config" / "plugins" / "llm_provider_gateway"
_SCRIPT = _REPO_ROOT / "scripts" / "ensure_gateway_binary.py"
_MOD_NAME = "llm_provider_gateway_server_test"


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


# ── manifest 契约（G2 注册闸同口径） ─────────────────────────────────────


def _manifest() -> dict[str, Any]:
    return json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))


def test_manifest_provider_identity() -> None:
    m = _manifest()
    assert m["id"] == "llm_provider_gateway"
    caps = m["provides"]["capabilities"]
    assert len(caps) == 1
    assert caps[0]["namespace"] == "llm_provider.gateway"
    assert caps[0]["tool_prefix"] == "gateway"
    assert sorted(caps[0]["methods"]) == ["complete_stream", "start", "status", "stop"]


def test_manifest_services_back_provides_methods() -> None:
    """provides 公告的 {prefix}.{method} 必须有同名 service 背书
    （kernel provides_methods_unbacked 同口径，缺背书 = 注册闸红）。"""
    m = _manifest()
    declared = {s["name"] for s in m["capabilities"]["services"]}
    prefix = m["provides"]["capabilities"][0]["tool_prefix"]
    for method in m["provides"]["capabilities"][0]["methods"]:
        assert f"{prefix}.{method}" in declared


def test_manifest_no_llm_tool_surface() -> None:
    """工具面恒空：services 是唯一接口面，LLM tool_ids 白名单零登记需求。"""
    m = _manifest()
    assert m["capabilities"]["tools"] == []


def test_manifest_config_files_declare_namespaces() -> None:
    m = _manifest()
    cf = {entry["id"]: entry["path"] for entry in m["config_files"]}
    assert set(cf) == {"gateway", "gateway_server"}
    assert cf["gateway"] == "config/plugins/llm_provider_gateway/gateway.yaml"
    assert cf["gateway_server"] == "config/plugins/llm_provider_gateway/gateway_server.yaml"


def test_seed_files_exist_and_carry_no_personal_assets() -> None:
    """种子在盘；零个人钱包车道（CodeBuddy/礼包/plan/zcode 零出现）；含 AUTH 令牌。"""
    control = (_CONFIG_DIR / "gateway.yaml").read_text(encoding="utf-8")
    server_seed = (_CONFIG_DIR / "gateway_server.yaml").read_text(encoding="utf-8")
    for text in (control, server_seed):
        for banned in ("C:\\Users\\jc", "CodeBuddy", "zcode-proxy", "8080", "8082"):
            assert banned not in text, f"种子不得含个人资产/本机车道: {banned}"
    assert "__AGENTOS_AUTH_DIR__" in server_seed
    assert "8317" in server_seed


def test_binary_recipe_script_exists_with_pins() -> None:
    """配方脚本存在且含版本常量 + SHA-256 pin 常量（防供应链自造）。"""
    assert _SCRIPT.is_file()
    text = _SCRIPT.read_text(encoding="utf-8")
    assert "GATEWAY_VERSION" in text
    assert "SHA256" in text.upper()
    assert len(text) > 500


def test_bin_directory_is_gitignored() -> None:
    """bin/ 产物不入库（ADR 2026-10-16）：.gitignore 覆盖插件 bin 目录。"""
    gitignore = (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "llm_provider_gateway/bin" in gitignore


def test_no_dll_or_zcode_proxy_in_plugin_dir() -> None:
    """许可红线：插件目录不存在 provider dll 与 zcode-proxy 产物。"""
    offenders = [
        p.name
        for p in _PLUGIN_DIR.rglob("*")
        if p.is_file() and (p.suffix == ".dll" or p.name.startswith("zcode-proxy"))
    ]
    assert offenders == []


# ── server.py 接线 ───────────────────────────────────────────────────────


class _FakeManager:
    def __init__(self, *, tmp_path: Any, ensure_result: dict[str, Any] | None = None, status_result: dict[str, Any] | None = None) -> None:
        from gateway_manager import GatewayConfig

        self.config = GatewayConfig(
            base_url="http://127.0.0.1:8317",
            port=8317,
            binary_path=tmp_path / "bin" / "cli-proxy-api.exe",
            server_config_path=tmp_path / "user" / "gateway_server.yaml",
            auth_dir=tmp_path / "user" / "auths",
            log_path=tmp_path / "user" / "logs" / "llm_gateway.log",
        )
        self.ensure_result = ensure_result or {"action": "reuse", "running": True, "detail": "HTTP 200"}
        self.status_result = status_result or {"running": True, "owned": False, "detail": "HTTP 200"}
        self.ensure_calls = 0
        self.stop_calls = 0

    async def ensure_running(self) -> dict[str, Any]:
        self.ensure_calls += 1
        return dict(self.ensure_result)

    async def status(self) -> dict[str, Any]:
        return dict(self.status_result)

    async def stop(self) -> dict[str, Any]:
        self.stop_calls += 1
        return {"action": "stopped", "detail": "done"}


@pytest.fixture()
def server_mod(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    module = _load_server()
    fake = _FakeManager(tmp_path=tmp_path)

    async def _fake_get_manager() -> Any:
        return fake

    monkeypatch.setattr(module, "_get_manager", _fake_get_manager)
    module._test_fake_manager = fake  # type: ignore[attr-defined]
    return module


def test_registered_tools_match_service_declarations(server_mod: Any) -> None:
    """注册工具名 = services 声明名（声明=实现，G2 同口径）。"""
    manifest = _manifest()
    declared = {s["name"] for s in manifest["capabilities"]["services"]}
    registered = set(server_mod.plugin._tools.keys())
    assert registered == declared


def test_complete_stream_success_passes_aggregate_through(
    server_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    aggregate = {"status": "streamed", "text": "ok", "tool_calls": [], "usage": {}, "finish_reason": "stop"}
    seen: dict[str, Any] = {}

    async def _fake_wire(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return aggregate

    monkeypatch.setattr(server_mod, "_complete_via_gateway", _fake_wire)
    result = asyncio.run(
        server_mod.gateway_complete_stream(
            model="glm-5.3-flash",
            model_name="glm-5.3-flash",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            stream=True,
            params={"temperature": 0.7},
            stream_ctx={"stream_id": "s1"},
        )
    )
    assert result == aggregate
    assert seen["model_name"] == "glm-5.3-flash"
    assert seen["stream"] is True
    # api_key/api_base 未注入（provider 无 keys → 插件自持）
    assert seen["client_api_key"] == ""
    assert server_mod._test_fake_manager.ensure_calls == 1


def test_complete_stream_gateway_unreachable_structured_failure(server_mod: Any) -> None:
    server_mod._test_fake_manager.ensure_result = {
        "action": "port_conflict",
        "running": False,
        "detail": "端口被第三方占用",
    }
    result = asyncio.run(
        server_mod.gateway_complete_stream(
            model="m", model_name="m", messages=[], tools=None, stream=False, params={}
        )
    )
    assert result["status"] == "error"
    assert result["llm_failure"]["code"] == "service_down"
    assert "port_conflict" in result["llm_failure"]["message"]


def test_complete_stream_upstream_error_maps_to_llm_failure(
    server_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway_wire import GatewayUpstreamError

    async def _fake_wire(**kwargs: Any) -> dict[str, Any]:
        raise GatewayUpstreamError("rate_limit", "slow down", retry_after=2.0)

    monkeypatch.setattr(server_mod, "_complete_via_gateway", _fake_wire)
    result = asyncio.run(
        server_mod.gateway_complete_stream(
            model="m", model_name="m", messages=[], tools=None, stream=False, params={}
        )
    )
    assert result["status"] == "error"
    assert result["llm_failure"]["code"] == "rate_limit"
    assert result["llm_failure"]["retry_after"] == 2.0


def test_complete_stream_api_key_param_overrides_config(
    server_mod: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """网关托管形态（provider 配 keys）→ slot 的 api_key/api_base 参数面生效。"""
    seen: dict[str, Any] = {}

    async def _fake_wire(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"status": "streamed", "text": "x"}

    monkeypatch.setattr(server_mod, "_complete_via_gateway", _fake_wire)
    asyncio.run(
        server_mod.gateway_complete_stream(
            model="m",
            model_name="m",
            messages=[],
            tools=None,
            stream=False,
            params={},
            api_key="sk-slot",
            api_base="http://127.0.0.1:9000",
        )
    )
    assert seen["client_api_key"] == "sk-slot"
    assert seen["base_url"] == "http://127.0.0.1:9000"


def test_status_start_stop_delegate_to_manager(server_mod: Any) -> None:
    fake = server_mod._test_fake_manager
    status = asyncio.run(server_mod.gateway_status())
    assert status["running"] is True
    start = asyncio.run(server_mod.gateway_start())
    assert start["action"] == "reuse"
    stop = asyncio.run(server_mod.gateway_stop())
    assert stop["action"] == "stopped"
    assert fake.stop_calls == 1


def test_on_unload_stops_owned_process(server_mod: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = server_mod._test_fake_manager
    asyncio.run(server_mod._on_unload({}))
    assert fake.stop_calls == 1
