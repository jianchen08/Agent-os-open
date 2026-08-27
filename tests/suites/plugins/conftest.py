"""插件测试 conftest — 通过 importlib 直接加载模块，绕过包导入链。

0.2 插件为「目录/plugin.py」包结构、目录无 __init__.py 链路，
本 conftest 提供从文件路径直接加载模块的工具函数
（tests.suites.plugins.test_new_plugins 等消费）。
"""

import importlib.util
import os
import sys

# 模块缓存
_module_cache: dict[str, object] = {}


def load_module_from_file(module_name: str, file_path: str):
    """通过文件路径直接加载 Python 模块，绕过 __init__.py。

    插件目录已从扁平结构（plugins/input/X.py）重构为包结构
    （plugins/input/X/plugin.py）。本函数自动把过期的扁平路径重定向到
    新的包内 plugin.py，避免批量改测试调用点。

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

    # 路径兼容：X.py 不存在但 X/plugin.py 存在 → 自动重定向（插件包结构迁移）
    if not os.path.exists(file_path) and file_path.endswith(".py"):
        packaged = os.path.join(os.path.dirname(file_path),
                                os.path.basename(file_path)[:-3], "plugin.py")
        if os.path.exists(packaged):
            file_path = packaged

    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None:
        raise ImportError(f"Cannot load module {module_name} from {file_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    _module_cache[module_name] = module
    spec.loader.exec_module(module)
    return module
