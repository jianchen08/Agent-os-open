"""健康检查模块测试。

覆盖场景：
- HealthChecker 初始化与各组件检查
- liveness_probe 始终返回 healthy
- readiness_probe 根据依赖状态返回不同结果
- 统一 JSON 格式输出
"""

from __future__ import annotations


from monitoring.health import HealthChecker, liveness_probe, readiness_probe


class TestHealthChecker:
    """HealthChecker 类测试。"""

    def test_init_default(self) -> None:
        """默认初始化。"""
        checker = HealthChecker()
        assert checker is not None

    def test_check_redis_success(self) -> None:
        """check_redis 在无 Redis 时降级返回 unknown（不报错）。"""
        checker = HealthChecker()
        result = checker.check_redis()
        assert "status" in result
        assert result["status"] in ("healthy", "unhealthy", "unknown")

    def test_check_channels_returns_dict(self) -> None:
        """check_channels 返回通道状态字典。"""
        checker = HealthChecker()
        result = checker.check_channels()
        assert isinstance(result, dict)
        assert "status" in result

    def test_check_pipeline_returns_dict(self) -> None:
        """check_pipeline 返回管道状态字典。"""
        checker = HealthChecker()
        result = checker.check_pipeline()
        assert isinstance(result, dict)
        assert "status" in result

    def test_full_check_returns_all_components(self) -> None:
        """full_check 包含所有组件状态。"""
        checker = HealthChecker()
        result = checker.full_check()
        assert isinstance(result, dict)
        # 必须包含关键字段
        assert "status" in result
        assert "components" in result
        components = result["components"]
        assert "redis" in components
        assert "channels" in components
        assert "pipeline" in components


class TestLivenessProbe:
    """liveness_probe 存活探针测试。"""

    def test_liveness_returns_healthy(self) -> None:
        """存活探针始终返回 healthy。"""
        result = liveness_probe()
        assert result["status"] == "healthy"

    def test_liveness_has_timestamp(self) -> None:
        """存活探针包含时间戳。"""
        result = liveness_probe()
        assert "timestamp" in result

    def test_liveness_has_probe_type(self) -> None:
        """存活探针标识类型。"""
        result = liveness_probe()
        assert result["probe"] == "liveness"


class TestReadinessProbe:
    """readiness_probe 就绪探针测试。"""

    def test_readiness_returns_dict(self) -> None:
        """就绪探针返回字典。"""
        result = readiness_probe()
        assert isinstance(result, dict)
        assert "status" in result
        assert "probe" in result
        assert result["probe"] == "readiness"

    def test_readiness_has_timestamp(self) -> None:
        """就绪探针包含时间戳。"""
        result = readiness_probe()
        assert "timestamp" in result

    def test_readiness_has_components(self) -> None:
        """就绪探针包含组件检查详情。"""
        result = readiness_probe()
        assert "components" in result

    def test_readiness_status_is_valid(self) -> None:
        """就绪探针状态只能是 ready 或 not_ready。"""
        result = readiness_probe()
        assert result["status"] in ("ready", "not_ready")
