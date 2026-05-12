"""LLM 适配器模块"""

# 从父目录的 adapters.py 模块导入 LLMClientAdapter
# 问题是：存在 adapters/ 目录和 adapters.py 文件同名冲突
# 解决：动态加载父模块中的 LLMClientAdapter
import importlib.util
import sys
from pathlib import Path


def _load_llm_client_adapter():
    """动态加载 LLMClientAdapter，避免模块名冲突"""
    # 检查是否已经加载过
    module_name = "_llm_adapters_file_module"
    if module_name in sys.modules:
        return sys.modules[module_name].LLMClientAdapter

    # 获取 adapters.py 的路径（父目录中的 adapters.py）
    adapters_file = Path(__file__).resolve().parent.parent / "adapters.py"

    if not adapters_file.exists():
        raise ImportError(f"找不到 adapters.py 文件: {adapters_file}")

    # 动态加载模块
    spec = importlib.util.spec_from_file_location(module_name, adapters_file)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块: {adapters_file}")

    _adapters_module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = _adapters_module
    spec.loader.exec_module(_adapters_module)

    return _adapters_module.LLMClientAdapter


# 导出 LLMClientAdapter
LLMClientAdapter = _load_llm_client_adapter()

__all__ = ["LLMClientAdapter"]
