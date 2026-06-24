"""配置管理体系测试 — Provider CRUD、API Key 脱敏、白名单读写、update_provider 修复验证。

测试范围：
1. Provider CRUD API 端点（GET/PUT + 404 语义）
2. API Key 脱敏逻辑
3. update_provider 隐式创建 → 404 修复验证
4. update_provider 脱敏值覆盖明文密钥防护
5. 配置白名单读写（generic 端点）
6. .env/YAML 文件写入集成测试
"""
from __future__ import annotations

import pytest
import yaml
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixture — 隔离 YAML 路径 + 提供认证的 TestClient
# ---------------------------------------------------------------------------

DEMO_CREDENTIALS = {"username": "demo", "password": "demo12345"}


@pytest.fixture
def llm_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """将 LLM YAML 隔离到临时目录，避免污染真实配置。"""
    yaml_path = tmp_path / "llm.yaml"
    initial_data = {
        "providers": {
            "openai": {"api_base": "https://api.openai.com/v1", "api_key": "sk-real-secret-key-123"},
            "zhipu": {"api_base": "https://open.bigmodel.cn/api/paas/v4", "api_key": "zhipu-real-key-999"},
        },
        "models": {
            "gpt-4o": {"provider": "openai", "model_name": "gpt-4o-2024-08-06", "display_name": "GPT-4o"},
            "glm-4": {"provider": "zhipu", "model_name": "glm-4", "display_name": "GLM-4"},
        },
        "defaults": {"chat": "gpt-4o", "embedding": "", "tiers": {"large": "gpt-4o"}},
    }
    with open(yaml_path, "w", encoding="utf-8") as f:
        yaml.dump(initial_data, f, allow_unicode=True)
    monkeypatch.setattr("channels.api.routes_config._LLM_YAML", yaml_path)
    return yaml_path


