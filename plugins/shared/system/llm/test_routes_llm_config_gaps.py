# @feature: FP-T12 前端适配 | @ci: python-coverage
"""routes_llm_config 未覆盖分支补测（缓存失效 / .env 兜底 / defaults / keys 合并 / 预置解析 / 拉取模型容错）。

行为契约（断输入→输出/副作用，直写临时 llm.yaml 后断言落盘结果）：
- _invalidate_llm_caches：hook 注册则调用（成功透传 / 失败吞掉仅告警），None 跳过
- _resolve_project_root：向上无 config/ 特征 → 回退 parent×4 兜底
- _read_yaml：配置文件不存在 → ConfigAPIError 404
- _resolve_env_value：空值 → None（未配置语义）
- _provider_key_status：keys 缺失/为空 → 回退顶层 api_key 判定
- _extract_api_key_to_env：keys[0].api_key 为掩码值/示例值 → 从提交中剔除，
  绝不写回 .env / os.environ（防 GET 掩码回路污染）
- save_defaults：defaults 段缺失时创建；chat/embedding/tiers 可空字段部分更新
- update_provider：keys 提交含非 dict 条目 → 原样保留（按条目合并不炸）
- get_llm_presets：声明文件 YAML 损坏 → ConfigAPIError 500（fail-closed）
- get_provider_types：litellm.provider_list 读取失败 → 回退核心类型清单
- get_remote_models：anthropic 类型默认基址与 /v1 归一（三种 api_base 形态）、
  HTTP 错误 → 502（提示手动输入）、载荷无 data 有 models → models 键兜底

外部依赖全桩（httpx / litellm / 文件系统经 tmp_path），绝不触网。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
# routes_llm_config 依赖 plugins/shared 根的共享模块（atomic_io 等），一并入 path
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
    # 隔离副作用：缓存失效与 ConfigCenter reload 均指向测试外的全局态
    m.invalidate_all_llm_caches = None
    m.get_config_center = None
    return m


@pytest.fixture
def llm_yaml(rlc: Any, tmp_path: Path) -> Path:
    """临时 llm.yaml：预置 deepseek 提供商（keys 含占位符）。"""
    path = tmp_path / "llm.yaml"
    path.write_text(
        yaml.dump(
            {
                "providers": {
                    "deepseek": {
                        "type": "deepseek",
                        "api_base": "https://api.deepseek.example",
                        "keys": [{"id": "deepseek_main", "api_key": "${DEEPSEEK_API_KEY}"}],
                    }
                },
                "models": {
                    "deepseek-v4-flash": {"provider": "deepseek", "model_name": "deepseek-v4-flash"}
                },
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    rlc._LLM_YAML = path
    return path


def _on_disk(llm_yaml: Path) -> dict[str, Any]:
    return yaml.safe_load(llm_yaml.read_text(encoding="utf-8"))


# ─────────────────────────── 缓存失效 hook ───────────────────────────


def test_invalidate_llm_caches_invokes_registered_hook(rlc: Any) -> None:
    """config.models 可导入（hook 非 None）→ 写配置后调用失效钩子。"""
    calls: list[bool] = []
    rlc.invalidate_all_llm_caches = lambda: calls.append(True)

    rlc._invalidate_llm_caches()

    assert calls == [True]


def test_invalidate_llm_caches_swallows_hook_failure(
    rlc: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """hook 自身失败 → 吞掉仅告警（配置写入主路径不受缓存失效故障拖累）。"""

    def _boom() -> None:
        raise RuntimeError("cache registry down")

    rlc.invalidate_all_llm_caches = _boom

    rlc._invalidate_llm_caches()  # 不外抛


# ─────────────────────────── 项目根探测兜底 ───────────────────────────


def test_resolve_project_root_falls_back_when_no_config_marker(
    rlc: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """向上找不到 config/models 特征 → 回退 parent×4 语义（不炸、可预期）。"""
    monkeypatch.setattr(
        rlc, "__file__", str(tmp_path / "x" / "y" / "z" / "routes_llm_config.py")
    )

    root = rlc._resolve_project_root()

    assert root == Path(str(tmp_path / "x" / "y" / "z" / "routes_llm_config.py")).resolve().parents[3]
    assert not (root / "config" / "models").is_dir()  # 兜底语义：不再要求特征命中


# ─────────────────────────── YAML 读取 / env 解析 ───────────────────────────


def test_read_yaml_missing_file_raises_404(rlc: Any, tmp_path: Path) -> None:
    with pytest.raises(rlc.ConfigAPIError) as exc_info:
        rlc._read_yaml(tmp_path / "not-exists.yaml")

    assert exc_info.value.status_code == 404
    assert "not-exists.yaml" in exc_info.value.detail


@pytest.mark.parametrize("raw", [None, ""])
def test_resolve_env_value_empty_returns_none(rlc: Any, raw: str | None) -> None:
    """空值/未填 → None（「未配置」语义，绝不能当成字面量 key 发上游）。"""
    assert rlc._resolve_env_value(raw) is None


def test_provider_key_status_falls_back_to_top_level_api_key(
    rlc: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """keys 缺失或为空 → 回退顶层 api_key：明文即已配置；占位符按 env 解析判定。"""
    has_key, env_var = rlc._provider_key_status({"api_key": "sk-plain-123456"})
    assert has_key is True
    assert env_var is None  # 非占位符形态 → 无 env 变量名

    monkeypatch.setenv("MY_TEST_VAR", "real-key")
    has_key, env_var = rlc._provider_key_status(
        {"keys": [], "api_key": "${MY_TEST_VAR}"}
    )
    assert has_key is True  # 占位符可在环境解析 → 已配置
    assert env_var == "MY_TEST_VAR"


# ─────────────────────────── 掩码值防回写 ───────────────────────────


@pytest.mark.parametrize("masked_key", ["sk-ab****wxyz", "your-api-key-here"])
def test_extract_api_key_drops_masked_keys0_value_never_writes_env(
    rlc: Any, tmp_path: Path, masked_key: str
) -> None:
    """keys[0].api_key 为掩码值/示例值 → 从提交中剔除，不写 .env / os.environ。"""
    rlc._ENV_FILE = tmp_path / ".env"
    provider_config: dict[str, Any] = {
        "api_base": "https://relay.example",
        "keys": [{"id": "prov_main", "api_key": masked_key}],
    }

    rlc._extract_api_key_to_env("prov", provider_config)

    assert "api_key" not in provider_config["keys"][0]  # 掩码值被剔除
    assert provider_config["keys"][0] == {"id": "prov_main"}  # 其余字段保留
    assert provider_config["api_base"] == "https://relay.example"  # 顶层字段不受影响
    assert not (tmp_path / ".env").exists()  # 未落盘
    assert "PROV_API_KEY" not in os.environ  # 未同步进程环境


# ─────────────────────────── save_defaults ───────────────────────────


def test_save_defaults_creates_missing_defaults_section(rlc: Any, llm_yaml: Path) -> None:
    """llm.yaml 无 defaults 段 → 创建并写入全量三键。"""
    body = {"chat": "m-chat", "embedding": "m-emb", "tiers": {"fast": "m-lite"}}
    result = rlc.save_defaults(body)

    assert result == body
    disk = _on_disk(llm_yaml)["defaults"]
    assert disk == body  # 全量落盘


def test_save_defaults_partial_update_preserves_other_fields(rlc: Any, llm_yaml: Path) -> None:
    """已有 defaults 时部分更新：只动提交的字段，其余保留（可空字段语义）。"""
    rlc.save_defaults({"chat": "m-chat", "embedding": "e-old", "tiers": {"fast": "m-lite"}})

    result = rlc.save_defaults({"embedding": "e-new"})

    assert result["embedding"] == "e-new"
    assert result["chat"] == "m-chat"  # 未提交字段保留
    assert result["tiers"] == {"fast": "m-lite"}
    assert _on_disk(llm_yaml)["defaults"]["chat"] == "m-chat"


# ─────────────────────────── update_provider：keys 条目合并 ───────────────────────────


def test_update_provider_keeps_non_dict_key_entries(rlc: Any, llm_yaml: Path) -> None:
    """keys 提交含非 dict 条目 → 原样进合并结果（合并循环不炸不丢）。"""
    result = rlc.update_provider("deepseek", {"config": {"keys": ["bogus-entry"]}})

    assert result["providers"]["deepseek"]["keys"] == ["bogus-entry"]
    assert _on_disk(llm_yaml)["providers"]["deepseek"]["keys"] == ["bogus-entry"]


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
