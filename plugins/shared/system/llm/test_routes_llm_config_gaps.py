# @feature: FP-0.2.CFG 配置读写单源化 | @ci: python-test
"""routes_llm_config 保留面（presets / provider-types / remote-models）测试。

2026-09-28 批次 A1：llm.yaml 文件 IO 端点（yaml CRUD + .env 直写）退役——
读写收口到内核单一配置面 `/api/v1/plugins/llm_service/config/llm`，provider
key 走 `PUT /api/v1/config/env`。本文件覆盖退役后仍存活的只读面与解析助手：

- 落点解析：_llm_yaml_path / _env_file_path 用户空间接管文件优先，缺省回落
  出厂种子（读写同源公理的读侧）；
- _read_yaml：配置文件不存在 → ConfigAPIError 404；
- _resolve_env_value：空值 → None（「未配置」语义）；
- get_llm_presets：声明文件 YAML 损坏 → ConfigAPIError 500（fail-closed）；
- get_provider_types：litellm.provider_list 读取失败 → 回退核心类型清单；
- get_remote_models：anthropic 类型默认基址与 /v1 归一（三种 api_base 形态）、
  上游 HTTP 错误 → 502、models 键载荷兜底。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
# routes_llm_config 依赖 plugins/shared 根的共享模块（atomic_io/user_space 等），一并入 path
_SHARED_DIR = _DIR.parents[1]
for _p in (str(_DIR), str(_SHARED_DIR)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


@pytest.fixture
def rlc() -> Any:
    """按显式路径加载 routes_llm_config（裸名防劫持）。"""
    spec = importlib.util.spec_from_file_location(
        "llm_routes_llm_config_gaps_test", str(_DIR / "routes_llm_config.py")
    )
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["llm_routes_llm_config_gaps_test"] = m
    spec.loader.exec_module(m)
    return m


def test_llm_yaml_path_user_space_takeover_wins(
    rlc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户空间已接管 llm.yaml → 落点取用户层（读写同源公理读侧）。"""
    user_llm = tmp_path / "user_root" / "config" / "plugins" / "llm" / "llm.yaml"
    user_llm.parent.mkdir(parents=True)
    user_llm.write_text("defaults: {}\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))

    resolved = rlc._llm_yaml_path()

    assert resolved == user_llm


def test_llm_yaml_path_falls_back_to_factory_without_takeover(
    rlc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户空间无该文件（未接管）→ 回落出厂种子路径。"""
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))
    factory = tmp_path / "factory"
    monkeypatch.setattr(rlc, "_PROJECT_ROOT", factory)

    resolved = rlc._llm_yaml_path()

    assert resolved == factory / "config" / "plugins" / "llm" / "llm.yaml"


def test_env_file_path_prefers_user_space(
    rlc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """.env 落点恒用户空间优先（provider key 的唯一真值面）。"""
    user_env = tmp_path / "user_root" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("DEEPSEEK_API_KEY=x\n", encoding="utf-8")
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user_root"))

    resolved = rlc._env_file_path()

    assert resolved == user_env


def test_read_yaml_missing_file_raises_404(rlc: Any, tmp_path: Path) -> None:
    with pytest.raises(rlc.ConfigAPIError) as exc_info:
        rlc._read_yaml(tmp_path / "not-exists.yaml")

    assert exc_info.value.status_code == 404
    assert "not-exists.yaml" in exc_info.value.detail


def test_resolve_env_value_empty_returns_none(rlc: Any) -> None:
    """空值/未填 → None（「未配置」语义，绝不能当成字面量 key 发上游）。"""
    assert rlc._resolve_env_value(None) is None
    assert rlc._resolve_env_value("") is None


# ─────────────────────────── get_llm_presets：解析失败 ───────────────────────────


def test_get_llm_presets_broken_yaml_fails_closed(rlc: Any, tmp_path: Path) -> None:
    """声明文件 YAML 损坏 → ConfigAPIError 500（不回退空清单静默降级）。"""
    broken = tmp_path / "llm_presets.yaml"
    broken.write_text("provider_groups: [unclosed", encoding="utf-8")
    rlc._PRESETS_FILE = broken

    with pytest.raises(rlc.ConfigAPIError) as exc_info:
        rlc.get_llm_presets()

    assert exc_info.value.status_code == 500
    assert "解析失败" in exc_info.value.detail


# ─────────────────────────── get_provider_types：litellm 故障回退 ───────────────────────────


def test_get_provider_types_falls_back_when_registry_read_fails(
    rlc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """provider_list 迭代失败 → 回退核心类型清单（前端下拉不空转）。"""
    import litellm

    class _Exploding:
        def __iter__(self) -> Any:
            raise RuntimeError("registry unavailable")

    monkeypatch.setattr(litellm, "provider_list", _Exploding())

    payload = rlc.get_provider_types()

    assert payload["types"] == ["anthropic", "deepseek", "minimax", "openai", "zai"]


# ─────────────────────────── get_remote_models ───────────────────────────


class _FakeResp:
    """伪 httpx 响应：预置 JSON 载荷，raise_for_status 可控。"""

    def __init__(self, payload: dict[str, Any], status_error: Exception | None = None) -> None:
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self) -> None:
        if self._status_error is not None:
            raise self._status_error

    def json(self) -> dict[str, Any]:
        return self._payload


def _stub_provider_llm_yaml(rlc: Any, provider_conf: dict[str, Any], provider_id: str = "prov") -> None:
    rlc._read_yaml = lambda _p: {"providers": {provider_id: provider_conf}}  # type: ignore[method-assign]
    rlc._resolve_env_value = lambda v: "sk-resolved" if v else None  # type: ignore[method-assign]


@pytest.mark.parametrize(
    ("api_base", "expected_url"),
    [
        # 未配置 api_base → 官方公共 API 默认基址
        ("", "https://api.anthropic.com/v1/models"),
        # 自定义网关 → 追加 /v1
        ("https://relay.example", "https://relay.example/v1/models"),
        # 已带 /v1 → 不重复追加
        ("https://relay.example/v1", "https://relay.example/v1/models"),
    ],
)
def test_get_remote_models_anthropic_base_and_headers(
    rlc: Any, monkeypatch: pytest.MonkeyPatch, api_base: str, expected_url: str
) -> None:
    """anthropic 类型：默认基址回落 + /v1 归一 + x-api-key/版本头（三种 api_base 形态）。"""
    import httpx

    _stub_provider_llm_yaml(rlc, {"type": "anthropic", "api_base": api_base,
                                  "keys": [{"api_key": "${X}"}]})
    captured: dict[str, Any] = {}

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> Any:
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp({"data": [{"id": "cla-x", "owned_by": "anthropic"}]})

    monkeypatch.setattr(httpx, "get", fake_get)

    payload = rlc.get_remote_models("prov")

    assert captured["url"] == expected_url
    assert captured["headers"]["x-api-key"] == "sk-resolved"
    assert captured["headers"]["anthropic-version"] == rlc._ANTHROPIC_API_VERSION
    assert payload["models"] == [{"id": "cla-x", "owned_by": "anthropic"}]


def test_get_remote_models_http_error_raises_502(
    rlc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """上游 HTTP 错误 → ConfigAPIError 502，提示可手动输入模型名（不裸抛 httpx 异常）。"""
    import httpx

    _stub_provider_llm_yaml(rlc, {"type": "openai", "api_base": "https://relay.example",
                                  "keys": [{"api_key": "${X}"}]})

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> Any:
        request = httpx.Request("GET", url)
        return _FakeResp({}, status_error=httpx.HTTPStatusError(
            "server error", request=request, response=httpx.Response(500, request=request)
        ))

    monkeypatch.setattr(httpx, "get", fake_get)

    with pytest.raises(rlc.ConfigAPIError) as exc_info:
        rlc.get_remote_models("prov")

    assert exc_info.value.status_code == 502
    assert "HTTP 500" in exc_info.value.detail


def test_get_remote_models_models_key_payload_fallback(
    rlc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """载荷无 data 键但有 models 键 → models 键兜底（非 OpenAI 形态网关）。"""
    import httpx

    _stub_provider_llm_yaml(rlc, {"type": "openai", "api_base": "https://relay.example",
                                  "keys": [{"api_key": "${X}"}]})

    def fake_get(url: str, *, headers: dict[str, str], timeout: float) -> Any:
        return _FakeResp({"models": [{"id": "m-b"}, {"id": "m-a"}]})

    monkeypatch.setattr(httpx, "get", fake_get)

    payload = rlc.get_remote_models("prov")

    assert [m["id"] for m in payload["models"]] == ["m-a", "m-b"]  # 按 id 排序下发
