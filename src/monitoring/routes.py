"""监控路由模块。

提供健康检查和 Prometheus 指标的 HTTP 端点：
- GET /health/live → liveness probe（存活探针）
- GET /health/ready → readiness probe（就绪探针）
- GET /metrics → Prometheus metrics（指标采集）

使用方式::

    from monitoring.routes import router
    app.include_router(router)
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from monitoring.health import liveness_probe, readiness_probe
from monitoring.metrics import get_metrics

router = APIRouter(tags=["监控"])


@router.get(
    "/health/live",
    summary="存活探针",
    description="进程级存活检查，始终返回 healthy",
)
def live() -> dict:
    """存活探针端点。

    Returns:
        {"status": "healthy", "probe": "liveness", "timestamp": ...}
    """
    return liveness_probe()


@router.get(
    "/health/ready",
    summary="就绪探针",
    description="服务级就绪检查，验证所有依赖组件正常",
)
def ready() -> dict:
    """就绪探针端点。

    Returns:
        {"status": "ready"/"not_ready", "probe": "readiness", ...}
    """
    return readiness_probe()


@router.get(
    "/metrics",
    summary="Prometheus 指标",
    description="Prometheus 格式的指标采集端点",
    response_class=PlainTextResponse,
)
def metrics() -> PlainTextResponse:
    """Prometheus 指标端点。

    Returns:
        Prometheus 文本格式的指标数据
    """
    return PlainTextResponse(
        content=get_metrics(),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
