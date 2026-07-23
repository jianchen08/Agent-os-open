"""测试套件主文件 - 统一入口

测试文件已整理到 tests/suites/ 目录下，按功能分为8个套件：
1. core       - 核心单元测试（Engine、Chain、Route、Tasks等）
2. m6_plugins - M6插件测试（Input/Output）
3. stage      - Stage闭环测试（Stage4/5/6、安全守卫）
4. cli        - CLI/E2E测试（需要 --run-integration）
5. memory     - 内存经验系统测试
6. task       - Task E2E测试
7. agent      - Agent测试
8. llm        - LLM测试

使用方法：
    pytest tests/suites/ -v                           # 运行所有测试
    pytest tests/suites/core/ -v                      # 只运行核心单元测试
    pytest tests/suites/core/test_engine.py -v        # 运行单个测试文件
    pytest tests/suites/ -m "not integration" -v       # 跳过集成测试
"""

import sys
from pathlib import Path

# 添加src到路径
SRC_DIR = Path(__file__).parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import pytest


def pytest_configure(config):
    """注册测试标记"""
    config.addinivalue_line("markers", "integration: 需要真实LLM API的集成测试")
    config.addinivalue_line("markers", "e2e: 端到端测试")
    config.addinivalue_line("markers", "unit: 单元测试")
    config.addinivalue_line("markers", "core: 核心单元测试")
    config.addinivalue_line("markers", "m6: M6插件测试")
    config.addinivalue_line("markers", "stage: Stage闭环测试")
    config.addinivalue_line("markers", "cli: CLI测试")
    config.addinivalue_line("markers", "memory: 内存经验测试")
    config.addinivalue_line("markers", "task: Task E2E测试")
    config.addinivalue_line("markers", "agent: Agent测试")
    config.addinivalue_line("markers", "llm: LLM相关测试")


def pytest_collection_modifyitems(config, items):
    """根据测试文件路径自动添加标记"""
    for item in items:
        file_path = Path(item.fspath).as_posix() if hasattr(item, 'fspath') else ""

        if "/suites/core/" in file_path or "\\suites\\core\\" in file_path:
            item.add_marker(pytest.mark.core)
            item.add_marker(pytest.mark.unit)
        elif "/suites/m6_plugins/" in file_path or "\\suites\\m6_plugins\\" in file_path:
            item.add_marker(pytest.mark.m6)
            item.add_marker(pytest.mark.unit)
        elif "/suites/stage/" in file_path or "\\suites\\stage\\" in file_path:
            item.add_marker(pytest.mark.stage)
            item.add_marker(pytest.mark.unit)
        elif "/suites/cli/" in file_path or "\\suites\\cli\\" in file_path:
            item.add_marker(pytest.mark.integration)
            item.add_marker(pytest.mark.cli)
            item.add_marker(pytest.mark.e2e)
        elif "/suites/memory/" in file_path or "\\suites\\memory\\" in file_path:
            item.add_marker(pytest.mark.memory)
            item.add_marker(pytest.mark.unit)
        elif "/suites/task/" in file_path or "\\suites\\task\\" in file_path:
            item.add_marker(pytest.mark.task)
            item.add_marker(pytest.mark.unit)
        elif "/suites/agent/" in file_path or "\\suites\\agent\\" in file_path:
            item.add_marker(pytest.mark.agent)
            item.add_marker(pytest.mark.unit)
        elif "/suites/llm/" in file_path or "\\suites\\llm\\" in file_path:
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
        "  - m6         : M6插件测试",
        "  - stage      : Stage闭环测试",
        "  - cli        : CLI测试 (需要 --run-integration)",
        "  - memory     : 内存经验测试",
        "  - task       : Task E2E测试",
        "  - agent      : Agent测试",
        "  - llm        : LLM测试",
        "  - integration: 集成测试 (需要 --run-integration)",
        "  - unit       : 单元测试",
        "  - e2e        : 端到端测试",
        "",
        "使用方法:",
        "  pytest tests/suites/ -v                           # 所有测试",
        "  pytest tests/suites/ -m core -v                   # 核心测试",
        "  pytest tests/suites/ -m unit -v                    # 单元测试",
        "  pytest tests/suites/ -m 'not integration' -v       # 跳过集成测试",
        "=" * 70,
    ]


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
