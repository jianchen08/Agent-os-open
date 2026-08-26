"""channels 测试公共配置。

0.2 架构下通道插件位于 plugins/shared/system/channel_*，每个插件的 server.py
把自身目录加入 sys.path 以支持平铺 import（如 `from adapter import ...`）。

注意：多个通道插件各自都有 adapter.py / stream_client.py 等同名模块，
若把多个通道目录同时加入 sys.path 会产生同名歧义（先入者胜），且
sys.modules 会缓存第一次导入的模块。因此各测试文件需调用 ``use_channel``
把它依赖的通道目录置于 sys.path 最前，并清理可能冲突的已缓存同名模块。
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SYSTEM_DIR = _REPO_ROOT / "plugins" / "shared" / "system"

# 通道插件名 → 目录
_CHANNEL_DIRS: dict[str, Path] = {
    "feishu": _SYSTEM_DIR / "channel_feishu",
    "gateway": _SYSTEM_DIR / "channel_gateway",
    "dingtalk": _SYSTEM_DIR / "channel_dingtalk",
    "qq": _SYSTEM_DIR / "channel_qq",
    "wecom": _SYSTEM_DIR / "channel_wecom",
    "cli": _SYSTEM_DIR / "channel_cli",
}

# 渠道共享包（C1 合流 2026-08-20）：input_adapter/output_adapter/base_combo_adapter
# 的单一事实源。镜像 server.py 的接线纪律：sys.path.**append**（绝不 insert(0)，
# 避免遮蔽其他插件同名模块）。
_CHANNEL_COMMON_DIR = _SYSTEM_DIR / "channel_common"

# 各通道目录下可能发生跨通道同名冲突的模块名（按需维护）。
# 注：input_adapter/output_adapter/base_combo_adapter/pipeline_types 四名
# 自 C1 合流后渠道目录内已无本地拷贝（共享包 channel_common / SDK 单一事实源），
# 逐出后重新解析的目标即共享源，逐出逻辑保持不变。
_AMBIGUOUS_MODULES = {
    "adapter",
    "base_combo_adapter",
    "input_adapter",
    "output_adapter",
    "pipeline_types",
    "stream_client",
    "card_builder",
    "channel_gateway",
    "message_normalizer",
    "session_bridge",
    "unified_types",
    # helpers：wecom/qq 两通道目录各有 helpers.py，跨通道先入缓存会互相命中
    "helpers",
    # workspace：跨目录同名（tasks/workspace.py、isolation/workspace.py 模块
    # vs system/workspace/ 包）——逐出确保重新解析到 system/workspace/ 包
    "workspace",
    "workspace_service",
}


def use_channel(channel: str) -> None:
    """把指定通道目录置于 sys.path 最前，并清理已缓存的同名冲突模块。

    在测试文件**模块级**、所有 `from xxx import ...` 之前调用一次::

        from tests.channels.conftest import use_channel
        use_channel("feishu")
        from adapter import FeishuAdapter  # 现在解析到 channel_feishu/adapter.py
    """
    d = str(_CHANNEL_DIRS[channel])
    if not sys.path or sys.path[0] != d:
        sys.path.insert(0, d)
    # 渠道共享包 channel_common：append 注入（与 server.py 接线同款纪律），
    # 供各渠道 adapter 的 `from input_adapter/output_adapter/base_combo_adapter import`
    # 平铺解析到共享单一事实源。
    _cc = str(_CHANNEL_COMMON_DIR)
    if _cc not in sys.path:
        sys.path.append(_cc)
    for m in _AMBIGUOUS_MODULES:
        sys.modules.pop(m, None)
    # 移除含同名 workspace.py 的平铺目录（pytest 收集其它插件测试（tasks 等）时
    # 会把其目录插入 sys.path）：裸 `import workspace` 会命中 tasks/workspace.py
    # 模块而压过 system/workspace/ 包（PathFinder 模块优先于 namespace 包）。
    # 此处移除保证通道测试的 `from workspace.workspace_service import`
    # 解析到 0.2 真相源 system/workspace/ 包。
    for _conflict in (_SYSTEM_DIR / "tasks", _SYSTEM_DIR / "isolation"):
        _s = str(_conflict)
        if _s in sys.path:
            sys.path.remove(_s)


# channel_api 于 2026-08-21 整体退役；channels.api 命名空间兼容注册
# 与 routes_* 懒加载跳过清单（collect_ignore_glob）随其删除。
collect_ignore_glob = []
