"""
REST API 热加载端点测试。

覆盖功能点：
- PUT /api/v1/config/{path} 写文件后触发 ConfigCenter 重读
- POST /api/v1/config/reload/{path} 手动重载
- 白名单路径校验
- 安全检查（路径必须在 config/ 内）
- ConfigCenter 未初始化时的 503 响应
"""
import pytest
import yaml
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)


@pytest.fixture
def config_dir(tmp_path):
    """临时 config 目录。"""
    return tmp_path / "config"


@pytest.fixture
def app_with_config(config_dir):
    """创建带路由的 FastAPI 测试应用。"""
    from fastapi import FastAPI

    app = FastAPI()

    # 动态导入并 patch 路径常量
    import src.channels.api.routes_config as routes_mod

    # 设置路径常量
    config_root = config_dir
    config_root.mkdir(parents=True, exist_ok=True)

    original_project_root = routes_mod._PROJECT_ROOT
    original_config_root = routes_mod._CONFIG_ROOT
    original_config_models = routes_mod._CONFIG_MODELS_DIR
    original_config_system = routes_mod._CONFIG_SYSTEM_DIR
    original_llm_yaml = routes_mod._LLM_YAML
    original_context_yaml = routes_mod._CONTEXT_WINDOW_YAML
    original_api_yaml = routes_mod._API_YAML
    original_concurrency_yaml = routes_mod._CONCURRENCY_YAML
    original_cost_yaml = routes_mod._COST_CONTROL_YAML
    original_whitelist = routes_mod._GENERIC_CONFIG_WHITELIST
    original_config_center = routes_mod._config_center

    models_dir = config_root / "models"
    system_dir = config_root / "system"
    models_dir.mkdir(parents=True, exist_ok=True)
    system_dir.mkdir(parents=True, exist_ok=True)

    routes_mod._PROJECT_ROOT = config_dir.parent
    routes_mod._CONFIG_ROOT = config_root
    routes_mod._CONFIG_MODELS_DIR = models_dir
    routes_mod._CONFIG_SYSTEM_DIR = system_dir
    routes_mod._LLM_YAML = models_dir / "llm.yaml"
    routes_mod._CONTEXT_WINDOW_YAML = system_dir / "context_window_config.yaml"
    routes_mod._API_YAML = system_dir / "api_config.yaml"
    routes_mod._CONCURRENCY_YAML = system_dir / "concurrency_config.yaml"
    routes_mod._COST_CONTROL_YAML = system_dir / "cost_control.yaml"
    routes_mod._config_center = None

    # 写入默认 llm.yaml
    _write_yaml(routes_mod._LLM_YAML, {
        "models": {},
        "providers": {},
        "defaults": {"chat": "", "embedding": ""},
    })

    routes_mod._GENERIC_CONFIG_WHITELIST = {
        "system/test": system_dir / "test.yaml",
        "isolation/test": config_root / "isolation" / "test.yaml",
    }

    # Mock invalidate_all_llm_caches 以避免 src.db 导入失败
    with patch.object(routes_mod, "invalidate_all_llm_caches", MagicMock()):
        app.include_router(routes_mod.router)
        yield app, routes_mod

    # 恢复
    routes_mod._PROJECT_ROOT = original_project_root
    routes_mod._CONFIG_ROOT = original_config_root
    routes_mod._CONFIG_MODELS_DIR = original_config_models
    routes_mod._CONFIG_SYSTEM_DIR = original_config_system
    routes_mod._LLM_YAML = original_llm_yaml
    routes_mod._CONTEXT_WINDOW_YAML = original_context_yaml
    routes_mod._API_YAML = original_api_yaml
    routes_mod._CONCURRENCY_YAML = original_concurrency_yaml
    routes_mod._COST_CONTROL_YAML = original_cost_yaml
    routes_mod._GENERIC_CONFIG_WHITELIST = original_whitelist
    routes_mod._config_center = original_config_center


@pytest.fixture
def client(app_with_config):
    """FastAPI 测试客户端。"""
    app, routes_mod = app_with_config
    return TestClient(app)


# ---------------------------------------------------------------------------
# POST /reload/{path} — 手动重载
# ---------------------------------------------------------------------------


