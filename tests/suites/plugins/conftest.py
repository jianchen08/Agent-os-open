"""插件测试 conftest — 通过 importlib 直接加载模块，绕过 __init__.py 导入链。

plugins/__init__.py 和 plugins/output/__init__.py 存在导入不一致问题，
本 conftest 提供直接从文件路径加载模块的工具函数。
"""

import importlib.util
import os
import sys


# src 目录的绝对路径
_SRC_DIR = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "src"
))

# 模块缓存
_module_cache: dict[str, object] = {}


def load_module_from_file(module_name: str, file_path: str):
    """通过文件路径直接加载 Python 模块，绕过 __init__.py。

    Args:
        module_name: 模块注册名称
        file_path: 模块文件绝对路径

    Returns:
        加载的模块对象
    """
    if module_name in _module_cache:
        return _module_cache[module_name]

    if module_name in sys.modules:
        return sys.modules[module_name]

    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None:
        raise ImportError(f"Cannot load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    _module_cache[module_name] = module
    spec.loader.exec_module(module)
    return module


def _get_plugin_classes():
    """预加载所有需要的插件模块，返回类名字到类的映射。"""
    classes = {}

    # Input plugins
    input_dir = os.path.join(_SRC_DIR, "plugins", "input")
    output_dir = os.path.join(_SRC_DIR, "plugins", "output")

    input_modules = {
        "tool_call_guard": os.path.join(input_dir, "tool_call_guard.py"),
        "security_check": os.path.join(input_dir, "security_check", "plugin.py"),
        "isolation_guard": os.path.join(input_dir, "isolation_guard", "plugin.py"),
        "level_guard": os.path.join(input_dir, "level_guard.py"),
        "cost_control": os.path.join(input_dir, "cost_control.py"),
        "tool_schema_validator": os.path.join(input_dir, "tool_schema_validator.py"),
        "knowledge_inject": os.path.join(input_dir, "knowledge_inject.py"),
        "prompt_build": os.path.join(input_dir, "prompt_build.py"),
        "circuit_breaker": os.path.join(input_dir, "circuit_breaker.py"),
        "message_inject": os.path.join(input_dir, "message_inject.py"),
        "tool_cache": os.path.join(input_dir, "tool_cache.py"),
        "memory_read": os.path.join(input_dir, "memory_read.py"),
        "tool_schema": os.path.join(input_dir, "tool_schema.py"),
        "reasoning_check": os.path.join(input_dir, "reasoning_check.py"),
    }

    output_modules = {
        "output_repetition_guard": os.path.join(output_dir, "output_repetition_guard.py"),
    }

    for mod_name, file_path in {**input_modules, **output_modules}.items():
        try:
            mod = load_module_from_file(mod_name, file_path)
            for attr_name in dir(mod):
                attr = getattr(mod, attr_name)
                if isinstance(attr, type) and attr_name not in classes:
                    classes[attr_name] = attr
        except Exception:
            pass

    return classes


# 预加载映射（懒加载）
_plugin_classes = None


def get_plugin_class(class_name: str):
    """获取插件类。

    Args:
        class_name: 插件类名

    Returns:
        插件类
    """
    global _plugin_classes
    if _plugin_classes is None:
        _plugin_classes = _get_plugin_classes()
    return _plugin_classes.get(class_name)
