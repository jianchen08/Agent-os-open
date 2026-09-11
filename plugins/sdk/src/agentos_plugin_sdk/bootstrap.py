"""插件 server.py sys.path 引导（SDK 单一真值源）。

0.2 插件 server.py 头部的「插件目录 + plugins/shared 根入 sys.path」引导样板
（67 处 ×26 形态，ADR 2026-09-08-plugin-bootstrap-sink）收敛于此。SDK 经
editable 安装（根环境 / 各 sidecar .venv / 合宿 _host/.venv 的
``__editable__.agentos_plugin_sdk`` .pth）在本模块被导入前已可达，故本模块
是唯一不需要引导的导入面——引导逻辑收敛到 SDK 不引入新的导入前提。

暴露接口：
- bootstrap_plugin(entry_file, extra=())：注入插件目录与共享根并置于 sys.path
  最前（去重幂等，重复引导重申解析优先权），返回三键路径；
  extra 为共享根相对子路径（如 "system"、"system/tasks"），供任务域等需要
  组根/包目录的插件显式声明注入意图（替代各 server.py 手写注入块）；
- Bootstrap：plugin_dir / group_root / shared_root（group_root 只返回不注入）。

裸名安全边界（ADR 决策 1）：只注入插件目录与共享根两项；组根（system/、
tools/、pipeline/input/ 等兄弟插件聚集目录）上 sys.path 会让兄弟插件的
plugin.py/tool.py 等裸名跨插件可达（合宿裸名冲突家族），需要组根的文件从
返回值派生路径后自行显式 insert，注入意图留在现场。
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass

__all__ = ["Bootstrap", "bootstrap_plugin"]

# 0.2 共享根布局指纹：同时含这三个分组子目录的祖先即 plugins/shared
_LAYOUT_FINGERPRINT = ("system", "tools", "pipeline")


@dataclass(frozen=True)
class Bootstrap:
    """bootstrap_plugin 的注入结果（绝对路径，可直接参与 os.path.join）。"""

    plugin_dir: str
    """插件目录（本地 plugin.py / tool.py 所在）。"""

    group_root: str
    """组根（插件目录直接父目录，如 system/、pipeline/input/）。只返回不注入。"""

    shared_root: str
    """plugins/shared 根（http_json / tenant_data 等共享裸模块所在）。"""


def _find_shared_root(plugin_dir: str) -> str:
    """自插件目录的父级向上找首个同时含 system/tools/pipeline 子目录的祖先。

    从父级起扫（不含插件目录自身）：插件自带同名子目录布局时不误判；顶级
    插件（plugins/shared/db_admin 等）的父级即共享根，同样命中。指纹缺失
    （非 0.2 布局的目录树）回退插件目录上三级——标准
    ``{system,tools,pipeline}/<插件>`` 深度。
    """
    current = os.path.dirname(os.path.abspath(plugin_dir))
    while True:
        if all(os.path.isdir(os.path.join(current, d)) for d in _LAYOUT_FINGERPRINT):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return os.path.abspath(os.path.join(plugin_dir, "..", "..", ".."))
        current = parent


def _promote(path: str) -> None:
    """把 path 移到 sys.path[0]（先移除全部既有出现再置前）。

    「在场但不在前」不满足解析优先：车道共跑进程里兄弟插件目录可能已把
    同名裸模块目录压到更前位，只判在场会让 ``from plugin import`` 解析到
    他人实现——本函数每次引导重申本插件目录的解析优先权（等价于收敛前
    手写样板的无条件 ``insert(0)``，且不产生重复条目）。
    """
    while path in sys.path:
        sys.path.remove(path)
    sys.path.insert(0, path)


def bootstrap_plugin(
    entry_file: str | os.PathLike[str],
    extra: Sequence[str | os.PathLike[str]] = (),
) -> Bootstrap:
    """插件 server.py 引导：插件目录与共享根注入 sys.path 并置于最前。

    两种运行语境均成立：

    - **sidecar**（直接 ``python server.py`` / 内核 respawn 同语境）：脚本目录
      虽由解释器自动入列，重复注入去重无害；
    - **合宿平铺**（_host/host.py 以唯一模块名 exec 成员 server.py，该路径
      不自动入列脚本目录）：宿主已先把成员目录插到 sys.path 最前，本函数
      置前语义与之一致。

    注入顺序先插件目录后共享根，终态 ``[shared_root, plugin_dir, ...]``——
    复刻 0.2 pipeline 插件族既有解析序（收敛前手写样板为无条件 ``insert(0)``，
    长驻进程中共跑方挤前时同样重申优先；裸名冲突审计：共享根 7 裸模块与全部
    组根/插件目录零交集，见 ADR 背景）。

    Args:
        entry_file: server.py 的 ``__file__``（str 或 Path 均可）。
        extra: 需要额外注入的共享根相对子路径（组根或包目录，如任务域的
            ``"system"`` 与 ``"system/tasks"``）。存在性校验（非目录跳过）；
            按序置前，终态序与调用序相反（与既有手写注入块逐条 insert 的
            语义一致）。
    """
    plugin_dir = os.path.dirname(os.path.abspath(entry_file))
    shared_root = _find_shared_root(plugin_dir)
    _promote(plugin_dir)
    _promote(shared_root)
    for rel in extra:
        extra_path = os.path.normpath(os.path.join(shared_root, os.fspath(rel)))
        if os.path.isdir(extra_path):
            _promote(extra_path)
    return Bootstrap(
        plugin_dir=plugin_dir,
        group_root=os.path.dirname(plugin_dir),
        shared_root=shared_root,
    )
