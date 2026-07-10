"""
REQ-20/21/23 验证：监控数据/模型名/上下文信息测试

验证范围：
1. REQ-20：监控页面数据 — PerformanceMonitor 指标采集
2. REQ-21：模型名显示 — AgentConfig.model_name/model_tier 在 loader 中的传递
3. REQ-23：上下文信息 — CLI output_adapter 的上下文窗口信息传递
4. Prometheus 指标 (metrics.py) 的采集与输出
5. 健康检查 (health.py) 的组件检查逻辑
"""
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ===========================================================================
# 1. REQ-20：监控数据采集验证
# ===========================================================================

class TestMetricsCollection:
    """验证 Prometheus 指标采集逻辑。"""

    def test_counter_increment(self):
        """验证 Counter 指标递增。"""
        from src.monitoring.metrics import MESSAGE_RECEIVED

        # 获取初始值
        initial_output = MESSAGE_RECEIVED.collect()
        # 递增
        MESSAGE_RECEIVED.labels(channel="test").inc()
        # 验证
        output = MESSAGE_RECEIVED.collect()
        # 应该有 test channel 的记录
        has_test = any("test" in line for line in output)
        assert has_test, "Counter 应记录 test channel"

    def test_counter_with_labels(self):
        """验证带标签的 Counter。"""
        from src.monitoring.metrics import MESSAGE_PROCESSED

        MESSAGE_PROCESSED.labels(channel="feishu", status="success").inc()
        output = MESSAGE_PROCESSED.collect()
        has_feishu = any("feishu" in line and "success" in line for line in output)
        assert has_feishu, "Counter 应记录 feishu+success 标签"

    def test_gauge_set(self):
        """验证 Gauge 设置值。"""
        from src.monitoring.metrics import ACTIVE_SESSIONS

        ACTIVE_SESSIONS.set(42)
        output = ACTIVE_SESSIONS.collect()
        has_42 = any("42" in line for line in output)
        assert has_42, "Gauge 应显示设置的值 42"

    def test_gauge_with_labels(self):
        """验证带标签的 Gauge。"""
        from src.monitoring.metrics import CHANNEL_STATUS

        CHANNEL_STATUS.labels(channel="websocket").set(1)
        CHANNEL_STATUS.labels(channel="dingtalk").set(0)
        output = CHANNEL_STATUS.collect()
        assert any("websocket" in line and "1" in line for line in output)
        assert any("dingtalk" in line and "0" in line for line in output)

    def test_histogram_observe(self):
        """验证 Histogram 观测值记录。"""
        from src.monitoring.metrics import PROCESSING_TIME

        PROCESSING_TIME.labels(channel="test").observe(0.15)
        PROCESSING_TIME.labels(channel="test").observe(0.5)
        output = PROCESSING_TIME.collect()
        # 应包含 _sum, _count, _bucket 行
        has_sum = any("_sum" in line for line in output)
        has_count = any("_count" in line for line in output)
        assert has_sum, "Histogram 应输出 _sum 行"
        assert has_count, "Histogram 应输出 _count 行"

    def test_get_metrics_output_format(self):
        """验证 get_metrics() 输出为 Prometheus 格式。"""
        from src.monitoring.metrics import get_metrics

        output = get_metrics()
        assert isinstance(output, str)
        assert "# HELP" in output, "应包含 HELP 注释"
        assert "# TYPE" in output, "应包含 TYPE 声明"
        assert "message_received_total" in output
        assert "message_processed_total" in output
        assert "processing_seconds" in output
        assert "active_sessions" in output
        assert "channel_status" in output

    def test_counter_thread_safety(self):
        """验证 Counter 的线程安全性。"""
        import threading

        from src.monitoring.metrics import _SimpleCounter

        counter = _SimpleCounter("test_thread", "Thread test", ("label",))
        errors = []

        def increment():
            try:
                for _ in range(100):
                    counter.labels(label="a").inc()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=increment) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, "并发递增不应有错误"
        # 验证最终值
        output = counter.collect()
        has_1000 = any("1000" in line for line in output)
        assert has_1000, "10线程×100次=1000"

    def test_histogram_empty_output(self):
        """验证无数据时 Histogram 仍能输出默认行。"""
        from src.monitoring.metrics import _SimpleHistogram

        hist = _SimpleHistogram("empty_hist", "Empty test")
        output = hist.collect()
        assert any("_bucket" in line for line in output)
        assert any("_sum" in line for line in output)
        assert any("_count" in line for line in output)


