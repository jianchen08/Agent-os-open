# @feature: FP-0.2.二 llm 插件 http 面 | @vision: V3 可嵌入 | @ci: python-coverage
"""llm_service 插件 thinking-mode 域 6 端点 + config/llm 段 13 端点测试。

（channel_api 侧车化承接；对齐原 routes_thinking_mode.py /
routes_config.py llm 段语义 + 新 http.handle 分发层）

覆盖：
1. thinking-mode：healthz / models / models/{name} / check/{name} /
   switch / recommendations + 404 边界
2. config/llm：llm、defaults（GET/PUT）、models（GET/POST/PUT/DELETE）、
   providers（GET/POST/PUT/DELETE）、provider-types、remote-models
   （404/400/502 错误映射）
3. LLM 配置写入语义：models/providers 写入 llm.yaml、明文 api_key 落 .env
   并改写 ${VAR} 占位符、mask 值回传剔除、409 重复创建防护、400 必填字段
   校验（不落盘）、404 detail 形态
4. plugin.json http_endpoints 声明 ↔ 分发路径对齐断言（20 端点、auth=user、
   timeout 沿用源值）

外部依赖：_LLM_YAML/_ENV_FILE 均以 tmp_path 重定向（模块全局替换），
不发真实网络请求（remote-models 成功路径 mock httpx.get），不接真实内核。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parents[4] / "plugins" / "shared" / "system" / "llm"

# 确定性 llm.yaml 夹具（thinking-mode + config/llm 共用）
_LLM_YAML_FIXTURE = """\
defaults:
  chat: reason-m1
  embedding: emb-1
  tiers:
    small: plain-m2
models:
  reason-m1:
    display_name: Reason M1
    provider: openai
    reasoning_model: true
    default_params:
      max_tokens: 4096
      temperature: 0.7
  plain-m2:
    display_name: Plain M2
    provider: openai
    reasoning_model: false
    default_params:
      max_tokens: 2048
providers:
  openai:
    api_base: https://api.openai.com/v1
    type: openai
    keys:
      - id: openai_main
        api_key: ${OPENAI_API_KEY}
  mock_llm:
    api_base: https://mock.local/v1
    type: openai
    keys:
      - id: mk
        api_key: sk-plain-key-12345678
"""


def _load_server() -> Any:
    """动态加载 llm/server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "llm_server_http_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["llm_server_http_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def server() -> Any:
    return _load_server()


@pytest.fixture
def rtm() -> Any:
    """routes_thinking_mode 模块（与 server 分发时 import 的同一模块对象）。"""
    import routes_thinking_mode  # noqa: PLC0415

    return routes_thinking_mode


@pytest.fixture
def rlc() -> Any:
    """routes_llm_config 模块（与 server 分发时 import 的同一模块对象）。"""
    import routes_llm_config  # noqa: PLC0415

    return routes_llm_config


