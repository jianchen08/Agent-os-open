"""tests/suites/ — 按域分组的测试套件。

现存套件：
1. core    - 核心稳定性套件（管道稳定性/插件修复/任务状态）
2. task    - 任务域套件（长期稳定性/竞态与注入）
3. plugins - 插件域套件（bash sidecar state）
4. e2e     - 隔离模式端到端
5. llm     - LLM 配置兜底

0.2 架构说明：src/ 已删除（迁移到 plugins/ 与 kernel/），原"把 src/ 加入
sys.path"的注入已移除；各套件如需插件源码路径，经各自的 conftest /
tests/_*_path 辅助模块注入。

使用方法：
    pytest tests/suites/ -v                           # 运行所有测试
    pytest tests/suites/core/ -v                      # 只运行核心单元测试
    pytest tests/suites/ -m "not integration" -v       # 跳过集成测试
"""

from pathlib import Path

import pytest


def pytest_configure(config):
    """注册测试标记"""
    config.addinivalue_line("markers", "integration: 需要真实LLM API的集成测试")
    config.addinivalue_line("markers", "e2e: 端到端测试")
    config.addinivalue_line("markers", "unit: 单元测试")
    config.addinivalue_line("markers", "core: 核心单元测试")
    config.addinivalue_line("markers", "task: Task E2E测试")
    config.addinivalue_line("markers", "llm: LLM相关测试")


def pytest_collection_modifyitems(config, items):
    """根据测试文件路径自动添加标记"""
    for item in items:
        file_path = Path(item.fspath).as_posix() if hasattr(item, 'fspath') else ""

        if "/suites/core/" in file_path or "\suites\core\\" in file_path:
            item.add_marker(pytest.mark.core)
            item.add_marker(pytest.mark.unit)
        elif "/suites/task/" in file_path or "\suites\task\\" in file_path:
            item.add_marker(pytest.mark.task)
            item.add_marker(pytest.mark.unit)
        elif "/suites/llm/" in file_path or "\suites\llm\\" in file_path:
            item.add_marker(pytest.mark.llm)
            item.add_marker(pytest.mark.integration)


def pytest_report_header(config):
    """添加测试报告头"""
    suites_dir = Path(__file__).parent
    return [
        "=" * 70,
        "Agent OS 统一测试套件",
        "=" * 70,
        f"测试目录: {suites_dir}",
        "",
        "可用标记:",
        "  - core       : 核心单元测试",
        "  - task       : Task E2E测试",
        "  - llm        : LLM测试",
        "  - integration: 集成测试 (需要 --run-integration)",
        "  - unit       : 单元测试",
        "  - e2e        : 端到端测试",
        "",
        "使用方法:",
        "  pytest tests/suites/ -v                           # 所有测试",
        "  pytest tests/suites/ -m core -v                   # 核心测试",
        "  pytest tests/suites/ -m 'not integration' -v       # 跳过集成测试",
        "=" * 70,
    ]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
