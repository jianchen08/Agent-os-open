# @feature: B11-L4 直连域名表单源 | @ci: python-coverage
"""LLM 直连域名白名单单源测试：真值 = llm.yaml providers api_base，不再有硬编码表。

锁定行为：
- 派生律（读真实 config/plugins/llm/llm.yaml）：_direct_hosts_from_llm_data
  的生成集 = providers 段全部 api_base（provider 级 + key 级覆盖）的 hostname
  并集——yaml 中每个 host 都在集内（含历史硬编码表漏掉的多数 host），非
  hostname 的 URL 部件（scheme/path 片段）不误入；
- disable_llm_proxy 契约保持：清代理 env、关 trust_env、NO_PROXY 与既有值
  逗号合并且幂等（重复调用集不变）；
- build_router 接线：构建时以注入配置派生的 host 集更新 NO_PROXY——host 表
  随配置走，配置加 provider 即进直连白名单，无需改代码。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
import yaml

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _PLUGIN_DIR.parents[2]
_LLM_YAML = _REPO_ROOT / "config" / "plugins" / "llm" / "llm.yaml"

_PROXY_ENV_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def _load_router_factory() -> Any:
    """按唯一模块名加载 router_factory（先弹平铺裸名缓存，防其它插件目录劫持）。"""
    for _m in ("key_pool", "exceptions"):
        sys.modules.pop(_m, None)
    mod_name = "router_factory_direct_hosts_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "router_factory.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def rf() -> Any:
    module = _load_router_factory()
    module.reset_router()
    yield module
    module.reset_router()


def _yaml_provider_hosts() -> set[str]:
    """真实 llm.yaml providers 段全部 api_base 的 hostname 并集（独立实现做交叉验证）。"""
    data = yaml.safe_load(_LLM_YAML.read_text(encoding="utf-8"))
    hosts: set[str] = set()
    for provider_conf in (data.get("providers") or {}).values():
        if not isinstance(provider_conf, dict):
            continue
        for base in [provider_conf.get("api_base", "")] + [
            k.get("api_base", "") for k in (provider_conf.get("keys") or []) if isinstance(k, dict)
        ]:
            if base:
                host = urlparse(base).hostname
                if host:
                    hosts.add(host)
    return hosts


def test_generated_hosts_equal_yaml_provider_hosts() -> None:
    """生成集与真实 llm.yaml 的 api_base hostname 并集全等（双向：不漏不增）。"""
    rf = _load_router_factory()
    data = yaml.safe_load(_LLM_YAML.read_text(encoding="utf-8"))
    generated = set(rf._direct_hosts_from_llm_data(data))
    expected = _yaml_provider_hosts()
    assert generated == expected
    assert expected, "llm.yaml providers 无任何 api_base（配置面漂移，本测试失去对象）"


def test_legacy_hardcoded_hosts_still_covered_and_no_phantom() -> None:
    """历史硬编码 4 host 中现役者仍在集内；yaml 已不存在的 host 不再误入。"""
    rf = _load_router_factory()
    data = yaml.safe_load(_LLM_YAML.read_text(encoding="utf-8"))
    generated = set(rf._direct_hosts_from_llm_data(data))
    for host in ("open.bigmodel.cn", "api.deepseek.com", "api.minimaxi.com"):
        assert host in generated
    # ai.1cc.ai 已不在 llm.yaml providers 的 api_base 中——配置驱动后不再误入
    assert "ai.1cc.ai" not in _yaml_provider_hosts()
    assert "ai.1cc.ai" not in generated
    # URL 部件不是 hostname，不误入
    assert "https" not in generated
    assert "v1" not in generated


def test_synthetic_data_edge_shapes() -> None:
    """合成配置边界：key 级覆盖并入、非 dict provider 跳过、脏 base 跳过、首现序去重。"""
    rf = _load_router_factory()
    data = {
        "providers": {
            "p1": {
                "type": "openai",
                "api_base": "https://a.example/v1",
                "keys": [
                    {"api_key": "k1", "api_base": "https://b.example/v1"},
                    "not-a-dict",
                ],
            },
            "p1_dup": {"api_base": "https://a.example/v2"},
            "bad": "not-a-dict",
            "empty_base": {},
            "dirty": {"api_base": "not a url"},
        },
    }
    hosts = rf._direct_hosts_from_llm_data(data)
    assert hosts == ("a.example", "b.example")


def test_disable_llm_proxy_clears_env_and_merges_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    rf = _load_router_factory()
    for var in _PROXY_ENV_VARS:
        monkeypatch.setenv(var, "http://proxy.local:7890")
    # Windows 的 os.environ 键大小写不敏感：必须先删 no_proxy 再设 NO_PROXY，
    # 逆序会把刚设的值当同一变量删掉
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.setenv("NO_PROXY", "internal.example")

    rf.disable_llm_proxy(("a.example", "b.example"))

    for var in _PROXY_ENV_VARS:
        assert var not in os.environ, f"{var} 应被清空"
    merged = os.environ["NO_PROXY"].split(",")
    assert merged[0] == "internal.example"  # 既有值在前，不丢
    assert set(merged) >= {"a.example", "b.example"}


def test_disable_llm_proxy_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """幂等性：同 host 集重复调用，NO_PROXY 集合不变（不重复追加）。"""
    rf = _load_router_factory()
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    rf.disable_llm_proxy(("a.example", "b.example"))
    first = os.environ["NO_PROXY"]
    rf.disable_llm_proxy(("a.example", "b.example"))
    assert os.environ["NO_PROXY"] == first
    assert os.environ["no_proxy"] == first


class _FakeLoader:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def load_llm_data(self) -> dict[str, Any]:
        return self._data


def test_build_router_wires_yaml_derived_hosts_into_no_proxy(
    rf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_router 接线：NO_PROXY 增量来自注入配置的 providers api_base。"""
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    class _FakeRouter:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    loader = _FakeLoader(
        {
            "defaults": {"call_timeout": 5},
            "providers": {
                "prov_a": {"type": "openai", "api_base": "https://wired.example/v1", "api_key": "sk-x"},
                "prov_b": {"type": "openai", "api_base": "https://wired2.example/v1", "api_key": "sk-y"},
            },
            "models": {"m1": {"provider": "prov_a", "model_name": "real-1"}},
        }
    )
    rf.build_router(loader)
    merged = set(os.environ["NO_PROXY"].split(","))
    assert {"wired.example", "wired2.example"} <= merged


