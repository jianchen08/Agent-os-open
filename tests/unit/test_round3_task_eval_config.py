"""
Round 3: 任务状态机完整性 + 评估引擎边界 + 配置管理边界测试。

补充 Round 1 未覆盖的场景：
1. 任务状态机：自转换拒绝、未知状态、多步链、实例隔离、工厂函数、空转换规则
2. 评估引擎边界：11种操作符对 None/空值/类型不匹配、深层嵌套路径、组合逻辑短路行为、默认评估
3. 配置管理边界：无效路径处理、多环境变量替换、空默认值、并发读取一致性
"""
import threading
from pathlib import Path

import pytest
import yaml

from src.config.loader import ConfigLoader
from src.core.exceptions import ConfigNotFoundError