"""监控告警模块。

提供健康检查、Prometheus 指标、结构化日志和监控路由能力：
- HealthChecker: 组件级健康检查
- Prometheus 指标: 消息计数、处理耗时、活跃会话、通道状态
- 结构化日志: JSON/彩色控制台输出，trace_id/request_id 上下文
- FastAPI 路由: /health/live, /health/ready, /metrics
"""

from monitoring.health import HealthChecker, liveness_probe, readiness_probe
from monitoring.logging_config import (
    ContextFilter,
    get_request_id,
    get_trace_id,
    set_request_id,
    set_trace_id,
    setup_logging,
)
from monitoring.metrics import (
    ACTIVE_SESSIONS,
    CHANNEL_STATUS,
    MESSAGE_PROCESSED,
    MESSAGE_RECEIVED,
    PROCESSING_TIME,
    get_metrics,
)
from monitoring.routes import router

__all__ = [
    # 健康
    "HealthChecker",
    "liveness_probe",
    "readiness_probe",
    # 指标
    "ACTIVE_SESSIONS",
    "CHANNEL_STATUS",
    "MESSAGE_PROCESSED",
    "MESSAGE_RECEIVED",
    "PROCESSING_TIME",
    "get_metrics",
    # 日志
    "ContextFilter",
    "get_request_id",
    "get_trace_id",
    "set_request_id",
    "set_trace_id",
    "setup_logging",
    # 路由
    "router",
]
