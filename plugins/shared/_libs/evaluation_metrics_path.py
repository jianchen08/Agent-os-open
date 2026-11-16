"""评估指标配置坐标探测——跨插件单源（task_evaluate / task_submit 共用）。

历史为两插件各自的对偶复制（docstring 互认「改解析算法须两边同步」的
同步义务），复制必漂移；抽取本模块后同步义务随复制件消亡，改算法只改这里。

算法契约：祖先逐级探测——从调用方插件目录逐级向上（含起点，至盘根），
首个含 ``config/plugins/evaluation/evaluation_metrics.yaml`` 的祖先即根。
三布局单算法通吃：
- 仓库布局：plugins/shared/<插件> → 仓库根；
- 装机树布局：resources/plugins/shared/<插件> → resources；
- 用户空间平铺布局：plugins/<插件> → agentos 根（固定级数向上拼会落到
  %APPDATA% 外的幽灵路径，2026-09-29 装机实证致指标全量诚实失败）。
全 miss 回落历史行为：探测起点向上 3 级拼相对尾（调用方总拿到路径字符串，
路径可能不存在，调用方自有降级）。
"""

from __future__ import annotations

import os

_PROBE_TAIL = os.path.join("config", "plugins", "evaluation", "evaluation_metrics.yaml")


def probe_metrics_config_path(plugin_dir: str) -> str:
    """从插件目录逐级向上探测 evaluation_metrics.yaml，返回命中路径字符串。

    Args:
        plugin_dir: 调用方插件模块目录（探测起点，取 ``__file__`` 的 dirname；
            经调用方传入而非本模块自取——探测锚是插件位置，不是本共享件位置）。

    Returns:
        首个含该 yaml 的祖先拼接路径；全 miss 回落 ``plugin_dir`` 向上 3 级
        拼相对尾（历史行为）。
    """
    here = os.path.abspath(plugin_dir)
    current = here
    while True:
        candidate = os.path.join(current, _PROBE_TAIL)
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:  # 逐级到盘根仍 miss
            break
        current = parent
    root = os.path.abspath(os.path.join(here, "..", "..", ".."))
    return os.path.join(root, _PROBE_TAIL)