class TestReloadEndpoint:
    """POST /api/v1/config/reload/{path} 手动重载配置。"""

    def test_reload_without_config_center_returns_503(self, client, app_with_config):
        """ConfigCenter 未初始化时应返回 503。"""
        _, routes_mod = app_with_config
        # 确保 _config_center 为 None
        original = routes_mod._config_center
        routes_mod._config_center = None
        try:
            resp = client.post("/api/v1/config/reload/system/test")
            assert resp.status_code == 503
            assert "未初始化" in resp.json()["detail"]
        finally:
            routes_mod._config_center = original

    def test_reload_with_config_center(self, client, app_with_config):
        """ConfigCenter 已初始化时应调用 reload。"""
        _, routes_mod = app_with_config
        mock_center = MagicMock()
        mock_center.reload.return_value = {
            "success": True, "error": None, "rolled_back": False, "config_type": "unknown"
        }
        routes_mod._config_center = mock_center

        # 写入白名单内的文件
        test_file = routes_mod._GENERIC_CONFIG_WHITELIST["system/test"]
        _write_yaml(test_file, {"key": "value"})

        try:
            resp = client.post("/api/v1/config/reload/system/test")
            assert resp.status_code == 200
            data = resp.json()
            assert data["success"] is True
            assert mock_center.reload.called
        finally:
            routes_mod._config_center = None

    def test_reload_nonexistent_file_returns_404(self, client, app_with_config):
        """重载不存在的文件应返回 404。"""
        _, routes_mod = app_with_config
        mock_center = MagicMock()
        mock_center.reload.side_effect = FileNotFoundError("文件不存在")
        routes_mod._config_center = mock_center

        try:
            resp = client.post("/api/v1/config/reload/system/nonexistent")
            assert resp.status_code == 404
        finally:
            routes_mod._config_center = None

    def test_reload_invalid_yaml_returns_400(self, client, app_with_config):
        """YAML 解析失败应返回 400。"""
        _, routes_mod = app_with_config
        mock_center = MagicMock()
        mock_center.reload.side_effect = ValueError("YAML 解析失败")
        routes_mod._config_center = mock_center

        # 写入白名单内的文件使其存在（这样才会走到 reload 调用）
        test_file = routes_mod._GENERIC_CONFIG_WHITELIST["system/test"]
        _write_yaml(test_file, {"key": "val"})

        try:
            resp = client.post("/api/v1/config/reload/system/test")
            assert resp.status_code == 400
        finally:
            routes_mod._config_center = None

    def test_reload_path_traversal_blocked(self, client, app_with_config):
        """路径遍历攻击应被阻止（403）。"""
        _, routes_mod = app_with_config
        mock_center = MagicMock()
        mock_center.reload.return_value = {"success": True}
        routes_mod._config_center = mock_center

        try:
            resp = client.post("/api/v1/config/reload/../../etc/passwd")
            # 应返回 403 或 404
            assert resp.status_code in (403, 404, 422)
        finally:
            routes_mod._config_center = None


# ---------------------------------------------------------------------------
# PUT /generic/{path} — 写入配置触发重读
# ---------------------------------------------------------------------------


