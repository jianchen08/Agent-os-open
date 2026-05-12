"""
开发调试工具模块

提供开发友好的调试输出功能，支持在生产环境中安全关闭。
"""

import logging
import os
from typing import Any

# 开发模式开关（通过环境变量控制）
_DEV_MODE = os.getenv("APP_DEV_MODE", "false").lower() in ("true", "1", "yes")

# 创建调试 logger
_debug_logger = logging.getLogger("dev.debug")


def dev_print(*args, **kwargs) -> None:
    """
    开发调试输出函数

    行为：
    - 开发模式 (APP_DEV_MODE=true): 同时输出到 print() 和 logger
    - 生产模式: 只输出到 logger.debug()
    - 如果 logger 未配置，则使用 print()

    使用示例：
        from src.utils.debug import dev_print

        dev_print("调试信息:", variable)
        dev_print(f"当前状态: {state}")

    环境变量：
        export APP_DEV_MODE=true   # 启用开发模式（同时 print）
        export APP_DEV_MODE=false  # 生产模式（仅 logger）
    """
    # 格式化消息
    message = " ".join(str(arg) for arg in args)

    # 始终记录到 logger
    _debug_logger.debug(message)

    # 开发模式下同时使用 print()
    if _DEV_MODE:
        print(f"[DEV] {message}", **kwargs)


def dev_pprint(obj: Any, indent: int = 2) -> None:
    """
    美化打印复杂对象（字典、列表等）

    使用示例：
        dev_pprint({"key": "value", "nested": {"a": 1}})
    """
    import json

    message = json.dumps(obj, ensure_ascii=False, indent=indent)
    dev_print(message)


class DevDebug:
    """
    开发调试上下文管理器

    使用示例：
        with DevDebug("执行工具"):
            # 调试代码
            result = some_function()
    """

    def __init__(self, label: str, enabled: bool = True):
        self.label = label
        self.enabled = enabled and _DEV_MODE

    def __enter__(self):
        if self.enabled:
            dev_print(f"→ {self.label}...")

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.enabled:
            if exc_type is None:
                dev_print(f"✓ {self.label} 完成")
            else:
                dev_print(f"✗ {self.label} 失败: {exc_val}")


# 便捷函数
def debug_enter(func_name: str) -> None:
    """打印函数进入调试信息"""
    dev_print(f"→ 进入 {func_name}")


def debug_exit(func_name: str, result: Any = None) -> None:
    """打印函数退出调试信息"""
    if result is not None:
        dev_print(f"← 退出 {func_name}: {type(result).__name__}")
    else:
        dev_print(f"← 退出 {func_name}")


def debug_var(name: str, value: Any, max_length: int = 200) -> None:
    """打印变量值"""
    value_str = str(value)
    if len(value_str) > max_length:
        value_str = value_str[:max_length] + "..."

    dev_print(f"[{name}] = {value_str}")


def debug_call(func: callable) -> callable:
    """
    函数调用调试装饰器

    使用示例：
        @debug_call
        def my_function(x, y):
            return x + y
    """

    def wrapper(*args, **kwargs):
        func_name = func.__name__
        debug_enter(func_name)
        debug_var("args", args)
        debug_var("kwargs", kwargs)

        try:
            result = func(*args, **kwargs)
            debug_exit(func_name, result)
            return result
        except Exception as e:
            dev_print(f"✗ {func_name} 异常: {e}")
            raise

    return wrapper


def check_dev_mode() -> bool:
    """检查是否处于开发模式"""
    return _DEV_MODE


def set_dev_mode(enabled: bool) -> None:
    """设置开发模式（用于测试）"""
    global _DEV_MODE
    _DEV_MODE = enabled


# 初始化时输出状态
if _DEV_MODE:
    print("🔧 开发模式已启用 (APP_DEV_MODE=true)")
    print("   - 使用 dev_print() 进行调试输出")
    print("   - 输出将同时显示在控制台和日志文件")
else:
    _debug_logger.debug("生产模式：dev_print 仅输出到日志")
