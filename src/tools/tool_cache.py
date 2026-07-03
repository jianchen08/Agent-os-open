"""工具缓存管理。

从 ToolExecutor 中提取的缓存职责，包括缓存配置、键生成、读写、统计和清理。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path  # noqa: F401
from typing import Any

import yaml  # noqa: F401
from pydantic import BaseModel, Field

from core.results import ToolExecutionResult

logger = logging.getLogger(__name__)


class ToolCacheConfig(BaseModel):
    """工具缓存配置"""

    enabled: bool = Field(default=True, description="是否启用缓存")
    default_ttl: int = Field(default=300, description="默认 TTL（秒）")
    tools: dict[str, dict[str, Any]] = Field(default_factory=dict, description="按工具配置")

    @classmethod
    def load_from_file(cls, path: str = "config/builtin_tools_config.yaml"):
        """从配置文件加载（通过 ConfigCenter 统一缓存）"""
        try:
            from config.config_center import get_config_center  # noqa: PLC0415

            rel = path.replace("config/", "", 1) if path.startswith("config/") else path
            data = get_config_center().get(rel) or {}
        except Exception:
            return cls()

        # 处理不同的配置文件格式
        if isinstance(data, list):
            return cls()

        cache_config = data.get("tool_cache", {})
        return cls(
            enabled=cache_config.get("enabled", True),
            default_ttl=cache_config.get("default_ttl", 300),
            tools=cache_config.get("tools", {}),
        )

    def is_cacheable(self, tool_name: str) -> bool:
        """检查工具是否可缓存"""
        if not self.enabled:
            return False

        tool_config = self.tools.get(tool_name, {})
        return tool_config.get("enabled", False)

    def get_ttl(self, tool_name: str) -> int:
        """获取工具的 TTL"""
        tool_config = self.tools.get(tool_name, {})
        return tool_config.get("ttl", self.default_ttl)


class ToolCache:
    """工具缓存管理器。

    封装缓存实例的延迟初始化、键生成、读写操作和统计信息。

    Args:
        cache_config: 缓存配置
    """

    def __init__(self, cache_config: ToolCacheConfig) -> None:
        self._cache_config = cache_config
        self._cache: Any | None = None  # 延迟初始化
        self._cache_hits: int = 0
        self._cache_misses: int = 0

    @property
    def config(self) -> ToolCacheConfig:
        """返回缓存配置。"""
        return self._cache_config

    def get_cache(self) -> Any:
        """获取缓存实例（延迟初始化）。"""
        if self._cache is None and self._cache_config.enabled:
            try:
                from cache.multi_level_cache import get_global_cache  # noqa: PLC0415

                self._cache = get_global_cache()
            except ImportError:
                logger.warning("缓存模块不可用，禁用工具缓存")
                self._cache_config.enabled = False
        return self._cache

    def _normalize_inputs_for_cache(self, inputs: dict[str, Any]) -> dict[str, Any]:
        """规范化输入参数，移除无关字段，提高缓存命中率。"""
        normalized: dict[str, Any] = {}
        skip_keys = {
            "timestamp",
            "request_id",
            "session_id",
            "user_id",
            "tool_call_id",
            "execution_id",
        }
        for key, value in inputs.items():
            if key in skip_keys:
                continue
            if isinstance(value, dict):
                nested = self._normalize_inputs_for_cache(value)
                if nested:
                    normalized[key] = nested
            elif value is not None and value != "":
                normalized[key] = value
        return normalized

    def generate_cache_key(self, tool_name: str, inputs: dict[str, Any]) -> str:
        """生成缓存键。"""
        try:
            normalized_inputs = self._normalize_inputs_for_cache(inputs)
            inputs_str = json.dumps(normalized_inputs, sort_keys=True, default=str)
            inputs_hash = hashlib.sha256(inputs_str.encode()).hexdigest()[:16]
            return f"tool:{tool_name}:{inputs_hash}"
        except Exception:
            return f"tool:{tool_name}:{hash(str(inputs))}"

    def should_cache(self, tool_name: str, inputs: dict[str, Any]) -> bool:
        """判断是否应该缓存工具执行结果。"""
        if not self._cache_config.is_cacheable(tool_name):
            return False

        no_cache_tools = ["task_submit"]
        if tool_name in no_cache_tools:
            return False

        return not _contains_sensitive_info(inputs)

    async def get_cached_result(self, tool_name: str, inputs: dict[str, Any]) -> ToolExecutionResult | None:
        """获取缓存的结果。"""
        if not self._cache_config.is_cacheable(tool_name):
            return None

        cache = self.get_cache()
        if cache is None:
            return None

        cache_key = self.generate_cache_key(tool_name, inputs)
        try:
            cached_data = await cache.get(cache_key)
            if cached_data is not None:
                self._cache_hits += 1
                logger.debug("缓存命中: %s", cache_key)
                return ToolExecutionResult(**cached_data)
        except Exception as e:
            logger.warning("读取缓存失败: %s", e)

        self._cache_misses += 1
        return None

    async def set_cached_result(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        result: ToolExecutionResult,
    ) -> None:
        """缓存结果。"""
        if not self._cache_config.is_cacheable(tool_name):
            return

        if not result.success:
            return

        cache = self.get_cache()
        if cache is None:
            return

        cache_key = self.generate_cache_key(tool_name, inputs)
        ttl = self._cache_config.get_ttl(tool_name)

        try:
            cache_data = result.model_dump()
            await cache.set(cache_key, cache_data, ttl)
            logger.debug("缓存结果: %s, TTL: %d", cache_key, ttl)
        except Exception as e:
            logger.warning("写入缓存失败: %s", e)

    def get_cache_stats(self) -> dict[str, Any]:
        """获取缓存统计。"""
        total = self._cache_hits + self._cache_misses
        hit_rate = self._cache_hits / total if total > 0 else 0

        return {
            "enabled": self._cache_config.enabled,
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "total": total,
            "hit_rate": round(hit_rate * 100, 2),
        }

    async def clear_tool_cache(self, tool_name: str | None = None) -> int:
        """清除工具缓存。"""
        cache = self.get_cache()
        if cache is None:
            return 0

        pattern = f"tool:{tool_name}:*" if tool_name else "tool:*"
        try:
            count = await cache.clear_pattern(pattern)
            logger.info("清除缓存: %s, 数量: %d", pattern, count)
            return count
        except Exception as e:
            logger.warning("清除缓存失败: %s", e)
            return 0


def _contains_sensitive_info(inputs: dict[str, Any]) -> bool:
    """判断输入中是否包含敏感信息。"""
    sensitive_keys = ["password", "token", "secret", "key", "credential"]

    def check_sensitive(data: Any) -> bool:
        if isinstance(data, dict):
            for key, value in data.items():
                if any(sensitive in key.lower() for sensitive in sensitive_keys):
                    return True
                if check_sensitive(value):
                    return True
        elif isinstance(data, str):
            return any(sensitive in data.lower() for sensitive in sensitive_keys)
        return False

    return check_sensitive(inputs)
