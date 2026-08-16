# @feature: FP-0.2.CFG 配置注入 | @ci: none-local（不在任何 CI 车道：python-coverage 的 BASE_TEST_PATHS 未收集本文件）
"""routes_config.py LLM 提供者管理单元测试（0.2 插件路径）。

覆盖本次「填 Key 即用」改造的核心逻辑：
1. ``_resolve_env_value`` —— ``${VAR}`` → os.environ → .env 文件兜底；
   掩码值 / .env.example 示例值视为未配置
2. ``_provider_key_status`` —— has_key 按解析结果判定（占位符 ≠ 已配置），
   env_var 提取供 UI 提示
3. ``update_provider`` —— 明文 api_key 路由到 .env（yaml 保持占位符）、
   掩码值忽略、keys 条目合并保留未提交字段
4. ``add_provider`` —— 既有 .env 写入行为回归
5. ``get_remote_models`` —— 未配 Key 400；httpx 分流（openai 兼容/anthropic）
6. ``get_provider_types`` —— litellm 动态类型目录

注：tests/test_config_management.py 引用的 0.1 ``channels.api`` 命名空间
已随迁移移除（既有断裂），本文件走 plugins/shared/system/channel_api/。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import HTTPException

# 0.2 插件路径：把 channel_api 目录加入 sys.path（平铺 import：deps/routes_config）
_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANNEL_API_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "channel_api"
if str(_CHANNEL_API_DIR) not in sys.path:
    sys.path.insert(0, str(_CHANNEL_API_DIR))

import routes_config as rc
from routes_config import (
    ProviderConfigUpdateRequest,
    ProviderCreateRequest,
)

_ENV_VARS_TO_CLEAN = ["P1_API_KEY", "NEWP_API_KEY", "RC_TEST_ENV_VAR"]


def _write_llm_yaml(path: Path, providers: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "models": {},
        "providers": providers,
        "defaults": {"chat": "", "embedding": "", "tiers": {}},
    }
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


@pytest.fixture
def isolated_rc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> rc:
    """隔离的 routes_config：llm.yaml / .env 指向临时文件。"""
    llm_yaml = tmp_path / "llm.yaml"
    env_file = tmp_path / ".env"
    _write_llm_yaml(
        llm_yaml,
        {
            "p1": {
                "type": "openai",
                "api_base": "https://api.p1.com/v1",
                "keys": [
                    {
                        "id": "p1_main",
                        "api_key": "${P1_API_KEY}",
                        "max_concurrent": 6,
                        "rpm": 10,
                        "token_quota": 0,
                    }
                ],
            },
            "p-anthropic": {
                "type": "anthropic",
                "api_base": "https://api.anthropic.com",
                "keys": [{"id": "pa_main", "api_key": "sk-real-anthropic-key"}],
            },
        },
    )
    monkeypatch.setattr(rc, "_LLM_YAML", llm_yaml)
    monkeypatch.setattr(rc, "_ENV_FILE", env_file)
    monkeypatch.setattr(rc, "_env_file_cache", None)
    for var in _ENV_VARS_TO_CLEAN:
        monkeypatch.delenv(var, raising=False)
    return rc


# ===================================================================
# 1. _resolve_env_value
# ===================================================================


class TestResolveEnvValue:
    def test_plaintext_passthrough(self, isolated_rc: rc) -> None:
        assert isolated_rc._resolve_env_value("sk-literal-key") == "sk-literal-key"

    def test_env_var_resolved(self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RC_TEST_ENV_VAR", "from-env")
        assert isolated_rc._resolve_env_value("${RC_TEST_ENV_VAR}") == "from-env"

    def test_env_file_fallback(self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch) -> None:
        """os.environ 未命中时回退读 .env 文件（UI 填 Key 免重启的关键）。"""
        monkeypatch.delenv("P1_API_KEY", raising=False)
        isolated_rc._ENV_FILE.write_text("P1_API_KEY=sk-from-env-file\n", encoding="utf-8")
        isolated_rc._env_file_cache = None  # 重置 mtime 缓存
        assert isolated_rc._resolve_env_value("${P1_API_KEY}") == "sk-from-env-file"

    def test_undefined_returns_none(self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("P1_API_KEY", raising=False)
        assert isolated_rc._resolve_env_value("${P1_API_KEY}") is None

    def test_masked_value_is_none(self, isolated_rc: rc) -> None:
        assert isolated_rc._resolve_env_value("sk-1********abcd") is None

    def test_example_placeholder_is_none(self, isolated_rc: rc) -> None:
        """.env 中是 .env.example 式示例值（your- 开头）同样视为未配置。"""
        assert isolated_rc._resolve_env_value("your-openai-api-key") is None
        isolated_rc._ENV_FILE.write_text("P1_API_KEY=your-key\n", encoding="utf-8")
        isolated_rc._env_file_cache = None
        assert isolated_rc._resolve_env_value("${P1_API_KEY}") is None


# ===================================================================
# 2. _provider_key_status / has_key
# ===================================================================


class TestProviderKeyStatus:
    def test_unresolved_placeholder_is_unconfigured(
        self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("P1_API_KEY", raising=False)
        pconf = {"keys": [{"api_key": "${P1_API_KEY}"}]}
        has_key, env_var = isolated_rc._provider_key_status(pconf)
        assert has_key is False
        assert env_var == "P1_API_KEY"

    def test_resolved_placeholder_is_configured(
        self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("P1_API_KEY", "sk-real")
        has_key, env_var = isolated_rc._provider_key_status({"keys": [{"api_key": "${P1_API_KEY}"}]})
        assert has_key is True
        assert env_var == "P1_API_KEY"

    def test_plaintext_key_configured_without_env_var(self, isolated_rc: rc) -> None:
        has_key, env_var = isolated_rc._provider_key_status({"keys": [{"api_key": "sk-real"}]})
        assert has_key is True
        assert env_var is None

    def test_get_llm_config_carries_status(self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("P1_API_KEY", raising=False)
        data = isolated_rc.get_llm_config()
        assert data["providers"]["p1"]["has_key"] is False
        assert data["providers"]["p1"]["env_var"] == "P1_API_KEY"
        assert data["providers"]["p-anthropic"]["has_key"] is True
        # api_key 脱敏
        assert "sk-real" not in str(data)


# ===================================================================
# 3. update_provider：明文 → .env；掩码忽略；keys 合并
# ===================================================================


class TestUpdateProvider:
    def test_plaintext_key_routed_to_env(self, isolated_rc: rc) -> None:
        """「更新 Key」提交明文 keys[0].api_key → 写 .env，yaml 保持占位符。"""
        isolated_rc.update_provider(
            "p1",
            ProviderConfigUpdateRequest(config={"keys": [{"id": "p1_main", "api_key": "sk-plain-new-key"}]}),
        )
        env_text = isolated_rc._ENV_FILE.read_text(encoding="utf-8")
        assert "P1_API_KEY=sk-plain-new-key" in env_text
        data = yaml.safe_load(isolated_rc._LLM_YAML.read_text(encoding="utf-8"))
        assert data["providers"]["p1"]["keys"][0]["api_key"] == "${P1_API_KEY}"
        # 同步进程环境（本进程 has_key 即时生效）
        import os

        assert os.environ.get("P1_API_KEY") == "sk-plain-new-key"

    def test_masked_value_ignored(self, isolated_rc: rc) -> None:
        """GET 返回的掩码值被回传保存时忽略——不写 .env、不污染 yaml。"""
        isolated_rc.update_provider(
            "p1",
            ProviderConfigUpdateRequest(config={"keys": [{"id": "p1_main", "api_key": "sk-1********abcd"}]}),
        )
        assert not isolated_rc._ENV_FILE.exists()
        data = yaml.safe_load(isolated_rc._LLM_YAML.read_text(encoding="utf-8"))
        assert data["providers"]["p1"]["keys"][0]["api_key"] == "${P1_API_KEY}"

    def test_keys_merge_preserves_unsubmitted_fields(self, isolated_rc: rc) -> None:
        """只提交 max_concurrent/rpm（不带 api_key）→ 占位符与 token_quota 保留。"""
        isolated_rc.update_provider(
            "p1",
            ProviderConfigUpdateRequest(
                config={"keys": [{"id": "p1_main", "max_concurrent": 9, "rpm": 99}]}
            ),
        )
        data = yaml.safe_load(isolated_rc._LLM_YAML.read_text(encoding="utf-8"))
        key0 = data["providers"]["p1"]["keys"][0]
        assert key0["api_key"] == "${P1_API_KEY}"
        assert key0["max_concurrent"] == 9
        assert key0["rpm"] == 99
        assert key0["token_quota"] == 0

    def test_404_for_unknown_provider(self, isolated_rc: rc) -> None:
        with pytest.raises(HTTPException) as e:
            isolated_rc.update_provider("nope", ProviderConfigUpdateRequest(config={"rpm": 5}))
        assert e.value.status_code == 404


# ===================================================================
# 4. add_provider 回归
# ===================================================================


class TestAddProvider:
    def test_api_key_routed_to_env(self, isolated_rc: rc) -> None:
        isolated_rc.add_provider(
            ProviderCreateRequest(
                provider_id="newp",
                config={"type": "openai", "api_base": "https://api.n.com/v1", "api_key": "sk-new-plain"},
            ),
        )
        assert "NEWP_API_KEY=sk-new-plain" in isolated_rc._ENV_FILE.read_text(encoding="utf-8")
        data = yaml.safe_load(isolated_rc._LLM_YAML.read_text(encoding="utf-8"))
        assert data["providers"]["newp"]["keys"][0]["api_key"] == "${NEWP_API_KEY}"

    def test_duplicate_409(self, isolated_rc: rc) -> None:
        with pytest.raises(HTTPException) as e:
            isolated_rc.add_provider(
                ProviderCreateRequest(provider_id="p1", config={"type": "openai"})
            )
        assert e.value.status_code == 409


# ===================================================================
# 5. get_remote_models
# ===================================================================


class TestRemoteModels:
    def test_no_key_400(self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("P1_API_KEY", raising=False)
        with pytest.raises(HTTPException) as e:
            isolated_rc.get_remote_models("p1")
        assert e.value.status_code == 400

    def test_unknown_provider_404(self, isolated_rc: rc) -> None:
        with pytest.raises(HTTPException) as e:
            isolated_rc.get_remote_models("ghost")
        assert e.value.status_code == 404

    def test_anthropic_fetch(self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch) -> None:
        """anthropic 类型：GET {api_base}/v1/models + x-api-key 头，data[].id 排序列表。"""
        captured: dict[str, Any] = {}

        class FakeResp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return {"data": [{"id": "m-b"}, {"id": "m-a", "owned_by": "owner-x"}]}

        def fake_get(url: str, headers: dict[str, str] | None = None, timeout: float = 0) -> FakeResp:
            captured["url"] = url
            captured["headers"] = headers or {}
            captured["timeout"] = timeout
            return FakeResp()

        monkeypatch.setattr("httpx.get", fake_get)
        result = isolated_rc.get_remote_models("p-anthropic")  # 明文 key 的 anthropic 也走 anthropic 分支
        # p-anthropic 是 anthropic 类型 → anthropic 分支
        assert captured["url"] == "https://api.anthropic.com/v1/models"
        assert captured["headers"]["x-api-key"] == "sk-real-anthropic-key"
        assert "anthropic-version" in captured["headers"]
        assert [m["id"] for m in result["models"]] == ["m-a", "m-b"]  # 排序
        assert result["models"][0]["owned_by"] == "owner-x"

    def test_openai_type_fetch(self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch) -> None:
        """type=openai 的提供者走 {api_base}/models + Bearer。"""
        import os

        monkeypatch.setenv("P1_API_KEY", "sk-ok")
        captured: dict[str, Any] = {}

        class FakeResp:
            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict[str, Any]:
                return {"data": [{"id": "gpt-x"}]}

        def fake_get(url: str, headers: dict[str, str] | None = None, timeout: float = 0) -> FakeResp:
            captured["url"] = url
            captured["headers"] = headers or {}
            return FakeResp()

        monkeypatch.setattr("httpx.get", fake_get)
        isolated_rc.get_remote_models("p1")
        assert captured["url"] == "https://api.p1.com/v1/models"
        assert captured["headers"]["Authorization"] == "Bearer sk-ok"
        assert os.environ.get("P1_API_KEY") == "sk-ok"

    def test_upstream_error_502(self, isolated_rc: rc, monkeypatch: pytest.MonkeyPatch) -> None:
        import httpx

        monkeypatch.setenv("P1_API_KEY", "sk-ok")

        def fake_get(url: str, **kwargs: Any) -> Any:
            resp = httpx.Response(403, request=httpx.Request("GET", url))
            raise httpx.HTTPStatusError("403", request=resp, response=resp)

        monkeypatch.setattr("httpx.get", fake_get)
        with pytest.raises(HTTPException) as e:
            isolated_rc.get_remote_models("p1")
        assert e.value.status_code == 502


# ===================================================================
# 6. get_provider_types（litellm 动态目录）
# ===================================================================


def test_get_provider_types_from_litellm() -> None:
    result = rc.get_provider_types()
    assert "openai" in result["types"]
    assert "anthropic" in result["types"]
    assert result["types"] == sorted(result["types"])
