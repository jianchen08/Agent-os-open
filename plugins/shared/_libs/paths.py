"""路径大小写 canon 单源（多端平台差异普查 ⚠️-2 收敛落点）。

Windows 文件系统大小写不敏感，路径比较前统一 normpath + 小写折叠；POSIX
大小写敏感，normpath 后原样返回。消费方（改比较口径先改这里，勿再散写
``norm(...).lower() if os.name == "nt"``）：

- ``project_manager/project_registry.py``（登记归一 ``_norm_path`` /
  ``_canonical_path`` 尾部）
- ``task_submit/tool.py``（挂靠项目 workspace 等价判定）

包边界副本（不可 import 共享根，靠对齐断言测试防漂移，改动须与单源同刀）：
- ``plugins/sdk/src/agentos_plugin_sdk/permission_checker.py::_canon_path``

语义口径（微差归一说明）：本函数只承载「normpath + nt 小写折叠」这一比较
尾语义，即现存各处实现的公共尾；abspath/resolve/realpath 等前置解析步骤是
各消费方自己的语义，留在调用方（project_registry 对登记路径先 abspath、
``_canonical_path`` 先做最近存在祖先 realpath，task_submit 只 normpath 原
串——均与本尾语义正交）。纯字符串变换不触盘（不做 realpath/exists）。
"""

from __future__ import annotations

import os

__all__ = ["canon_path"]


def canon_path(path: str) -> str:
    """路径比较用 canon：``normpath`` 后 Windows（``os.name == "nt"``）小写折叠。"""
    norm = os.path.normpath(path)
    return norm.lower() if os.name == "nt" else norm