@pytest.fixture
def generic_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离通用配置白名单 YAML 到临时目录。"""
    yaml_path = tmp_path / "test_generic.yaml"

    # 在白名单中注入测试路径
    from channels.api import routes_config

    original_whitelist = dict(routes_config._GENERIC_CONFIG_WHITELIST)
    routes_config._GENERIC_CONFIG_WHITELIST["test/unit_test"] = yaml_path
    yield yaml_path
    # 恢复白名单
    routes_config._GENERIC_CONFIG_WHITELIST.clear()
    routes_config._GENERIC_CONFIG_WHITELIST.update(original_whitelist)


@pytest.fixture
def patched_config_center(monkeypatch: pytest.MonkeyPatch):
    """Mock ConfigCenter.reload 避免实际重载。"""
    mock_cc = type("MockCC", (), {"reload": lambda self, path: {"config_type": "mock", "success": True}})()
    monkeypatch.setattr("channels.api.routes_config.get_config_center", lambda: mock_cc)
    return mock_cc


@pytest.fixture
def client(
    llm_yaml: Path,
    patched_config_center: Any,
) -> TestClient:
    """提供配置好隔离的 FastAPI TestClient。"""
    from channels.api.app import create_app
    return TestClient(create_app())


@pytest.fixture
def auth_headers(client: TestClient) -> dict[str, str]:
    """登录 demo 用户，返回认证头。"""
    resp = client.post("/api/v1/auth/login", json=DEMO_CREDENTIALS)
    assert resp.status_code == 200, f"登录失败: {resp.text}"
    token = resp.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Provider CRUD API 端点测试
# ---------------------------------------------------------------------------

class TestProviderCRUD:
    """Provider CRUD 操作 API 测试。"""

    def test_get_providers_returns_list(self, client: TestClient, auth_headers: dict[str, str]):
        """GET /llm/providers 返回提供商列表（含 has_key 状态）。"""
        resp = client.get("/api/v1/config/llm/providers", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        assert "openai" in data["providers"]
        assert data["providers"]["openai"]["has_key"] is True
        assert "api_base" in data["providers"]["openai"]
        # 不应暴露明文 api_key
        assert "api_key" not in data["providers"]["openai"]

    def test_get_llm_config_masks_api_keys(self, client: TestClient, auth_headers: dict[str, str]):
        """GET /llm 返回的 providers 中 api_key 应被脱敏。"""
        resp = client.get("/api/v1/config/llm", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        openai_key = data["providers"]["openai"]["api_key"]
        # 脱敏值不应包含明文
        assert "sk-real-secret-key-123" not in openai_key
        assert "********" in openai_key or openai_key == "****"

    def test_update_provider_existing_success(self, client: TestClient, auth_headers: dict[str, str]):
        """PUT 更新已存在的 provider 成功，字段合并到现有配置。"""
        resp = client.put(
            "/api/v1/config/llm/providers/openai",
            json={"config": {"api_base": "https://new.openai.com/v2", "timeout": 30}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["providers"]["openai"]["api_base"] == "https://new.openai.com/v2"
        assert data["providers"]["openai"]["timeout"] == 30

    def test_update_provider_not_found_returns_404(self, client: TestClient, auth_headers: dict[str, str]):
        """更新不存在的 provider 应返回 404，而非隐式创建。"""
        resp = client.put(
            "/api/v1/config/llm/providers/nonexistent",
            json={"config": {"api_key": "new-key"}},
            headers=auth_headers,
        )
        assert resp.status_code == 404
        assert "不存在" in resp.json()["detail"]

    def test_update_provider_not_found_does_not_create(self, client: TestClient, auth_headers: dict[str, str], llm_yaml: Path):
        """404 响应后，provider 不应被写入文件。"""
        client.put(
            "/api/v1/config/llm/providers/ghost",
            json={"config": {"api_key": "ghost-key"}},
            headers=auth_headers,
        )
        with open(llm_yaml, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert "ghost" not in saved.get("providers", {})


# ---------------------------------------------------------------------------
# API Key 脱敏 + 脱敏值覆盖防护测试
# ---------------------------------------------------------------------------

class TestApiKeyMasking:
    """API Key 脱敏逻辑和覆盖防护。"""

    def test_mask_key_short(self):
        """短 key（≤8 字符）统一脱敏为 ****。"""
        from channels.api.routes_config import _mask_key
        assert _mask_key("short") == "****"
        assert _mask_key("") == ""

    def test_mask_key_long(self):
        """长 key 保留首尾各 4 位，中间替换为 ********。"""
        from channels.api.routes_config import _mask_key
        masked = _mask_key("sk-abcdef1234567890")
        assert masked.startswith("sk-a")
        assert masked.endswith("7890")
        assert "********" in masked

    def test_is_masked_key_detects_mask(self):
        """_is_masked_key 正确识别脱敏值。"""
        from channels.api.routes_config import _is_masked_key
        assert _is_masked_key("sk-a********7890") is True
        assert _is_masked_key("sk-real-plaintext") is False
        assert _is_masked_key("") is False

    def test_update_provider_rejects_masked_api_key(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        llm_yaml: Path,
    ):
        """PUT 携带脱敏 api_key 时不覆盖明文密钥。"""
        # 先获取脱敏值
        get_resp = client.get("/api/v1/config/llm", headers=auth_headers)
        masked_key = get_resp.json()["providers"]["openai"]["api_key"]

        # 用脱敏值 PUT
        put_resp = client.put(
            "/api/v1/config/llm/providers/openai",
            json={"config": {"api_key": masked_key}},
            headers=auth_headers,
        )
        assert put_resp.status_code == 200

        # 验证文件中明文密钥未被覆盖
        with open(llm_yaml, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["providers"]["openai"]["api_key"] == "sk-real-secret-key-123"

    def test_update_provider_accepts_real_api_key(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        llm_yaml: Path,
    ):
        """PUT 携带明文 api_key 时正常更新。"""
        put_resp = client.put(
            "/api/v1/config/llm/providers/openai",
            json={"config": {"api_key": "sk-brand-new-real-key"}},
            headers=auth_headers,
        )
        assert put_resp.status_code == 200

        with open(llm_yaml, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["providers"]["openai"]["api_key"] == "sk-brand-new-real-key"


# ---------------------------------------------------------------------------
# Model CRUD API 测试
# ---------------------------------------------------------------------------

class TestModelCRUD:
    """Model CRUD 操作 API 测试。"""

    def test_get_models_masks_keys(self, client: TestClient, auth_headers: dict[str, str]):
        """GET /llm/models 返回脱敏的 api_key。"""
        resp = client.get("/api/v1/config/llm/models", headers=auth_headers)
        assert resp.status_code == 200
        models = resp.json()["models"]
        assert "gpt-4o" in models
        # 不应含明文
        for mid, mconf in models.items():
            if "api_key" in mconf:
                assert "********" in mconf["api_key"] or mconf["api_key"] == "****"

    def test_add_model_success(self, client: TestClient, auth_headers: dict[str, str]):
        """POST 添加新模型成功。"""
        resp = client.post(
            "/api/v1/config/llm/models",
            json={"models": {"claude-3.5": {"provider": "anthropic", "model_name": "claude-3-5-sonnet"}}},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "claude-3.5" in resp.json()["models"]

    def test_update_model_not_found_returns_404(self, client: TestClient, auth_headers: dict[str, str]):
        """更新不存在的模型返回 404。"""
        resp = client.put(
            "/api/v1/config/llm/models/nonexistent-model",
            json={"config": {"temperature": 0.5}},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_delete_model_success(self, client: TestClient, auth_headers: dict[str, str]):
        """DELETE 删除已存在的模型成功。"""
        resp = client.delete("/api/v1/config/llm/models/glm-4", headers=auth_headers)
        assert resp.status_code == 200
        assert "glm-4" not in resp.json()["models"]

    def test_delete_model_not_found_returns_404(self, client: TestClient, auth_headers: dict[str, str]):
        """删除不存在的模型返回 404。"""
        resp = client.delete("/api/v1/config/llm/models/nonexistent-model", headers=auth_headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# LLM Defaults 读写测试
# ---------------------------------------------------------------------------

class TestDefaultsRW:
    """LLM 默认模型配置读写。"""

    def test_get_defaults(self, client: TestClient, auth_headers: dict[str, str]):
        """GET /llm/defaults 返回默认配置。"""
        resp = client.get("/api/v1/config/llm/defaults", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["chat"] == "gpt-4o"
        assert "tiers" in data

    def test_put_defaults_updates_fields(self, client: TestClient, auth_headers: dict[str, str]):
        """PUT /llm/defaults 更新默认模型。"""
        resp = client.put(
            "/api/v1/config/llm/defaults",
            json={"chat": "glm-4", "embedding": "text-embedding-3"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["chat"] == "glm-4"
        assert data["embedding"] == "text-embedding-3"


# ---------------------------------------------------------------------------
# 配置白名单读写测试
# ---------------------------------------------------------------------------

class TestGenericConfigWhitelist:
    """通用配置白名单读端点测试。"""

    def test_get_generic_unknown_path_returns_404(self, client: TestClient, auth_headers: dict[str, str]):
        """GET 未注册的配置路径返回 404。"""
        resp = client.get("/api/v1/config/generic/invalid/path", headers=auth_headers)
        assert resp.status_code == 404

    def test_put_generic_unknown_path_returns_404(self, client: TestClient, auth_headers: dict[str, str]):
        """PUT 未注册的配置路径返回 404。"""
        resp = client.put(
            "/api/v1/config/generic/invalid/path",
            json={"data": {}},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    def test_generic_config_rw_roundtrip(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        generic_yaml: Path,
    ):
        """白名单内的配置 PUT→GET 回环一致。"""
        config_data = {
            "enabled": True,
            "retry_count": 5,
            "label": "测试标签",
            "nested": {"timeout": 60},
        }

        put_resp = client.put(
            "/api/v1/config/generic/test/unit_test",
            json={"data": config_data},
            headers=auth_headers,
        )
        assert put_resp.status_code == 200
        assert put_resp.json() == config_data

        get_resp = client.get("/api/v1/config/generic/test/unit_test", headers=auth_headers)
        assert get_resp.status_code == 200
        assert get_resp.json() == config_data

    def test_generic_config_file_written_to_disk(
        self,
        client: TestClient,
        auth_headers: dict[str, str],
        generic_yaml: Path,
    ):
        """PUT 后配置确实写入磁盘。"""
        config_data = {"flag": True, "value": 42}
        client.put(
            "/api/v1/config/generic/test/unit_test",
            json={"data": config_data},
            headers=auth_headers,
        )
        assert generic_yaml.exists()
        with open(generic_yaml, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved == config_data


# ---------------------------------------------------------------------------
# 认证保护测试
# ---------------------------------------------------------------------------

class TestAuthProtection:
    """验证配置端点需要认证。"""

    def test_get_llm_without_auth_returns_401(self, client: TestClient):
        """无认证访问配置端点返回 401。"""
        resp = client.get("/api/v1/config/llm")
        assert resp.status_code == 401

    def test_put_provider_without_auth_returns_401(self, client: TestClient):
        """无认证更新提供商返回 401。"""
        resp = client.put(
            "/api/v1/config/llm/providers/openai",
            json={"config": {"timeout": 30}},
        )
        assert resp.status_code == 401
