"""测试公共配置。"""

import sys
import os

collect_ignore = [
    "suites",
    "test_cross_domain_discovery.py",
    "test_directory_generator.py",
    "test_memory_metrics.py",
    "test_pgvector_store.py",
    "test_state_evolution_levels.py",
    "test_task_submit_event_chain.py",
    "test_yaml_error_chain.py",
]

# 将 src 目录添加到 Python 路径，确保测试能正确导入项目模块
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
