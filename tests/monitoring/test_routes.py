"""监控路由模块测试。

覆盖场景：
- GET /health/live 返回 200 + liveness JSON
- GET /health/ready 返回 200 + readiness JSON
- GET /metrics 返回 200 + Prometheus 文本
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from monitoring.routes import router


def _create_test_app() -> TestClient:
    """创建挂载监控路由的测试客户端。"""
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestLivenessRoute:
    """GET /health/live 测试。"""

    def test_returns_200(self) -> None:
        """存活探针返回 200。"""
        client = _create_test_app()
        resp = client.get("/health/live")
        assert resp.status_code == 200

    def test_returns_json(self) -> None:
        """存活探针返回 JSON。"""
        client = _create_test_app()
        resp = client.get("/health/live")
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["probe"] == "liveness"

    def test_has_timestamp(self) -> None:
        """存活探针包含时间戳。"""
        client = _create_test_app()
        resp = client.get("/health/live")
        assert "timestamp" in resp.json()


class TestReadinessRoute:
    """GET /health/ready 测试。"""

    def test_returns_200(self) -> None:
        """就绪探针返回 200。"""
        client = _create_test_app()
        resp = client.get("/health/ready")
        assert resp.status_code == 200

    def test_returns_json(self) -> None:
        """就绪探针返回 JSON。"""
        client = _create_test_app()
        resp = client.get("/health/ready")
        data = resp.json()
        assert data["probe"] == "readiness"
        assert data["status"] in ("ready", "not_ready")

    def test_has_components(self) -> None:
        """就绪探针返回组件详情。"""
        client = _create_test_app()
        resp = client.get("/health/ready")
        assert "components" in resp.json()


class TestMetricsRoute:
    """GET /metrics 测试。"""

    def test_returns_200(self) -> None:
        """指标端点返回 200。"""
        client = _create_test_app()
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_returns_text(self) -> None:
        """指标端点返回 Prometheus 文本格式。"""
        client = _create_test_app()
        resp = client.get("/metrics")
        assert "text/plain" in resp.headers.get("content-type", "")
        assert "# HELP" in resp.text

    def test_contains_metric_names(self) -> None:
        """指标端点包含核心指标名。"""
        client = _create_test_app()
        resp = client.get("/metrics")
        assert "message_received" in resp.text