class TestGenericConfigEndpoints:
    """通用配置端点（白名单模式）。"""

    def test_get_generic_config(self, client, app_with_config):
        """GET 白名单内配置应返回数据。"""
        _, routes_mod = app_with_config
        test_file = routes_mod._GENERIC_CONFIG_WHITELIST["system/test"]
        _write_yaml(test_file, {"key": "value"})

        resp = client.get("/api/v1/config/generic/system/test")
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "value"

    def test_get_generic_config_unknown_path_404(self, client, app_with_config):
        """GET 非白名单路径应返回 404。"""
        resp = client.get("/api/v1/config/generic/unknown/path")
        assert resp.status_code == 404

    def test_put_generic_config(self, client, app_with_config):
        """PUT 白名单内配置应写入文件。"""
        _, routes_mod = app_with_config
        test_file = routes_mod._GENERIC_CONFIG_WHITELIST["system/test"]

        new_data = {"key": "updated", "version": 2}
        resp = client.put("/api/v1/config/generic/system/test", json=new_data)

        assert resp.status_code == 200
        assert resp.json()["key"] == "updated"

        # 验证文件确实被写入
        with open(test_file, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["key"] == "updated"

    def test_put_generic_config_unknown_path_404(self, client, app_with_config):
        """PUT 非白名单路径应返回 404。"""
        resp = client.put("/api/v1/config/generic/unknown/path", json={"key": "val"})
        assert resp.status_code == 404

    def test_put_triggers_config_center_reload(self, client, app_with_config):
        """PUT 写入后应触发 ConfigCenter reload。"""
        _, routes_mod = app_with_config
        mock_center = MagicMock()
        mock_center.reload.return_value = {"success": True}
        routes_mod._config_center = mock_center

        try:
            resp = client.put(
                "/api/v1/config/generic/system/test",
                json={"key": "triggered"},
            )
            assert resp.status_code == 200
            # _write_yaml 内部会调用 _config_center.reload
            assert mock_center.reload.called
        finally:
            routes_mod._config_center = None


# ---------------------------------------------------------------------------
# LLM 配置端点
# ---------------------------------------------------------------------------


class TestLlmConfigEndpoints:
    """LLM 配置 API 端点。"""

    def test_get_llm_config(self, client, app_with_config):
        """GET LLM 配置应返回脱敏后的数据。"""
        resp = client.get("/api/v1/config/llm")
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        assert "providers" in data
        assert "defaults" in data

    def test_get_llm_providers(self, client, app_with_config):
        """GET 提供商列表应返回基本信息。"""
        _, routes_mod = app_with_config
        _write_yaml(routes_mod._LLM_YAML, {
            "providers": {"test_provider": {"api_key": "sk-secret-key-12345678", "api_base": "http://test"}},
            "models": {},
            "defaults": {},
        })

        resp = client.get("/api/v1/config/llm/providers")
        assert resp.status_code == 200
        data = resp.json()
        provider = data["providers"]["test_provider"]
        # providers 端点返回 api_base 和 has_key，不返回原始 api_key
        assert provider["api_base"] == "http://test"
        assert provider["has_key"] is True

    def test_put_llm_defaults(self, client, app_with_config):
        """PUT 默认模型配置应更新并持久化。"""
        resp = client.put("/api/v1/config/llm/defaults", json={"chat": "gpt-4"})
        assert resp.status_code == 200
        assert resp.json()["chat"] == "gpt-4"


# ---------------------------------------------------------------------------
# 并发配置端点
# ---------------------------------------------------------------------------


class TestConcurrencyEndpoints:
    """并发配置 API。"""

    def test_put_concurrency_config(self, client, app_with_config):
        """PUT 并发配置应持久化。"""
        new_config = {
            "task": {"max_concurrent_tasks": 5},
            "agent": {"l1_max_concurrent": 1},
        }
        resp = client.put("/api/v1/config/concurrency", json=new_config)
        assert resp.status_code == 200

    def test_get_concurrency_config(self, client, app_with_config):
        """GET 并发配置应返回数据。"""
        resp = client.get("/api/v1/config/concurrency")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 上下文窗口配置端点
# ---------------------------------------------------------------------------


class TestContextWindowEndpoints:
    """上下文窗口配置 API。"""

    def test_get_context_window(self, client, app_with_config):
        """GET 上下文窗口配置。"""
        _, routes_mod = app_with_config
        _write_yaml(routes_mod._CONTEXT_WINDOW_YAML, {
            "max_context_length": 200000,
            "budgets": {"system_prompt": 0.06},
        })

        resp = client.get("/api/v1/config/context-window")
        assert resp.status_code == 200
        data = resp.json()
        assert data["max_context_length"] == 200000

    def test_put_context_window(self, client, app_with_config):
        """PUT 上下文窗口配置。"""
        _, routes_mod = app_with_config
        _write_yaml(routes_mod._CONTEXT_WINDOW_YAML, {"budgets": {}})

        resp = client.put(
            "/api/v1/config/context-window",
            json={"max_context_length": 100000},
        )
        assert resp.status_code == 200
        assert resp.json()["max_context_length"] == 100000


# ---------------------------------------------------------------------------
# _write_yaml 集成测试
# ---------------------------------------------------------------------------


class TestWriteYamlIntegration:
    """_write_yaml 写入后通知 ConfigCenter。"""

    def test_write_yaml_notifies_config_center(self, app_with_config):
        """写入 YAML 后应调用 ConfigCenter.reload。"""
        _, routes_mod = app_with_config
        mock_center = MagicMock()
        mock_center.reload.return_value = {"success": True}
        routes_mod._config_center = mock_center

        try:
            test_file = routes_mod._CONFIG_SYSTEM_DIR / "test_notify.yaml"
            routes_mod._write_yaml(test_file, {"key": "value"})
            assert mock_center.reload.called
            call_path = mock_center.reload.call_args[0][0]
            assert str(test_file) in call_path
        finally:
            routes_mod._config_center = None

    def test_write_yaml_handles_reload_failure(self, app_with_config):
        """ConfigCenter reload 失败不应阻塞写入。"""
        _, routes_mod = app_with_config
        mock_center = MagicMock()
        mock_center.reload.side_effect = Exception("reload failed")
        routes_mod._config_center = mock_center

        try:
            test_file = routes_mod._CONFIG_SYSTEM_DIR / "test_failure.yaml"
            # 不应抛异常
            routes_mod._write_yaml(test_file, {"key": "value"})
            # 文件应仍被写入
            assert test_file.exists()
        finally:
            routes_mod._config_center = None