@pytest.fixture
def llm_yaml(rtm: Any, rlc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把两域模块的 _LLM_YAML/_ENV_FILE 重定向到 tmp_path 并写入夹具内容。"""
    # 本机可能配置了 OPENAI_API_KEY：删掉保证 ${OPENAI_API_KEY} 不可解析
    # （has_key=False / remote-models 400 语义确定性）。
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yaml_path = tmp_path / "llm.yaml"
    yaml_path.write_text(_LLM_YAML_FIXTURE, encoding="utf-8")
    rtm._LLM_YAML = yaml_path  # type: ignore[attr-defined]
    monkeypatch.setattr(rlc, "_llm_yaml_path", lambda: yaml_path)
    monkeypatch.setattr(rlc, "_env_file_path", lambda: tmp_path / ".env")
    rlc._env_file_cache = None  # type: ignore[attr-defined]
    return yaml_path


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _call(server: Any, **kwargs: Any) -> dict[str, Any]:
    """同步调用 http.handle（测试侧统一 asyncio 跑）。"""
    return _run(server.http_handle(**kwargs))


def _decode_http(result: dict[str, Any]) -> tuple[int, Any]:
    """解包 http.handle 返回 → (status, json_body)。"""
    assert result["success"], result
    resp = result["data"]
    body = base64.b64decode(resp["body"]).decode("utf-8")
    return resp["status"], json.loads(body)


def _b64(payload: Any) -> str:
    """把 dict 编码为 base64 raw_body（http.handle body_encoding=base64 形态）。"""
    return base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii")


# ── manifest ↔ 分发对齐 ───────────────────────────────────────────────


def test_manifest_declares_9_http_endpoints() -> None:
    """plugin.json http_endpoints 声明 9 端点（6 thinking-mode + 3 config/llm）。

    批次 A1 后 llm.yaml 文件 IO 收口内核单一配置面（/api/v1/plugins/llm_service/
    config/llm + /api/v1/config/env），插件 /ext 文件 IO 端点全部退役；仍存
    config/llm 三端点 = provider-types / presets / remote-models（无状态读）。
    """
    manifest = json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))
    eps = manifest["http_endpoints"]
    by_id = {e["route_id"]: e for e in eps}
    assert len(by_id) == 9
    # thinking-mode 6
    assert by_id["thinking_mode_health"]["path"] == "/ext/llm_service/thinking-mode/healthz"
    assert by_id["thinking_mode_models_list"]["path"] == "/ext/llm_service/thinking-mode/models"
    assert by_id["thinking_mode_model_info"]["path"] == "/ext/llm_service/thinking-mode/models/{model_name}"
    assert by_id["thinking_mode_check"]["path"] == "/ext/llm_service/thinking-mode/check/{model_name}"
    assert by_id["thinking_mode_switch"]["method"] == "POST"
    assert by_id["thinking_mode_recommendations"]["method"] == "POST"
    # config/llm 3（文件 IO 已收口内核）
    assert by_id["config_llm_provider_types_get"]["path"] == "/ext/llm_service/config/llm/provider-types"
    assert by_id["config_llm_presets_get"]["path"] == "/ext/llm_service/config/llm/presets"
    remote = by_id["config_llm_providers_remote_models_get"]
    assert remote["path"] == "/ext/llm_service/config/llm/providers/{provider_id}/remote-models"
    assert remote["timeout_ms"] == 15000
    for e in eps:
        assert e["auth"] == "user"
        assert e["handler_capability"] == "http.handle"
        assert not e["path"].startswith("/ext/channel_api/")
    assert all(e["timeout_ms"] == 5000 for e in eps if e["route_id"] != "config_llm_providers_remote_models_get")


# ── thinking-mode 域 ──────────────────────────────────────────────────


def test_healthz(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/healthz", method="GET")
    )
    assert status == 200
    assert body == {"status": "ok", "available_models": 1, "service": "thinking-mode"}


def test_list_models_only_reasoning(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/models", method="GET")
    )
    assert status == 200
    names = [m["model_name"] for m in body]
    assert names == ["reason-m1"]
    assert body[0]["thinking_type"] == "parameter_switch"
    assert body[0]["is_same_model"] is True


def test_get_model_info_reasoning(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/models/reason-m1", method="GET")
    )
    assert status == 200
    assert body["thinking_type"] == "parameter_switch"
    assert body["thinking_params"]["reasoning_effort"] == 99
    assert body["normal_params"]["max_tokens"] == 4096


def test_get_model_info_plain(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/models/plain-m2", method="GET")
    )
    assert status == 200
    assert body["thinking_type"] == "none"
    assert body["switch_description"] == "该模型不支持思考模式"
    # 非思考模型的 thinking_params = default_params 透传（源语义）
    assert body["thinking_params"] == {"max_tokens": 2048}


def test_get_model_info_unknown(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/models/unknown", method="GET")
    )
    assert status == 200
    assert body["thinking_type"] == "none"
    assert body["switch_description"] == "该模型不支持思考模式"


def test_check_support(server: Any, llm_yaml: Path) -> None:
    _, ok = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/check/reason-m1", method="GET")
    )
    assert ok["supports_thinking"] is True
    _, no = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/check/plain-m2", method="GET")
    )
    assert no["supports_thinking"] is False
    _, unk = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/check/unknown", method="GET")
    )
    assert unk == {"model_name": "unknown", "supports_thinking": False}


def test_switch_mode_enable(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(
            server,
            path="/ext/llm_service/thinking-mode/switch",
            method="POST",
            raw_body=_b64({"current_model": "reason-m1", "enable_thinking": True}),
        )
    )
    assert status == 200
    assert body["switch_type"] == "parameter_switch"
    assert body["params"]["reasoning_effort"] == 99
    assert "已启用" in body["description"]


def test_switch_mode_disable(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(
            server,
            path="/ext/llm_service/thinking-mode/switch",
            method="POST",
            raw_body=_b64({"current_model": "reason-m1", "enable_thinking": False}),
        )
    )
    assert status == 200
    assert body["params"].get("reasoning_effort") is None
    assert "已关闭" in body["description"]


def test_switch_mode_unknown_model(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(
            server,
            path="/ext/llm_service/thinking-mode/switch",
            method="POST",
            raw_body=_b64({"current_model": "unknown", "enable_thinking": True}),
        )
    )
    assert status == 200
    assert body["switch_type"] == "none"
    assert "未找到" in body["description"]


def test_recommendations(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/recommendations", method="POST")
    )
    assert status == 200
    assert [m["model_name"] for m in body] == ["reason-m1"]
    assert body[0]["suitability_score"] == 0.95  # 默认 chat 模型优先


def test_thinking_mode_unknown_subpath_404(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/thinking-mode/unknown", method="GET")
    )
    assert status == 404
    assert body["error"] == "not found"


# ── config/llm 段：读端点 ─────────────────────────────────────────────


def test_get_provider_types(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/config/llm/provider-types", method="GET")
    )
    assert status == 200
    assert isinstance(body["types"], list)
    assert body["types"]
    assert "deepseek" in body["types"]


# ── config/llm 段：写端点（defaults/models/providers）─────────────────


# ── config/llm 段：必填字段校验（镜像源 pydantic 必填语义，缺字段不落盘）──


# ── config/llm 段：remote-models ──────────────────────────────────────


def test_remote_models_unknown_provider_404(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(
            server,
            path="/ext/llm_service/config/llm/providers/nope/remote-models",
            method="GET",
        )
    )
    assert status == 404
    assert body == {"detail": "提供商 'nope' 不存在"}


def test_remote_models_no_api_key_400(server: Any, llm_yaml: Path) -> None:
    """${OPENAI_API_KEY} 未配置 → 400。"""
    status, body = _decode_http(
        _call(
            server,
            path="/ext/llm_service/config/llm/providers/openai/remote-models",
            method="GET",
        )
    )
    assert status == 400
    assert "尚未配置可用的 API Key" in body["detail"]


def test_remote_models_success(server: Any, llm_yaml: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """明文 key 的 mock_llm + mock httpx.get → 模型清单。"""
    import httpx  # noqa: PLC0415

    class _FakeResp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return {"data": [{"id": "mock-1", "owned_by": "me"}, {"id": "mock-0", "owned_by": ""}]}

    def _fake_get(url: str, headers: dict[str, str] | None = None, timeout: float = 8.0) -> Any:
        assert url == "https://mock.local/v1/models"
        return _FakeResp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    status, body = _decode_http(
        _call(
            server,
            path="/ext/llm_service/config/llm/providers/mock_llm/remote-models",
            method="GET",
        )
    )
    assert status == 200
    assert body["provider"] == "mock_llm"
    assert [m["id"] for m in body["models"]] == ["mock-0", "mock-1"]  # 按 id 排序
    assert body["models"][1]["owned_by"] == "me"


def test_remote_models_upstream_error_502(
    server: Any, llm_yaml: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx  # noqa: PLC0415

    def _fake_get(url: str, headers: dict[str, str] | None = None, timeout: float = 8.0) -> Any:
        raise ValueError("boom")

    monkeypatch.setattr(httpx, "get", _fake_get)
    status, body = _decode_http(
        _call(
            server,
            path="/ext/llm_service/config/llm/providers/mock_llm/remote-models",
            method="GET",
        )
    )
    assert status == 502
    assert "拉取模型列表失败" in body["detail"]


# ── 分发层边界 ────────────────────────────────────────────────────────


def test_config_llm_unknown_subpath_404(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/config/llm/unknown", method="GET")
    )
    assert status == 404
    assert body["error"] == "not found"


def test_non_llm_path_404(server: Any, llm_yaml: Path) -> None:
    status, body = _decode_http(
        _call(server, path="/ext/llm_service/other", method="GET")
    )
    assert status == 404


# ── llm.yaml 缺失/损坏：显式报错，禁止伪造空配置（假 fallback 治理）──


def _health_via_http(server: Any, rtm: Any, yaml_path: Path) -> tuple[int, Any]:
    """把 rtm._LLM_YAML 重定向到指定路径后走 healthz，返回 (status, body)。"""
    saved = rtm._LLM_YAML
    rtm._LLM_YAML = yaml_path
    try:
        return _decode_http(
            _call(server, path="/ext/llm_service/thinking-mode/healthz", method="GET")
        )
    finally:
        rtm._LLM_YAML = saved


class TestThinkingModeConfigErrors:
    def test_missing_llm_yaml_health_errors(
        self, server: Any, rtm: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """llm.yaml 缺失 → health 返回 500 明确报错，不得 status=ok 0 模型。"""
        missing = tmp_path / "no_such_llm.yaml"
        assert not missing.exists()
        status, body = _health_via_http(server, rtm, missing)
        assert status == 500
        assert "llm.yaml 不存在" in body["detail"]

    def test_missing_llm_yaml_models_endpoint_errors_too(
        self, server: Any, rtm: Any, tmp_path: Path
    ) -> None:
        """缺失在列表端点同样报错——任何路由都不吃伪造空配置。"""
        missing = tmp_path / "absent.yaml"
        saved = rtm._LLM_YAML
        rtm._LLM_YAML = missing
        try:
            status, body = _decode_http(
                _call(server, path="/ext/llm_service/thinking-mode/models", method="GET")
            )
        finally:
            rtm._LLM_YAML = saved
        assert status == 500
        assert "不存在" in body["detail"] or "无有效配置" in body["detail"]

    def test_empty_llm_yaml_is_malformed_not_ok(
        self, server: Any, rtm: Any, tmp_path: Path
    ) -> None:
        """/空内容 yaml 解析为 None → 按无效配置报错（不伪造 defaults）。"""
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        status, body = _health_via_http(server, rtm, empty)
        assert status == 500
        assert "无有效配置" in body["detail"]

    def test_valid_yaml_health_still_ok(
        self, server: Any, rtm: Any, llm_yaml: Path
    ) -> None:
        """对照基线：夹具 yaml 正常时 health ok 且正确计数 reasoning 模型。"""
        status, body = _health_via_http(server, rtm, llm_yaml)
        assert status == 200
        assert body["status"] == "ok"
        assert body["available_models"] >= 1
