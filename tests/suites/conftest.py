"""pytest 配置 — 集成测试标记 + MiniMax M2.7 fixture + E2E fixture"""

import os

import pytest
from rich.console import Console


def pytest_addoption(parser):
    """添加 --run-integration 命令行选项。"""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that call real LLM APIs",
    )


def pytest_configure(config):
    """注册 integration 标记。"""
    config.addinivalue_line(
        "markers",
        "integration: LLM integration tests (require --run-integration)",
    )


def pytest_collection_modifyitems(config, items):
    """未指定 --run-integration 时跳过集成测试。

    自动将 config_cli_test / real_cli_test / real_e2e_full_test 中的测试
    标记为 integration，这些测试需要真实 LLM API 和外部服务。
    """
    integration_files = {
        "config_cli_test",
        "real_cli_test",
        "real_e2e_full_test",
    }
    if not config.getoption("--run-integration"):
        skip_integration = pytest.mark.skip(reason="Need --run-integration to run")
        for item in items:
            # 对已有 integration 标记的测试跳过
            if "integration" in item.keywords:
                item.add_marker(skip_integration)
                continue
            # 自动标记三个 E2E 测试文件中的测试为 integration
            module_name = item.module.__name__.rsplit(".", 1)[-1] if "." in item.module.__name__ else item.module.__name__
            if module_name in integration_files:
                item.add_marker(pytest.mark.integration)
                item.add_marker(skip_integration)


# ---------------------------------------------------------------------------
# E2E 测试所需 fixture（config_cli_test / real_cli_test / real_e2e_full_test）
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def console():
    """Rich Console 实例 fixture，供 E2E 测试使用。"""
    return Console()


@pytest.fixture(scope="session")
def services():
    """共享服务字典 fixture。

    对于需要真实服务的 E2E 测试（如 real_e2e_full_test），
    这些测试被 --run-integration 保护，默认不执行。
    此处提供空字典占位，避免 pytest 收集时报 fixture not found。
    真正运行时由各测试文件的 main() 自行构建完整服务。
    """
    return {}


@pytest.fixture(scope="session")
def engine():
    """PipelineEngine 实例 fixture。

    对于需要真实管道引擎的 E2E 测试（如 real_e2e_full_test），
    这些测试被 --run-integration 保护，默认不执行。
    此处提供 None 占位，避免 pytest 收集时报 fixture not found。
    真正运行时由各测试文件的 main() 自行构建引擎。
    """
    return None


@pytest.fixture(scope="session")
def minimax_config():
    """MiniMax M2.7 测试模型配置 fixture。"""
    return {
        "provider": "minimax",
        "model_name": "MiniMax-M2.7",
        "api_base": "https://api.minimaxi.com/v1",
        "api_key": os.environ.get(
            "MINIMAX_API_KEY",
            "sk-cp-I-8VhT0NLEJJQoa8le9gV0NhiPGglEsiBo39dkcgcbdaP8Dxl19zGpvXkbg3Tgts4KSyQWMKv1ooCX2JjNYN6mSv6z8Ia4cvObX-DUsdfJvILgVfDsmth5Y",
        ),
        "default_params": {"temperature": 0.7, "max_tokens": 8192},
    }


# ---------------------------------------------------------------------------
# 评估稳定性测试所需 fixture + MockAgentRegistry
# ---------------------------------------------------------------------------


class MockAgentConfig:
    """测试用 Agent 配置 Mock，供 MockAgentRegistry 按 config_id 查找。"""

    def __init__(self, config_id: str, name: str = "") -> None:
        self.config_id = config_id
        self.name = name


class MockAgentRegistry:
    """测试用 Agent 注册表 Mock。

    模拟 agent_registry 的 get / list_all 接口，
    支持按 config_id 和 name 查找。
    """

    def __init__(self, configs: list | None = None) -> None:
        self._configs = configs or []

    def get(self, config_id: str):
        """按 config_id 查找 Agent 配置。"""
        for cfg in self._configs:
            if getattr(cfg, "config_id", None) == config_id:
                return cfg
        return None

    def list_all(self) -> list:
        """返回所有 Agent 配置。"""
        return list(self._configs)
