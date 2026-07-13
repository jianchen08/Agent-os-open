"""配置管理 API 测试。

覆盖 routes_config.py 的四个核心功能域：
1. Provider CRUD — 创建/读取/更新/删除，含 update_provider 404 回归测试
2. .env 写入 — 创建 provider 时 api_key 自动写入 .env，yaml 中改为 ${VAR} 引用
3. API Key 脱敏 — GET llm 配置时 api_key 被掩码，不泄露明文
4. 白名单读写 — generic config 端点对未知路径返回 404，合法路径正常读写
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _write_test_llm_yaml(path: Path) -> None:
    """写入测试用 llm.yaml，包含已知 api_key 以验证脱敏。"""
    data: dict[str, Any] = {
        "models": {
            "test-model": {
                "provider": "test-provider",
                "model_name": "test-model",
                "api_key": "sk-test-key-1234567890",
                "context_window": 4096,
            },
        },
        "providers": {
            "test-provider": {
                "type": "openai",
                "api_base": "https://api.test.com/v1",
                "keys": [
                    {
                        "id": "test-provider_main",
                        "api_key": "sk-provider-key-abcdef",
                    }
                ],
            },
            "empty-provider": {
                "type": "deepseek",
                "api_base": "https://api.empty.com/v1",
                "keys": [],
            },
        },
        "defaults": {
            "chat": "test-model",
            "embedding": "",
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


def _mask_key(key: str) -> str:
    """与 routes_config._mask_key 相同的掩码逻辑，用于测试断言。"""
    if not key or len(key) <= 8:
        return "****" if key else ""
    return f"{key[:4]}{'*' * 8}{key[-4:]}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> TestClient:
    """创建隔离的 TestClient（最小 FastAPI 应用，仅挂载 config 路由）。

    - 跳过认证（patch get_current_user 让任意 token 通过）
    - llm.yaml 指向临时文件，含预置测试数据
    - .env 指向临时文件

    注：不使用完整 create_app()，避免引入 av/cv2 等可选媒体依赖。
    """
    # 跳过认证
    monkeypatch.setattr(
        "channels.api.deps.get_current_user",
        lambda token: {"sub": "test-user", "username": "tester"},
    )

    # 隔离 llm.yaml
    test_llm = tmp_path / "llm.yaml"
    _write_test_llm_yaml(test_llm)
    monkeypatch.setattr("channels.api.routes_config._LLM_YAML", test_llm)

    # 隔离 .env
    test_env = tmp_path / ".env"
    monkeypatch.setattr("channels.api.routes_config._ENV_FILE", test_env)

    # 构造最小应用：仅挂载 config 路由 + 错误处理器
    from fastapi import FastAPI

    from channels.api.deps import (
        APIError,
        api_error_handler,
    )
    from channels.api.routes_config import router as config_router

    app = FastAPI()
    app.add_exception_handler(APIError, api_error_handler)
    app.include_router(config_router)
    client = TestClient(app)
    # 默认携带 Bearer token（mock 的 get_current_user 会返回固定用户信息）
    client.headers.update({"Authorization": "Bearer test-token"})
    return client


# ===================================================================
# 1. Provider CRUD
# ===================================================================

class TestProviderCRUD:
    """Provider 增删改查完整流程测试。"""

    def test_get_providers_returns_list(self, config_client: TestClient) -> None:
        """GET /llm/providers 返回所有 provider 的精简信息。"""
        resp = config_client.get("/api/v1/config/llm/providers")
        assert resp.status_code == 200
        data = resp.json()
        assert "providers" in data
        assert "test-provider" in data["providers"]
        assert "empty-provider" in data["providers"]
        # test-provider 有 key → has_key=True
        assert data["providers"]["test-provider"]["has_key"] is True
        # empty-provider 无 key → has_key=False
        assert data["providers"]["empty-provider"]["has_key"] is False

    def test_create_provider_success(self, config_client: TestClient) -> None:
        """POST /llm/providers 创建新 provider 成功。"""
        resp = config_client.post(
            "/api/v1/config/llm/providers",
            json={
                "provider_id": "new-provider",
                "config": {
                    "type": "openai",
                    "api_base": "https://api.new.com/v1",
                },
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "new-provider" in data["providers"]
        assert data["providers"]["new-provider"]["api_base"] == "https://api.new.com/v1"

    def test_create_provider_duplicate_returns_409(
        self, config_client: TestClient,
    ) -> None:
        """创建已存在的 provider_id 应返回 409 Conflict。"""
        resp = config_client.post(
            "/api/v1/config/llm/providers",
            json={
                "provider_id": "test-provider",
                "config": {"type": "openai"},
            },
        )
        assert resp.status_code == 409

    def test_update_provider_success(self, config_client: TestClient) -> None:
        """PUT /llm/providers/{id} 更新已存在 provider 成功。"""
        resp = config_client.put(
            "/api/v1/config/llm/providers/test-provider",
            json={"config": {"api_base": "https://api.updated.com/v1"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["providers"]["test-provider"]["api_base"] == "https://api.updated.com/v1"
        # 原有字段应保留
        assert data["providers"]["test-provider"]["type"] == "openai"

    def test_update_provider_not_found_returns_404(
        self, config_client: TestClient,
    ) -> None:
        """PUT 不存在的 provider_id 必须返回 404，不能隐式创建。

        这是 Must Fix 回归测试：修复前会隐式创建空 provider 并返回 200。
        """
        resp = config_client.put(
            "/api/v1/config/llm/providers/nonexistent-provider",
            json={"config": {"api_base": "https://api.fake.com/v1"}},
        )
        assert resp.status_code == 404
        # 验证没有隐式创建
        detail = resp.json().get("detail", "")
        assert "不存在" in detail

    def test_delete_provider_success(self, config_client: TestClient) -> None:
        """DELETE /llm/providers/{id} 删除已存在 provider 成功。"""
        resp = config_client.delete("/api/v1/config/llm/providers/test-provider")
        assert resp.status_code == 200
        data = resp.json()
        assert "test-provider" not in data["providers"]

    def test_delete_provider_not_found_returns_404(
        self, config_client: TestClient,
    ) -> None:
        """DELETE 不存在的 provider_id 应返回 404。"""
        resp = config_client.delete(
            "/api/v1/config/llm/providers/nonexistent-provider",
        )
        assert resp.status_code == 404


# ===================================================================
# 2. .env 写入
# ===================================================================

class TestEnvWrite:
    """创建 provider 时 api_key 自动写入 .env 的测试。"""

    def test_create_provider_with_api_key_writes_env(
        self,
        config_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """创建含 api_key 的 provider 时，密钥写入 .env 文件。"""
        env_file = tmp_path / ".env"
        # config_client fixture 已将 _ENV_FILE 指向 tmp_path / ".env"
        # 直接读取该路径

        config_client.post(
            "/api/v1/config/llm/providers",
            json={
                "provider_id": "deepseek",
                "config": {
                    "type": "deepseek",
                    "api_base": "https://api.deepseek.com/v1",
                    "api_key": "sk-real-secret-key",
                },
            },
        )

        # 验证 .env 文件包含正确的环境变量
        assert env_file.exists(), ".env 文件未创建"
        env_content = env_file.read_text(encoding="utf-8")
        assert "DEEPSEEK_API_KEY=sk-real-secret-key" in env_content, (
            f".env 中未找到 DEEPSEEK_API_KEY: {env_content}"
        )

    def test_create_provider_env_reference_in_yaml(
        self,
        config_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """创建含 api_key 的 provider 后，llm.yaml 中使用 ${VAR} 引用而非明文。"""
        llm_file = tmp_path / "llm.yaml"

        config_client.post(
            "/api/v1/config/llm/providers",
            json={
                "provider_id": "openai_custom",
                "config": {
                    "type": "openai",
                    "api_base": "https://api.openai.com/v1",
                    "api_key": "sk-openai-secret",
                },
            },
        )

        # 读取 llm.yaml 验证密钥引用格式
        with open(llm_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        provider = data["providers"]["openai_custom"]
        keys = provider.get("keys", [])
        assert len(keys) == 1, "应有一个 key 条目"
        api_key_val = keys[0]["api_key"]
        assert api_key_val == "${OPENAI_CUSTOM_API_KEY}", (
            f"api_key 应为 ${{OPENAI_CUSTOM_API_KEY}} 引用，实际: {api_key_val}"
        )
        # 明文不应出现在 yaml 中
        assert "sk-openai-secret" not in yaml.dump(data), (
            "明文密钥不应残留在 llm.yaml 中"
        )

    def test_create_provider_without_api_key_skips_env(
        self,
        config_client: TestClient,
        tmp_path: Path,
    ) -> None:
        """创建不含 api_key 的 provider 时，不写 .env 文件。"""
        env_file = tmp_path / ".env"

        config_client.post(
            "/api/v1/config/llm/providers",
            json={
                "provider_id": "no-key-provider",
                "config": {
                    "type": "openai",
                    "api_base": "https://api.noverify.com/v1",
                },
            },
        )

        assert not env_file.exists(), "无 api_key 时不应创建 .env 文件"


# ===================================================================
# 3. API Key 脱敏
# ===================================================================

class TestKeyMasking:
    """GET 配置时 api_key 被掩码处理的测试。"""

    def test_get_llm_config_masks_provider_keys(
        self, config_client: TestClient,
    ) -> None:
        """GET /llm 返回的 provider keys 中 api_key 被掩码。"""
        resp = config_client.get("/api/v1/config/llm")
        assert resp.status_code == 200
        data = resp.json()

        provider = data["providers"]["test-provider"]
        keys = provider.get("keys", [])
        assert len(keys) == 1

        original_key = "sk-provider-key-abcdef"
        expected_masked = _mask_key(original_key)
        actual_key = keys[0]["api_key"]

        assert actual_key == expected_masked, (
            f"provider api_key 应被掩码为 {expected_masked}，实际: {actual_key}"
        )
        assert original_key not in actual_key, "掩码后的值不应包含原始密钥"

    def test_get_llm_config_masks_model_keys(
        self, config_client: TestClient,
    ) -> None:
        """GET /llm 返回的 model api_key 被掩码。"""
        resp = config_client.get("/api/v1/config/llm")
        assert resp.status_code == 200
        data = resp.json()

        model = data["models"]["test-model"]
        original_key = "sk-test-key-1234567890"
        expected_masked = _mask_key(original_key)
        actual_key = model["api_key"]

        assert actual_key == expected_masked, (
            f"model api_key 应被掩码为 {expected_masked}，实际: {actual_key}"
        )
        assert original_key not in actual_key, "掩码后的值不应包含原始密钥"

    def test_get_models_masks_keys(self, config_client: TestClient) -> None:
        """GET /llm/models 返回的 api_key 被掩码。"""
        resp = config_client.get("/api/v1/config/llm/models")
        assert resp.status_code == 200
        data = resp.json()

        model = data["models"]["test-model"]
        original_key = "sk-test-key-1234567890"
        actual_key = model["api_key"]

        assert actual_key == _mask_key(original_key), "model api_key 应被掩码"
        assert original_key not in actual_key, "不应泄露原始密钥"

    def test_get_providers_does_not_leak_keys(
        self, config_client: TestClient,
    ) -> None:
        """GET /llm/providers 不返回 api_key 明文，仅返回 has_key 布尔值。"""
        resp = config_client.get("/api/v1/config/llm/providers")
        assert resp.status_code == 200
        data = resp.json()

        for pid, pconf in data["providers"].items():
            assert "api_key" not in pconf, (
                f"provider {pid} 的精简信息不应包含 api_key 字段"
            )
            assert "has_key" in pconf, f"provider {pid} 应包含 has_key 字段"


# ===================================================================
# 4. 白名单读写
# ===================================================================

class TestGenericWhitelist:
    """generic config 端点的白名单校验测试。"""

    def test_get_generic_unknown_path_returns_404(
        self, config_client: TestClient,
    ) -> None:
        """GET 未在白名单中的配置路径返回 404。"""
        resp = config_client.get("/api/v1/config/generic/nonexistent/path")
        assert resp.status_code == 404

    def test_put_generic_unknown_path_returns_404(
        self, config_client: TestClient,
    ) -> None:
        """PUT 未在白名单中的配置路径返回 404。"""
        resp = config_client.put(
            "/api/v1/config/generic/nonexistent/path",
            json={"data": {"key": "value"}},
        )
        assert resp.status_code == 404

    def test_get_generic_valid_path_returns_config(
        self,
        config_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """GET 白名单中的合法路径返回配置内容。"""
        import channels.api.routes_config as rc

        # 创建临时配置文件
        test_yaml = tmp_path / "whitelist_test.yaml"
        test_data = {"setting": "test-value", "nested": {"key": 123}}
        test_yaml.write_text(
            yaml.dump(test_data, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )

        # 注册到白名单
        monkeypatch.setitem(
            rc._GENERIC_CONFIG_WHITELIST,
            "test/whitelist_read",
            test_yaml,
        )

        resp = config_client.get("/api/v1/config/generic/test/whitelist_read")
        assert resp.status_code == 200
        assert resp.json() == test_data

    def test_put_generic_valid_path_writes_config(
        self,
        config_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """PUT 白名单中的合法路径写入配置并返回写入内容。"""
        import channels.api.routes_config as rc

        test_yaml = tmp_path / "whitelist_put.yaml"
        monkeypatch.setitem(
            rc._GENERIC_CONFIG_WHITELIST,
            "test/whitelist_write",
            test_yaml,
        )

        new_data = {"new_key": "new_value", "enabled": True}
        resp = config_client.put(
            "/api/v1/config/generic/test/whitelist_write",
            json={"data": new_data},
        )
        assert resp.status_code == 200
        assert resp.json() == new_data

        # 验证文件实际写入磁盘
        assert test_yaml.exists(), "配置文件未写入磁盘"
        with open(test_yaml, encoding="utf-8") as f:
            file_data = yaml.safe_load(f)
        assert file_data == new_data, "文件内容与写入数据不一致"

    def test_put_generic_valid_path_roundtrip(
        self,
        config_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """PUT 后 GET 回读，内容一致（白名单配置读写回环）。"""
        import channels.api.routes_config as rc

        test_yaml = tmp_path / "whitelist_roundtrip.yaml"
        monkeypatch.setitem(
            rc._GENERIC_CONFIG_WHITELIST,
            "test/roundtrip",
            test_yaml,
        )

        payload = {"level": 3, "name": "roundtrip-test"}
        put_resp = config_client.put(
            "/api/v1/config/generic/test/roundtrip",
            json={"data": payload},
        )
        assert put_resp.status_code == 200

        get_resp = config_client.get("/api/v1/config/generic/test/roundtrip")
        assert get_resp.status_code == 200
        assert get_resp.json() == payload, "GET 回读内容与 PUT 不一致"


# ===================================================================
# 5. Model CRUD（含 422 schema 契约回归）
# ===================================================================


class TestModelCRUD:
    """Model 增删改查完整流程测试。

    覆盖 POST /llm/models、PUT /llm/models/{id}、DELETE /llm/models/{id}，
    含前后端 payload schema 契约回归（422 防漂移）。
    """

    def test_add_model_success(self, config_client: TestClient) -> None:
        """POST /llm/models 用正确 payload {models: {id: conf}} 添加成功。"""
        resp = config_client.post(
            "/api/v1/config/llm/models",
            json={
                "models": {
                    "new-model": {
                        "provider": "test-provider",
                        "model_name": "new-model",
                        "display_name": "New Model",
                    }
                }
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "new-model" in data["models"]
        assert data["models"]["new-model"]["display_name"] == "New Model"
        # 原有模型不应被覆盖
        assert "test-model" in data["models"]

    def test_add_model_wrong_payload_returns_422(
        self, config_client: TestClient,
    ) -> None:
        """POST /llm/models 用旧 payload {id: conf}（缺少 models 包裹）必须 422。

        回归测试：前端曾直接把 model_id 作为顶层 key 发送，后端 ModelAddRequest
        要求顶层 models 字段，导致 422。此测试锁定前后端 schema 契约，防止回退。
        """
        resp = config_client.post(
            "/api/v1/config/llm/models",
            json={"wrong-model": {"provider": "p", "model_name": "m"}},
        )
        assert resp.status_code == 422
        # Pydantic 错误详情应指明 models 字段缺失
        detail = resp.json().get("detail", [])
        assert any(
            "models" in str(err.get("loc", [])) and err.get("type") == "missing"
            for err in detail
        ), f"422 详情应指向 models 字段缺失，实际: {detail}"

    def test_add_model_empty_dict_returns_200_noop(
        self, config_client: TestClient,
    ) -> None:
        """POST /llm/models 空 models 字典 → 200，无副作用。"""
        resp = config_client.post(
            "/api/v1/config/llm/models",
            json={"models": {}},
        )
        assert resp.status_code == 200
        # 不应新增模型
        assert "test-model" in resp.json()["models"]

    def test_update_model_success(self, config_client: TestClient) -> None:
        """PUT /llm/models/{id} 用正确 payload {config: {...}} 更新成功。"""
        resp = config_client.put(
            "/api/v1/config/llm/models/test-model",
            json={"config": {"display_name": "Updated Name", "context_window": 8192}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["models"]["test-model"]["display_name"] == "Updated Name"
        assert data["models"]["test-model"]["context_window"] == 8192
        # 原有字段应保留（部分更新，非整体替换）
        assert data["models"]["test-model"]["provider"] == "test-provider"

    def test_update_model_wrong_payload_returns_422(
        self, config_client: TestClient,
    ) -> None:
        """PUT /llm/models/{id} 用旧 payload（直接传 config 对象，缺少 config 包裹）必须 422。

        回归测试：前端曾直接传 config 对象而非 {config: {...}}，后端
        ModelConfigUpdateRequest 要求顶层 config 字段。锁定前后端 schema 契约。
        """
        resp = config_client.put(
            "/api/v1/config/llm/models/test-model",
            json={"display_name": "Should Fail"},
        )
        assert resp.status_code == 422
        detail = resp.json().get("detail", [])
        assert any(
            "config" in str(err.get("loc", [])) and err.get("type") == "missing"
            for err in detail
        ), f"422 详情应指向 config 字段缺失，实际: {detail}"

    def test_update_model_not_found_returns_404(
        self, config_client: TestClient,
    ) -> None:
        """PUT 不存在的 model_id 必须返回 404，不能隐式创建。"""
        resp = config_client.put(
            "/api/v1/config/llm/models/nonexistent-model",
            json={"config": {"display_name": "Ghost"}},
        )
        assert resp.status_code == 404
        assert "不存在" in resp.json().get("detail", "")

    def test_delete_model_success(self, config_client: TestClient) -> None:
        """DELETE /llm/models/{id} 删除已存在模型成功。"""
        resp = config_client.delete("/api/v1/config/llm/models/test-model")
        assert resp.status_code == 200
        assert "test-model" not in resp.json()["models"]

    def test_delete_model_not_found_returns_404(
        self, config_client: TestClient,
    ) -> None:
        """DELETE 不存在的 model_id 应返回 404。"""
        resp = config_client.delete(
            "/api/v1/config/llm/models/nonexistent-model",
        )
        assert resp.status_code == 404

    def test_model_crud_roundtrip(self, config_client: TestClient) -> None:
        """Model 完整生命周期：添加 → 更新 → 删除。"""
        # 1. 添加
        add_resp = config_client.post(
            "/api/v1/config/llm/models",
            json={
                "models": {
                    "lifecycle-model": {
                        "provider": "test-provider",
                        "model_name": "lifecycle",
                        "display_name": "Lifecycle",
                    }
                }
            },
        )
        assert add_resp.status_code == 200
        assert "lifecycle-model" in add_resp.json()["models"]

        # 2. 更新
        upd_resp = config_client.put(
            "/api/v1/config/llm/models/lifecycle-model",
            json={"config": {"display_name": "Lifecycle Updated"}},
        )
        assert upd_resp.status_code == 200
        assert (
            upd_resp.json()["models"]["lifecycle-model"]["display_name"]
            == "Lifecycle Updated"
        )

        # 3. 删除
        del_resp = config_client.delete(
            "/api/v1/config/llm/models/lifecycle-model",
        )
        assert del_resp.status_code == 200
        assert "lifecycle-model" not in del_resp.json()["models"]


# ===================================================================
# 6. 配置修改端点统一 schema 契约（CI/CD 门禁）
# ===================================================================


class TestConfigSchemaContract:
    """所有配置修改端点的 payload schema 契约回归测试。

    统一门禁：每个需 body 的 POST/PUT 配置端点，发送错误 payload 结构
    （模拟前后端 schema 漂移）必须返回 422，而非静默成功或 500。

    新增配置修改端点时，在此参数化追加一行即可套用同一 CI 门禁。
    """

    @pytest.mark.parametrize(
        ("method", "path", "wrong_payload", "expected_field"),
        [
            # Model 端点 — 缺少 models/config 包裹
            (
                "POST",
                "/api/v1/config/llm/models",
                {"bare-id": {"provider": "p"}},
                "models",
            ),
            (
                "PUT",
                "/api/v1/config/llm/models/test-model",
                {"provider": "p", "model_name": "m"},
                "config",
            ),
            # Provider 端点 — 缺少 config 包裹 / provider_id
            (
                "POST",
                "/api/v1/config/llm/providers",
                {"type": "openai", "api_base": "x"},
                "provider_id",
            ),
            (
                "PUT",
                "/api/v1/config/llm/providers/test-provider",
                {"api_base": "x"},
                "config",
            ),
            # 通用配置端点 — 缺少 data 包裹
            (
                "PUT",
                "/api/v1/config/api",
                {"endpoint": {"base_url": "x"}},
                "data",
            ),
            (
                "PUT",
                "/api/v1/config/concurrency",
                {"task": {"max_concurrent_tasks": 3}},
                "data",
            ),
            (
                "PUT",
                "/api/v1/config/cost-control",
                {"enabled": True},
                "data",
            ),
        ],
        ids=[
            "post-llm-models-missing-models",
            "put-llm-models-missing-config",
            "post-llm-providers-missing-provider_id",
            "put-llm-providers-missing-config",
            "put-api-missing-data",
            "put-concurrency-missing-data",
            "put-cost-control-missing-data",
        ],
    )
    def test_wrong_payload_returns_422(
        self,
        config_client: TestClient,
        method: str,
        path: str,
        wrong_payload: dict[str, Any],
        expected_field: str,
    ) -> None:
        """错误 payload 结构必须返回 422，锁定前后端 schema 契约。"""
        if method == "POST":
            resp = config_client.post(path, json=wrong_payload)
        else:
            resp = config_client.put(path, json=wrong_payload)
        assert resp.status_code == 422, (
            f"{method} {path} 错误 payload 应返回 422，"
            f"实际 {resp.status_code}: {resp.text[:200]}"
        )
        detail = resp.json().get("detail", [])
        assert any(
            expected_field in str(err.get("loc", []))
            for err in detail
        ), (
            f"422 详情应指向字段 '{expected_field}'，实际: {detail}"
        )
