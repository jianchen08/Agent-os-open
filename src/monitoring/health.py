"""健康检查模块。

提供存活探针、就绪探针和组件级健康检查能力。

组件检查：
- Redis: 缓存/会话存储连接
- Channels: 各 IM 通道适配器状态
- Pipeline: 管道引擎运行状态

探针说明：
- liveness_probe: 进程存活检查，始终返回 healthy
- readiness_probe: 服务就绪检查，依赖所有组件正常
"""

from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class HealthChecker:
    """组件级健康检查器。

    检查 Redis、通道适配器和管道引擎的运行状态，
    返回统一格式的健康状态 JSON。

    Example::

        checker = HealthChecker()
        result = checker.full_check()
        # {"status": "healthy", "components": {...}}
    """

    def __init__(self) -> None:
        """初始化健康检查器。"""
        self._check_time: float = 0.0

    def check_redis(self) -> dict[str, Any]:
        """检查 Redis 连接状态。

        通过 ServiceProvider 获取 Redis 实例并 ping，
        无法连接时降级返回 unknown（不阻塞启动）。

        Returns:
            包含 status 和可选 detail 的字典
        """
        try:
            from infrastructure.service_provider import get_service_provider

            provider = get_service_provider()
            redis = provider.get("redis")
            if redis is None:
                return {"status": "unknown", "detail": "Redis not configured"}
            # 尝试 ping
            if hasattr(redis, "ping"):
                redis.ping()
            return {"status": "healthy"}
        except Exception as exc:
            logger.debug("Redis check failed: %s", exc)
            return {"status": "unhealthy", "detail": str(exc)}

    def check_channels(self) -> dict[str, Any]:
        """检查各通道适配器连接状态。

        通过 ServiceProvider 获取 ChannelGateway，
        遍历已注册适配器检查运行状态。

        Returns:
            包含 status 和各通道详情的字典
        """
        try:
            from infrastructure.service_provider import get_service_provider

            provider = get_service_provider()
            gateway = provider.get("gateway")
            if gateway is None:
                return {"status": "unknown", "detail": "Gateway not initialized"}
            # 检查适配器
            adapters: dict[str, Any] = getattr(gateway, "_adapters", {})
            if not adapters:
                return {"status": "unknown", "detail": "No adapters registered"}
            channels: dict[str, str] = {}
            all_ok = True
            for name, adapter in adapters.items():
                running = getattr(adapter, "is_running", lambda: True)()
                channels[name] = "healthy" if running else "unhealthy"
                if not running:
                    all_ok = False
            return {
                "status": "healthy" if all_ok else "degraded",
                "channels": channels,
            }
        except Exception as exc:
            logger.debug("Channels check failed: %s", exc)
            return {"status": "unknown", "detail": str(exc)}

    def check_pipeline(self) -> dict[str, Any]:
        """检查管道引擎运行状态。

        通过 ServiceProvider 获取管道引擎实例。

        Returns:
            包含 status 的字典
        """
        try:
            from infrastructure.service_provider import get_service_provider

            provider = get_service_provider()
            engine = provider.get("engine")
            if engine is None:
                return {"status": "unknown", "detail": "Pipeline engine not initialized"}
            return {"status": "healthy"}
        except Exception as exc:
            logger.debug("Pipeline check failed: %s", exc)
            return {"status": "unhealthy", "detail": str(exc)}

    def full_check(self) -> dict[str, Any]:
        """执行完整健康检查。

        检查所有组件并汇总为统一 JSON 格式。

        Returns:
            包含总体 status 和各组件 details 的字典
        """
        self._check_time = time.time()

        components = {
            "redis": self.check_redis(),
            "channels": self.check_channels(),
            "pipeline": self.check_pipeline(),
        }

        # 判定总体状态：全部 healthy/unknown → healthy，否则 degraded
        statuses = [c["status"] for c in components.values()]
        has_unhealthy = any(s == "unhealthy" for s in statuses)
        overall = "healthy" if not has_unhealthy else "unhealthy"

        return {
            "status": overall,
            "timestamp": self._check_time,
            "components": components,
        }


def liveness_probe() -> dict[str, Any]:
    """存活探针。

    进程级别的存活检查，始终返回 healthy。
    用于 Kubernetes liveness probe 或负载均衡健康检查。

    Returns:
        {"status": "healthy", "probe": "liveness", "timestamp": ...}
    """
    return {
        "status": "healthy",
        "probe": "liveness",
        "timestamp": time.time(),
    }


def readiness_probe() -> dict[str, Any]:
    """就绪探针。

    服务级别的就绪检查，验证所有依赖组件正常。
    用于 Kubernetes readiness probe。

    Returns:
        {"status": "ready"/"not_ready", "probe": "readiness",
         "timestamp": ..., "components": {...}}
    """
    checker = HealthChecker()
    result = checker.full_check()

    is_ready = result["status"] != "unhealthy"
    return {
        "status": "ready" if is_ready else "not_ready",
        "probe": "readiness",
        "timestamp": result["timestamp"],
        "components": result["components"],
    }
