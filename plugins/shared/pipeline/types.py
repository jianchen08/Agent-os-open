"""pipeline.types 顶层 re-export——指向 _base/types.py。

保留此文件使老代码的 `from pipeline.types import ...` 能正常解析。
"""
from ._base.types import *  # noqa: F401, F403
from ._base.types import (  # noqa: F401
    ErrorPolicy,
    StateKeys,
    TargetType,
    create_initial_state,
)
