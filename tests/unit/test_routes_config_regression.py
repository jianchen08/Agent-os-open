"""
回归测试：验证设置保存不生效 + API数据不一致的修复。

Bug 1 根因：并发配置 GET 从 llm.yaml 读，PUT 写入 concurrency_config.yaml（读写不同文件）
Bug 2 根因：API 配置 GET 返回硬编码，PUT 只回显不持久化

修复：GET/PUT 指向同一文件，实现真正的读写一致。
"""
import pytest
from pathlib import Path
import yaml
import tempfile
import os

# 直接导入被测函数
from src.channels.api.routes_config import (
    get_api_config,
    save_api_config,
    get_concurrency_config,
    save_concurrency_config,
    _read_yaml,
    _write_yaml,
    _API_YAML,
    _CONCURRENCY_YAML,
)


class TestApiConfigSavePersistence:
    """Bug 2 回归：API 配置保存后重新加载应返回已保存的数据"""

    def test_api_config_save_and_reload(self, tmp_path):
        """保存 API 配置后，GET 应返回已保存的数据，而非硬编码默认值"""
        import src.channels.api.routes_config as mod

        # 备份原始路径
        original_api_yaml = mod._API_YAML
        test_file = tmp_path / "api_config.yaml"
        mod._API_YAML = test_file

        try:
            new_config = {
                "endpoint": {
                    "base_url": "http://custom-host:9999",
                    "version": "v2",
                    "timeout": 60,
                },
                "rate_limit": {
                    "global_limit": "200/minute",
                    "auth": "10/minute",
                    "tasks": "50/minute",
                    "websocket": "100/minute",
                },
                "cors_origins": ["http://localhost:3000"],
            }

            # 保存
            result = save_api_config(new_config)
            assert result == new_config

            # 重新加载 - 核心断言：GET 必须返回刚才保存的数据
            loaded = get_api_config()
            assert loaded["endpoint"]["base_url"] == "http://custom-host:9999"
            assert loaded["endpoint"]["timeout"] == 60
            assert loaded["cors_origins"] == ["http://localhost:3000"]
        finally:
            mod._API_YAML = original_api_yaml
            if test_file.exists():
                test_file.unlink()


class TestConcurrencyConfigSavePersistence:
    """Bug 1 回归：并发配置保存后重新加载应返回已保存的数据"""

    def test_concurrency_config_save_and_reload(self, tmp_path):
        """保存并发配置后，GET 应从同一文件读取已保存的数据"""
        import src.channels.api.routes_config as mod

        original_concurrency_yaml = mod._CONCURRENCY_YAML
        test_file = tmp_path / "concurrency_config.yaml"
        mod._CONCURRENCY_YAML = test_file

        try:
            new_config = {
                "task": {
                    "max_concurrent_tasks": 10,
                    "task_max_workers": 20,
                    "task_timeout": 120,
                },
                "agent": {
                    "l1_max_concurrent": 5,
                    "l2_max_concurrent": 8,
                    "l3_max_concurrent": 16,
                },
                "workflow": {"max_concurrent": 6},
                "llm": {
                    "zhipu_max_concurrent": 4,
                    "openai_max_concurrent": 3,
                    "anthropic_max_concurrent": 3,
                    "default_max_concurrent": 2,
                },
            }

            # 保存
            result = save_concurrency_config(new_config)
            assert result == new_config

            # 重新加载 - 核心断言：GET 必须返回刚才保存的数据（从同一文件）
            loaded = get_concurrency_config()
            assert loaded["task"]["max_concurrent_tasks"] == 10
            assert loaded["agent"]["l1_max_concurrent"] == 5
            assert loaded["llm"]["openai_max_concurrent"] == 3
        finally:
            mod._CONCURRENCY_YAML = original_concurrency_yaml
            if test_file.exists():
                test_file.unlink()