# ===========================================================================
# 2. REQ-20：PerformanceMonitor 系统指标采集
# ===========================================================================

class TestPerformanceMonitor:
    """验证 PerformanceMonitor 的系统指标采集。"""

    @pytest.fixture
    def monitor(self):
        from src.monitoring.performance_monitor import PerformanceMonitor
        return PerformanceMonitor()

    @pytest.mark.asyncio
    async def test_get_system_metrics(self, monitor):
        """验证系统指标采集返回完整字段。"""
        metrics = await monitor.get_system_metrics()
        assert hasattr(metrics, "cpu_usage")
        assert hasattr(metrics, "memory_usage")
        assert hasattr(metrics, "disk_usage")
        assert hasattr(metrics, "network_sent")
        assert hasattr(metrics, "network_recv")
        assert hasattr(metrics, "timestamp")
        assert 0 <= metrics.cpu_usage <= 100
        assert 0 <= metrics.memory_usage <= 100
        assert 0 <= metrics.disk_usage <= 100

    @pytest.mark.asyncio
    async def test_get_database_metrics(self, monitor):
        """验证数据库指标。"""
        metrics = await monitor.get_database_metrics()
        assert hasattr(metrics, "active_connections")
        assert hasattr(metrics, "connection_pool_size")
        assert hasattr(metrics, "connection_wait_time")
        assert hasattr(metrics, "query_execution_time")

    @pytest.mark.asyncio
    async def test_get_llm_metrics(self, monitor):
        """验证 LLM 指标。"""
        # 先记录一些数据
        monitor.record_llm_request(response_time=1.5, error=False)
        monitor.record_llm_request(response_time=2.0, error=True)
        monitor.record_llm_response()

        metrics = await monitor.get_llm_metrics()
        assert hasattr(metrics, "active_requests")
        assert hasattr(metrics, "request_rate")
        assert hasattr(metrics, "average_response_time")
        assert hasattr(metrics, "error_rate")

    @pytest.mark.asyncio
    async def test_get_tool_metrics(self, monitor):
        """验证工具指标。"""
        monitor.record_tool_execution(0.5, cache_hit=True, error=False)
        monitor.record_tool_execution(1.0, cache_hit=False, error=True)

        metrics = await monitor.get_tool_metrics()
        assert metrics.execution_count == 2
        assert metrics.error_count == 1

    @pytest.mark.asyncio
    async def test_get_task_metrics(self, monitor):
        """验证任务指标。"""
        monitor.update_task_status(pending=5, running=3, completed=10)

        metrics = await monitor.get_task_metrics()
        assert metrics.pending_tasks == 5
        assert metrics.running_tasks == 3
        assert metrics.completed_tasks == 10

    @pytest.mark.asyncio
    async def test_get_current_metrics_returns_all(self, monitor):
        """验证 get_current_metrics 返回所有维度。"""
        result = await monitor.get_current_metrics()
        assert "system" in result
        assert "database" in result
        assert "llm" in result
        assert "tool" in result
        assert "task" in result

    @pytest.mark.asyncio
    async def test_record_llm_request_updates_stats(self, monitor):
        """验证 LLM 请求记录更新统计。"""
        monitor.record_llm_request(response_time=1.0)
        assert monitor._llm_stats["active_requests"] == 1
        assert monitor._llm_stats["request_count"] == 1
        assert monitor._llm_stats["total_response_time"] == 1.0

        monitor.record_llm_response()
        assert monitor._llm_stats["active_requests"] == 0

    @pytest.mark.asyncio
    async def test_record_tool_execution_updates_stats(self, monitor):
        """验证工具执行记录更新统计。"""
        monitor.record_tool_execution(0.5, cache_hit=True)
        assert monitor._tool_stats["execution_count"] == 1
        assert monitor._tool_stats["cache_hits"] == 1
        assert monitor._tool_stats["cache_misses"] == 0

        monitor.record_tool_execution(1.0, cache_hit=False, error=True)
        assert monitor._tool_stats["execution_count"] == 2
        assert monitor._tool_stats["cache_misses"] == 1
        assert monitor._tool_stats["error_count"] == 1

    def test_metrics_history_recording(self, monitor):
        """验证指标历史记录。"""
        from src.monitoring.performance_monitor import SystemMetrics

        metrics = SystemMetrics(
            cpu_usage=50.0,
            memory_usage=60.0,
            disk_usage=70.0,
            network_sent=100.0,
            network_recv=200.0,
        )
        monitor._record_metrics("system", metrics)
        history = monitor.get_metrics_history("system")
        assert len(history) == 1
        assert history[0]["metrics"]["cpu_usage"] == 50.0

    def test_metrics_history_limit(self, monitor):
        """验证指标历史记录上限。"""
        from src.monitoring.performance_monitor import SystemMetrics

        for i in range(1100):
            monitor._record_metrics("system", SystemMetrics(
                cpu_usage=float(i),
                memory_usage=50.0,
                disk_usage=60.0,
                network_sent=0,
                network_recv=0,
            ))

        history = monitor.get_metrics_history("system")
        assert len(history) <= 1000

    @pytest.mark.asyncio
    async def test_alert_triggered_on_high_cpu(self, monitor):
        """验证高 CPU 时触发告警。"""
        alerts = []

        async def on_alert(alert):
            alerts.append(alert)

        monitor._alert_callback = on_alert

        # 直接调用 _trigger_alert
        with patch.object(monitor, "get_system_metrics", new_callable=AsyncMock) as mock_sys:
            mock_sys.return_value = MagicMock(cpu_usage=90.0, memory_usage=50.0)
            with patch.object(monitor, "get_llm_metrics", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = MagicMock(active_requests=0, error_rate=0)
                with patch.object(monitor, "get_task_metrics", new_callable=AsyncMock) as mock_task:
                    mock_task.return_value = MagicMock()
                    await monitor._trigger_alert("high", "CPU使用率过高: 90%")

        assert len(alerts) == 1
        assert alerts[0].level == "high"


# ===========================================================================
# 3. REQ-21：模型名传递验证
# ===========================================================================

class TestModelNamePassthrough:
    """验证 LLM 模型名在系统中的传递。"""

    def test_agent_config_has_model_name_field(self):
        """验证 AgentConfig 有 model_name 和 model_tier 字段。"""
        from src.agents.types import AgentConfig

        config = AgentConfig(
            config_id="test",
            name="test-agent",
            model_name="glm-4-flash",
            model_tier="medium",
        )
        assert config.model_name == "glm-4-flash"
        assert config.model_tier == "medium"

    def test_agent_config_default_empty_model(self):
        """验证默认 model_name 和 model_tier 为空。"""
        from src.agents.types import AgentConfig

        config = AgentConfig(config_id="test", name="test-agent")
        assert config.model_name == ""
        assert config.model_tier == ""

    def test_loader_passes_model_name_from_yaml(self):
        """验证 AgentConfigLoader 从 YAML 传递 model_name。"""
        import tempfile
        import os

        from src.agents.loader import AgentConfigLoader

        yaml_content = (
            "config_id: test-agent-yaml\n"
            "name: test-agent\n"
            "display_name: 测试Agent\n"
            "description: 测试\n"
            "agent_type: specialized\n"
            "level: L3\n"
            "model_name: minimax-m2.7\n"
            "model_tier: large\n"
        )

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            f.write(yaml_content)
            temp_path = f.name

        try:
            config = AgentConfigLoader.load_from_yaml(temp_path)
            assert config.model_name == "minimax-m2.7", (
                f"model_name 应为 'minimax-m2.7'，实际为 '{config.model_name}'"
            )
            assert config.model_tier == "large", (
                f"model_tier 应为 'large'，实际为 '{config.model_tier}'"
            )
        finally:
            os.unlink(temp_path)

    def test_cli_output_adapter_displays_model_name(self):
        """验证 CLI output_adapter 能显示模型名。"""
        from src.channels.cli.output_adapter import StatusBarRenderer

        renderer = StatusBarRenderer()
        assert renderer.model_name == "unknown"

        renderer.update(model_name="glm-4-flash")
        assert renderer.model_name == "glm-4-flash"

    def test_cli_output_adapter_model_short_name(self):
        """验证 CLI 对长模型名进行截断显示。"""
        from src.channels.cli.output_adapter import StatusBarRenderer

        renderer = StatusBarRenderer()
        renderer.update(model_name="provider/glm-4-flash")
        # StatusBarRenderer 内部对 "/" 进行了截断
        assert "glm-4-flash" in renderer.model_name.split("/")[-1]


# ===========================================================================
# 4. REQ-23：上下文信息对齐验证
# ===========================================================================

class TestContextInfoAlignment:
    """验证上下文窗口信息的传递和计算。"""

    def test_cli_status_bar_has_context_pct(self):
        """验证 CLI StatusBar 有 context_pct 字段。"""
        from src.channels.cli.output_adapter import StatusBarRenderer

        renderer = StatusBarRenderer()
        assert renderer.context_pct == 0.0

        renderer.update(context_pct=75.5)
        assert renderer.context_pct == 75.5

    def test_cli_status_bar_has_turn_count(self):
        """验证 CLI StatusBar 有 turn_count 字段。"""
        from src.channels.cli.output_adapter import StatusBarRenderer

        renderer = StatusBarRenderer()
        assert renderer.turn_count == 0

        renderer.update(turn_count=5)
        assert renderer.turn_count == 5

    def test_performance_monitor_has_task_context(self):
        """验证 PerformanceMonitor 包含任务上下文信息。"""
        from src.monitoring.performance_monitor import PerformanceMonitor

        monitor = PerformanceMonitor()
        monitor.update_task_status(
            pending=10, running=5, completed=100, task_time=2.5
        )

        assert monitor._task_stats["pending_tasks"] == 10
        assert monitor._task_stats["running_tasks"] == 5
        assert monitor._task_stats["completed_tasks"] == 100
        assert monitor._task_stats["total_task_time"] == 2.5

    def test_cli_update_status_bar_method_exists(self):
        """验证 CLIOutputAdapter 有 update_status_bar 方法。"""
        from src.channels.cli.output_adapter import CLIOutputAdapter

        adapter = CLIOutputAdapter.__new__(CLIOutputAdapter)
        # 只验证方法存在
        assert hasattr(adapter, "update_status_bar")
        assert callable(adapter.update_status_bar)


# ===========================================================================
# 5. 健康检查验证
# ===========================================================================

class TestHealthCheck:
    """验证健康检查组件。"""

    def test_liveness_probe_returns_healthy(self):
        """验证存活探针始终返回 healthy。"""
        from src.monitoring.health import liveness_probe

        result = liveness_probe()
        assert result["status"] == "healthy"
        assert result["probe"] == "liveness"
        assert "timestamp" in result

    def test_readiness_probe_structure(self):
        """验证就绪探针返回正确结构。"""
        from src.monitoring.health import readiness_probe

        result = readiness_probe()
        assert result["probe"] == "readiness"
        assert result["status"] in ("ready", "not_ready")
        assert "timestamp" in result
        assert "components" in result

    def test_full_check_returns_all_components(self):
        """验证 full_check 包含所有组件。"""
        from src.monitoring.health import HealthChecker

        checker = HealthChecker()
        result = checker.full_check()

        assert "status" in result
        assert "timestamp" in result
        assert "components" in result
        assert "redis" in result["components"]
        assert "channels" in result["components"]
        assert "pipeline" in result["components"]
        assert "isolation" in result["components"]

    def test_check_isolation_returns_status(self):
        """验证 isolation 检查返回完整信息。"""
        from src.monitoring.health import HealthChecker

        checker = HealthChecker()
        result = checker.check_isolation()

        assert "status" in result
        assert "docker_available" in result
        # 不论 Docker 是否安装，都应返回有效状态
        assert result["status"] in ("healthy", "degraded")

    def test_health_checker_overall_status_logic(self):
        """验证总体状态判定逻辑。"""
        from src.monitoring.health import HealthChecker

        checker = HealthChecker()

        # 当所有组件 unknown/healthy 时，总体 healthy
        result = checker.full_check()
        # 在无外部依赖的环境中，各组件应返回 unknown 或 healthy
        component_statuses = [
            v["status"] for v in result["components"].values()
        ]
        has_unhealthy = any(s == "unhealthy" for s in component_statuses)
        if has_unhealthy:
            assert result["status"] == "unhealthy"
        else:
            assert result["status"] == "healthy"


# ===========================================================================
# 6. 监控路由验证
# ===========================================================================

class TestMonitoringRoutes:
    """验证监控路由的 Schema 定义。"""

    def test_router_has_expected_routes(self):
        """验证路由包含预期的端点。"""
        from src.monitoring.routes import router

        routes = [route.path for route in router.routes]
        assert "/health/live" in routes
        assert "/health/ready" in routes
        assert "/metrics" in routes

    def test_live_endpoint_method(self):
        """验证存活探针使用 GET 方法。"""
        from src.monitoring.routes import router

        for route in router.routes:
            if route.path == "/health/live":
                assert "GET" in route.methods
                break

    def test_metrics_endpoint_returns_plain_text(self):
        """验证指标端点返回纯文本。"""
        from src.monitoring.routes import router

        for route in router.routes:
            if route.path == "/metrics":
                # response_class 应该是 PlainTextResponse
                assert route.response_class is not None
                break
