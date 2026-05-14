"""通道适配器模块。

提供管道与外部系统之间的输入/输出适配层：
- IInputAdapter: 输入适配器基类，接收外部请求
- IOutputAdapter: 输出适配器基类，输出管道结果
"""

from channels.input_adapter import IInputAdapter
from channels.output_adapter import IOutputAdapter

__all__ = [
    "IInputAdapter",
    "IOutputAdapter",
]
