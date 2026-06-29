"""
Token 用量监控 - 测试监控页面 token 使用量统计功能

根因: _get_token_usage() 只查 usage_monitor 和 performance_monitor 两个
ServiceProvider 服务，两者从未注册 → 永远返回零值兜底。
而实际有数据的 ExecutionRecordStorage（已注册、有 get_total_tokens() 方法）
从未被查询。

修复后: _get_token_usage() 应优先从 ExecutionRecordStorage 读取真实 token 数据。
"""
from unittest.mock import MagicMock

from channels.api.routes_missing import _get_token_usage


def _patch_provider(monkeypatch, storage):
    """统一 mock ServiceProvider。

    确保 usage_monitor 和 performance_monitor 都不可用（模拟真实未注册状态），
    只注入 execution_record_storage。
    """
    mock_provider = MagicMock()

    def _get(key):
        if key == "execution_record_storage":
            return storage
        return None

    mock_provider.get.side_effect = _get
    mock_provider.get_or_create.return_value = storage

    monkeypatch.setattr(
        "infrastructure.service_provider.get_service_provider",
        lambda: mock_provider,
    )


def _make_storage_with_data():
    """构造有真实 summary 数据的 mock ExecutionRecordStorage。"""
    storage = MagicMock()
    storage.get_total_tokens.return_value = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "total_tokens": 1500,
        "cached_tokens": 100,
    }
    # 三条管道，各自跑了 5/3/2 轮迭代（每轮迭代 = 一次 LLM 调用），
    # 故真实 LLM 请求数 = 5+3+2 = 10，而非管道运行数 3。
    def _summary(iters):
        s = MagicMock()
        s.total_iterations = iters
        return s

    storage.list_all_summaries.return_value = [_summary(5), _summary(3), _summary(2)]
    return storage


def _make_storage_empty():
    """构造空数据的 mock ExecutionRecordStorage。"""
    storage = MagicMock()
    storage.get_total_tokens.return_value = {}
    storage.list_all_summaries.return_value = []
    return storage


class TestGetTokenUsageFromStorage:
    """Token 用量统计 — 从 ExecutionRecordStorage 读取真实数据。"""

    def test_returns_real_tokens_from_storage(self, monkeypatch):
        """测试: 当 ExecutionRecordStorage 有数据时，返回非零 token 统计。"""
        storage = _make_storage_with_data()
        _patch_provider(monkeypatch, storage)

        result = _get_token_usage()

        assert result["total_tokens"] == 1500, f"expected 1500, got {result['total_tokens']}"
        assert result["prompt_tokens"] == 1000, f"expected 1000, got {result['prompt_tokens']}"
        assert result["completion_tokens"] == 500, f"expected 500, got {result['completion_tokens']}"
        # request_count 是各管道迭代次数之和（每轮迭代 = 一次 LLM 调用），
        # 而非管道运行数。三条管道迭代 5+3+2 = 10 次请求。
        assert result["request_count"] == 10, f"expected 10, got {result['request_count']}"

    def test_returns_zero_when_storage_empty(self, monkeypatch):
        """测试: 当 ExecutionRecordStorage 无数据时，返回零值。"""
        storage = _make_storage_empty()
        _patch_provider(monkeypatch, storage)

        result = _get_token_usage()

        assert result["total_tokens"] == 0
        assert result["prompt_tokens"] == 0
        assert result["completion_tokens"] == 0

    def test_response_schema_complete(self, monkeypatch):
        """测试: 返回的 dict 包含前端 TokenUsage 接口所需的所有字段。"""
        storage = _make_storage_with_data()
        _patch_provider(monkeypatch, storage)

        result = _get_token_usage()

        # Schema 校验：前端 TokenUsage 接口需要这四个字段
        required_keys = {"total_tokens", "prompt_tokens", "completion_tokens", "request_count"}
        assert required_keys.issubset(result.keys()), \
            f"missing keys: {required_keys - result.keys()}"
        assert isinstance(result["total_tokens"], int)
        assert isinstance(result["prompt_tokens"], int)
        assert isinstance(result["completion_tokens"], int)
        assert isinstance(result["request_count"], int)
