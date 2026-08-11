"""
系统性能监控和瓶颈检测模块

监控系统的关键性能指标，检测性能瓶颈，提供告警机制
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

import psutil
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class PerformanceMetric(BaseModel):
    """性能指标"""

    name: str = Field(..., description="指标名称")
    value: float = Field(..., description="指标值")
    unit: str = Field(..., description="指标单位")
    timestamp: float = Field(default_factory=time.time, description="时间戳")
    tags: dict[str, str] = Field(default_factory=dict, description="标签")


class SystemMetrics(BaseModel):
    """系统指标"""

    cpu_usage: float = Field(..., description="CPU使用率")
    memory_usage: float = Field(..., description="内存使用率")
    disk_usage: float = Field(..., description="磁盘使用率")
    network_sent: float = Field(..., description="网络发送速率")
    network_recv: float = Field(..., description="网络接收速率")
    timestamp: float = Field(default_factory=time.time, description="时间戳")


class DatabaseMetrics(BaseModel):
    """数据库指标"""

    active_connections: int = Field(..., description="活跃连接数")
    connection_pool_size: int = Field(..., description="连接池大小")
    connection_wait_time: float = Field(..., description="连接等待时间")
    query_execution_time: float = Field(..., description="查询执行时间")
    timestamp: float = Field(default_factory=time.time, description="时间戳")


class LLMMetrics(BaseModel):
    """LLM指标"""

    active_requests: int = Field(..., description="活跃请求数")
    request_rate: float = Field(..., description="请求速率")
    average_response_time: float = Field(..., description="平均响应时间")
    error_rate: float = Field(..., description="错误率")
    timestamp: float = Field(default_factory=time.time, description="时间戳")


class ToolMetrics(BaseModel):
    """工具执行指标"""

    execution_count: int = Field(..., description="执行次数")
    average_execution_time: float = Field(..., description="平均执行时间")
    cache_hit_rate: float = Field(..., description="缓存命中率")
    error_count: int = Field(..., description="错误次数")
    timestamp: float = Field(default_factory=time.time, description="时间戳")


class TaskMetrics(BaseModel):
    """任务执行指标"""

    pending_tasks: int = Field(..., description="待处理任务数")
    running_tasks: int = Field(..., description="运行中任务数")
    completed_tasks: int = Field(..., description="已完成任务数")
    average_task_time: float = Field(..., description="平均任务执行时间")
    timestamp: float = Field(default_factory=time.time, description="时间戳")


class PerformanceAlert(BaseModel):
    """性能告警"""

    level: str = Field(..., description="告警级别")
    message: str = Field(..., description="告警消息")
    metrics: dict[str, Any] = Field(..., description="相关指标")
    timestamp: float = Field(default_factory=time.time, description="时间戳")


class PerformanceMonitor:
    """性能监控器"""

    def __init__(self, alert_callback: Callable | None = None):
        """
        初始化性能监控器

        Args:
            alert_callback: 告警回调函数
        """
        self._alert_callback = alert_callback
        self._metrics_history: dict[str, list[PerformanceMetric]] = {}
        self._max_history_size = 1000
        self._last_network_stats = psutil.net_io_counters()
        self._last_network_time = time.time()
        self._database_stats = {
            "active_connections": 0,
            "connection_pool_size": 0,
            "connection_wait_time": 0,
            "query_execution_time": 0,
        }
        self._llm_stats = {
            "active_requests": 0,
            "request_count": 0,
            "total_response_time": 0,
            "error_count": 0,
            "last_request_time": 0,
        }
        self._tool_stats = {
            "execution_count": 0,
            "total_execution_time": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "error_count": 0,
        }
        self._task_stats = {
            "pending_tasks": 0,
            "running_tasks": 0,
            "completed_tasks": 0,
            "total_task_time": 0,
        }
        # 后台任务引用，防止垃圾回收
        self._monitor_task: asyncio.Task | None = None
        self._shutdown_event = asyncio.Event()

    async def start(self):
        """启动性能监控"""
        logger.info("性能监控器已启动")
        self._shutdown_event.clear()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop(self):
        """停止性能监控"""
        logger.info("性能监控器已停止")
        self._shutdown_event.set()
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
        self._monitor_task = None

    async def _monitor_loop(self):
        """监控主循环"""
        while not self._shutdown_event.is_set():
            try:
                system_metrics = await self.get_system_metrics()
                self._record_metrics("system", system_metrics)

                db_metrics = await self.get_database_metrics()
                self._record_metrics("database", db_metrics)

                llm_metrics = await self.get_llm_metrics()
                self._record_metrics("llm", llm_metrics)

                tool_metrics = await self.get_tool_metrics()
                self._record_metrics("tool", tool_metrics)

                task_metrics = await self.get_task_metrics()
                self._record_metrics("task", task_metrics)

                await self.detect_bottlenecks()

            except asyncio.CancelledError:
                logger.debug("性能监控循环被取消")
                break
            except Exception as e:
                logger.error(f"性能监控出错: {e}")

            try:
                await asyncio.wait_for(self._shutdown_event.wait(), timeout=5.0)
            except TimeoutError:
                continue

    async def get_system_metrics(self) -> SystemMetrics:
        """获取系统指标"""
        cpu_usage = psutil.cpu_percent(interval=0.1)
        memory_usage = psutil.virtual_memory().percent
        disk_usage = psutil.disk_usage("/").percent

        current_network_stats = psutil.net_io_counters()
        current_time = time.time()
        time_diff = current_time - self._last_network_time

        if time_diff > 0:
            network_sent = (current_network_stats.bytes_sent - self._last_network_stats.bytes_sent) / time_diff / 1024
            network_recv = (current_network_stats.bytes_recv - self._last_network_stats.bytes_recv) / time_diff / 1024
        else:
            network_sent = network_recv = 0

        self._last_network_stats = current_network_stats
        self._last_network_time = current_time

        return SystemMetrics(
            cpu_usage=cpu_usage,
            memory_usage=memory_usage,
            disk_usage=disk_usage,
            network_sent=network_sent,
            network_recv=network_recv,
        )

    async def get_database_metrics(self) -> DatabaseMetrics:
        """获取数据库指标"""
        return DatabaseMetrics(
            active_connections=self._database_stats["active_connections"],
            connection_pool_size=self._database_stats["connection_pool_size"],
            connection_wait_time=self._database_stats["connection_wait_time"],
            query_execution_time=self._database_stats["query_execution_time"],
        )

    async def get_llm_metrics(self) -> LLMMetrics:
        """获取LLM指标"""
        current_time = time.time()
        time_diff = current_time - self._llm_stats["last_request_time"]
        total_requests = self._llm_stats["request_count"]

        return LLMMetrics(
            active_requests=self._llm_stats["active_requests"],
            request_rate=self._llm_stats["request_count"] / time_diff if time_diff > 0 else 0,
            average_response_time=self._llm_stats["total_response_time"] / total_requests if total_requests > 0 else 0,
            error_rate=self._llm_stats["error_count"] / total_requests if total_requests > 0 else 0,
        )

    async def get_tool_metrics(self) -> ToolMetrics:
        """获取工具执行指标"""
        total_executions = self._tool_stats["execution_count"]
        total_cache = self._tool_stats["cache_hits"] + self._tool_stats["cache_misses"]

        return ToolMetrics(
            execution_count=total_executions,
            average_execution_time=self._tool_stats["total_execution_time"] / total_executions
            if total_executions > 0
            else 0,
            cache_hit_rate=self._tool_stats["cache_hits"] / total_cache if total_cache > 0 else 0,
            error_count=self._tool_stats["error_count"],
        )

    async def get_task_metrics(self) -> TaskMetrics:
        """获取任务执行指标"""
        total_completed = self._task_stats["completed_tasks"]

        return TaskMetrics(
            pending_tasks=self._task_stats["pending_tasks"],
            running_tasks=self._task_stats["running_tasks"],
            completed_tasks=total_completed,
            average_task_time=self._task_stats["total_task_time"] / total_completed if total_completed > 0 else 0,
        )

    def _record_metrics(self, metric_type: str, metrics: Any):
        """记录指标"""
        if metric_type not in self._metrics_history:
            self._metrics_history[metric_type] = []

        # 记录指标
        self._metrics_history[metric_type].append({"timestamp": time.time(), "metrics": metrics.model_dump()})

        # 限制历史记录长度
        if len(self._metrics_history[metric_type]) > self._max_history_size:
            self._metrics_history[metric_type] = self._metrics_history[metric_type][-self._max_history_size :]

    async def detect_bottlenecks(self):
        """检测性能瓶颈"""
        system_metrics = await self.get_system_metrics()
        db_metrics = await self.get_database_metrics()
        llm_metrics = await self.get_llm_metrics()
        tool_metrics = await self.get_tool_metrics()
        task_metrics = await self.get_task_metrics()

        # 系统资源瓶颈检测
        if system_metrics.cpu_usage > 80:
            await self._trigger_alert("high", f"CPU使用率过高: {system_metrics.cpu_usage}%")
        if system_metrics.memory_usage > 80:
            await self._trigger_alert("high", f"内存使用率过高: {system_metrics.memory_usage}%")
        if system_metrics.disk_usage > 80:
            await self._trigger_alert("medium", f"磁盘使用率过高: {system_metrics.disk_usage}%")

        # 数据库瓶颈检测
        if db_metrics.active_connections > db_metrics.connection_pool_size * 0.8:
            await self._trigger_alert(
                "medium", f"数据库连接池使用率过高: {db_metrics.active_connections}/{db_metrics.connection_pool_size}"
            )
        if db_metrics.query_execution_time > 1.0:
            await self._trigger_alert("medium", f"数据库查询执行时间过长: {db_metrics.query_execution_time}秒")

        # LLM瓶颈检测
        if llm_metrics.active_requests > 10:
            await self._trigger_alert("medium", f"LLM活跃请求数过多: {llm_metrics.active_requests}")
        if llm_metrics.average_response_time > 5.0:
            await self._trigger_alert("high", f"LLM响应时间过长: {llm_metrics.average_response_time}秒")
        if llm_metrics.error_rate > 0.1:
            await self._trigger_alert("high", f"LLM错误率过高: {llm_metrics.error_rate * 100}%")

        # 工具执行瓶颈检测
        if tool_metrics.average_execution_time > 3.0:
            await self._trigger_alert("medium", f"工具平均执行时间过长: {tool_metrics.average_execution_time}秒")
        if tool_metrics.cache_hit_rate < 0.5:
            await self._trigger_alert("low", f"工具缓存命中率过低: {tool_metrics.cache_hit_rate * 100}%")

        # 任务执行瓶颈检测
        if task_metrics.pending_tasks > 50:
            await self._trigger_alert("high", f"待处理任务数过多: {task_metrics.pending_tasks}")
        if task_metrics.average_task_time > 30.0:
            await self._trigger_alert("medium", f"平均任务执行时间过长: {task_metrics.average_task_time}秒")

    async def _trigger_alert(self, level: str, message: str):
        """触发告警"""
        alert = PerformanceAlert(
            level=level,
            message=message,
            metrics={
                "system": (await self.get_system_metrics()).model_dump(),
                "llm": (await self.get_llm_metrics()).model_dump(),
                "task": (await self.get_task_metrics()).model_dump(),
            },
        )

        logger.warning(f"性能告警 [{level.upper()}]: {message}")

        if self._alert_callback:
            try:
                await self._alert_callback(alert)
            except Exception as e:
                logger.error(f"告警回调执行失败: {e}")

    def record_database_connection(self, connection_time: float):
        """记录数据库连接"""
        self._database_stats["connection_wait_time"] = connection_time

    def record_query_execution(self, execution_time: float):
        """记录查询执行"""
        self._database_stats["query_execution_time"] = execution_time

    def update_database_connections(self, active: int, pool_size: int):
        """更新数据库连接信息"""
        self._database_stats["active_connections"] = active
        self._database_stats["connection_pool_size"] = pool_size

    def record_llm_request(self, response_time: float, error: bool = False):
        """记录LLM请求"""
        self._llm_stats["active_requests"] += 1
        self._llm_stats["request_count"] += 1
        self._llm_stats["total_response_time"] += response_time
        self._llm_stats["last_request_time"] = time.time()
        if error:
            self._llm_stats["error_count"] += 1

    def record_llm_response(self):
        """记录LLM响应"""
        if self._llm_stats["active_requests"] > 0:
            self._llm_stats["active_requests"] -= 1

    def record_tool_execution(self, execution_time: float, cache_hit: bool = False, error: bool = False):
        """记录工具执行"""
        self._tool_stats["execution_count"] += 1
        self._tool_stats["total_execution_time"] += execution_time
        if cache_hit:
            self._tool_stats["cache_hits"] += 1
        else:
            self._tool_stats["cache_misses"] += 1
        if error:
            self._tool_stats["error_count"] += 1

    def update_task_status(self, pending: int, running: int, completed: int, task_time: float = 0):
        """更新任务状态"""
        self._task_stats["pending_tasks"] = pending
        self._task_stats["running_tasks"] = running
        self._task_stats["completed_tasks"] = completed
        if task_time > 0:
            self._task_stats["total_task_time"] += task_time

    def get_metrics_history(self, metric_type: str, limit: int = 100) -> list[dict[str, Any]]:
        """获取指标历史"""
        if metric_type not in self._metrics_history:
            return []
        return self._metrics_history[metric_type][-limit:]

    async def get_current_metrics(self) -> dict[str, Any]:
        """获取当前指标"""
        return {
            "system": (await self.get_system_metrics()).model_dump(),
            "database": (await self.get_database_metrics()).model_dump(),
            "llm": (await self.get_llm_metrics()).model_dump(),
            "tool": (await self.get_tool_metrics()).model_dump(),
            "task": (await self.get_task_metrics()).model_dump(),
        }

    def get_current_stats(self) -> dict[str, Any]:
        """获取当前统计信息（同步版本）"""
        import asyncio  # noqa: PLC0415

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果在异步环境中，创建新任务获取结果
                asyncio.ensure_future(self._get_stats_async())
                # 使用 run_coroutine_threadsafe 如果在线程中
                return {}
            return loop.run_until_complete(self._get_stats_async())
        except RuntimeError:
            return {}

    async def _get_stats_async(self) -> dict[str, Any]:
        """异步获取统计信息"""
        stats = {}
        try:
            system_metrics = await self.get_system_metrics()
            stats["cpu"] = {"usage": system_metrics.cpu_usage}
            stats["memory"] = {"usage": system_metrics.memory_usage}
        except Exception:
            pass

        # 响应时间统计
        if hasattr(self, "_response_times") and self._response_times:
            response_times = self._response_times
            stats["response_time"] = {
                "avg": sum(response_times) / len(response_times),
                "min": min(response_times),
                "max": max(response_times),
                "count": len(response_times),
            }

        return stats

    def get_health_status(self) -> dict[str, Any]:
        """获取系统健康状态"""
        issues = []
        metrics = {}

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                system_metrics = {
                    "cpu_usage": psutil.cpu_percent(interval=0.1),
                    "memory_usage": psutil.virtual_memory().percent,
                }
            else:
                system_metrics = loop.run_until_complete(self._get_health_metrics())
        except RuntimeError:
            system_metrics = {
                "cpu_usage": psutil.cpu_percent(interval=0.1),
                "memory_usage": psutil.virtual_memory().percent,
            }

        cpu_usage = metrics["cpu"] = system_metrics.get("cpu_usage", 0)
        memory_usage = metrics["memory"] = system_metrics.get("memory_usage", 0)

        # 确定健康状态
        status = "healthy"
        if cpu_usage > 80 or memory_usage > 80:
            status = "critical"
            if cpu_usage > 80:
                issues.append(f"CPU使用率过高: {cpu_usage:.1f}%")
            if memory_usage > 80:
                issues.append(f"内存使用率过高: {memory_usage:.1f}%")
        elif cpu_usage > 60 or memory_usage > 60:
            status = "warning"
            if cpu_usage > 60:
                issues.append(f"CPU使用率偏高: {cpu_usage:.1f}%")
            if memory_usage > 60:
                issues.append(f"内存使用率偏高: {memory_usage:.1f}%")

        return {"status": status, "issues": issues, "metrics": metrics}

    async def _get_health_metrics(self) -> dict[str, float]:
        """获取健康检查所需的指标"""
        system_metrics = await self.get_system_metrics()
        return {
            "cpu_usage": system_metrics.cpu_usage,
            "memory_usage": system_metrics.memory_usage,
        }

    async def start_monitoring(self, interval: int = 5):
        """启动性能监控"""
        self._monitoring_interval = interval
        self._monitoring_active = True
        self._response_times = []
        await self.start()

    async def stop_monitoring(self):
        """停止性能监控"""
        self._monitoring_active = False
        await self.stop()

    def measure_response_time(self):
        """
        响应时间测量上下文管理器

        Usage:
            async with monitor.measure_response_time():
                await some_operation()
        """
        return ResponseTimeContext(self)


class ResponseTimeContext:
    """响应时间测量上下文管理器"""

    def __init__(self, monitor: PerformanceMonitor):
        self.monitor = monitor
        self.start_time = 0

    async def __aenter__(self):
        """进入上下文，开始计时"""
        self.start_time = time.time() * 1000
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """退出上下文，记录响应时间"""
        end_time = time.time() * 1000
        response_time = end_time - self.start_time

        if not hasattr(self.monitor, "_response_times"):
            self.monitor._response_times = []

        self.monitor._response_times.append(response_time)
        if len(self.monitor._response_times) > 1000:
            self.monitor._response_times = self.monitor._response_times[-1000:]


# 全局性能监控器实例
_performance_monitor: PerformanceMonitor | None = None


def get_performance_monitor() -> PerformanceMonitor:
    """
    获取全局性能监控器实例

    Returns:
        PerformanceMonitor: 性能监控器实例
    """
    global _performance_monitor  # noqa: PLW0603
    if _performance_monitor is None:
        _performance_monitor = PerformanceMonitor()
    return _performance_monitor


async def start_performance_monitor() -> None:
    """
    启动性能监控器
    """
    monitor = get_performance_monitor()
    await monitor.start()


async def stop_performance_monitor() -> None:
    """
    停止性能监控器
    """
    monitor = get_performance_monitor()
    await monitor.stop()