def test_wire_client_transport_is_direct_under_system_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """wire_client 传输层必须直连：httpx 默认 trust_env=True 会读环境变量与
    Windows 注册表系统代理——只清 env（disable_llm_proxy）后代理仍生效，
    本机 stub/外呼会被系统代理劫持成 502（2026-11-04 e2e 实测）。

    用死代理地址验证行为：若客户端信任代理则连接必失败，直连则真实到达
    本地 server。
    """
    import asyncio
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            pass

        def do_POST(self) -> None:  # noqa: N802
            payload = json.dumps(
                {"choices": [{"message": {"role": "assistant", "content": "direct-ok"}}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

    sys.path.insert(0, str(_PLUGIN_DIR))
    import wire_client  # noqa: PLC0415

    wire_client._shared_client = None  # noqa: SLF001 —— 断共享单例，按当前环境重建

    async def _scenario():
        # 客户端与关闭必须同一事件循环（跨 loop aclose 触发 "Event loop is closed"）
        try:
            return await wire_client.post_chat(
                base_url, "k", {"model": "m", "messages": []}, timeout=5
            )
        finally:
            await wire_client.aclose_shared_client()

    try:
        result = asyncio.run(_scenario())
        assert result.choices[0].message.content == "direct-ok"
    finally:
        wire_client._shared_client = None  # noqa: SLF001
        server.shutdown()
        server.server_close()
