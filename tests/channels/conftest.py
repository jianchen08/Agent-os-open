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
    "api": _SYSTEM_DIR / "channel_api",
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
    # workspace：跨目录同名（tasks/workspace.py、isolation/workspace.py 模块
    # vs system/workspace/ 包）——逐出确保重新解析到 system/workspace/ 包
    "workspace",
    "workspace_service",
    # channel_api 内部模块（与其它通道同目录平铺导入时可能冲突）
    "deps",
    "models",
    "server",
    "routes_missing",
    "routes_tasks",
    "routes_workspaces",
    "routes_reviews",
    "routes_artifacts",
    "routes_config",
    "routes_scene",
    "routes_ui",
    "routes_asr",
    "routes_evaluation",
    "routes_thinking_mode",
    "memory_store",
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
    # channel_api 的路由模块按 namespace package 访问兄弟系统插件
    #（workspace/tasks/multimodal 等），需把 system/ 也加入 path。
    if channel == "api":
        _s = str(_SYSTEM_DIR)
        if _s not in sys.path:
            sys.path.insert(0, _s)
        _register_channels_api_compat()
    for m in _AMBIGUOUS_MODULES:
        sys.modules.pop(m, None)
    # 移除含同名 workspace.py 的平铺目录（pytest 收集其它插件测试（tasks 等）时
    # 会把其目录插入 sys.path）：裸 `import workspace` 会命中 tasks/workspace.py
    # 模块而压过 system/workspace/ 包（PathFinder 模块优先于 namespace 包）。
    # 此处移除保证 channel_api 路由的 `from workspace.workspace_service import`
    # 解析到 0.2 真相源 system/workspace/ 包。
    for _conflict in (_SYSTEM_DIR / "tasks", _SYSTEM_DIR / "isolation"):
        _s = str(_conflict)
        if _s in sys.path:
            sys.path.remove(_s)


def _register_channels_api_compat() -> None:
    """0.1 `channels.api.*` 命名空间兼容：把 channel_api 目录挂为 `channels.api` 包。

    0.2 迁移后通道插件平铺在 plugins/shared/system/channel_api/（`from routes_config
    import ...`），但部分测试仍按 0.1 命名空间 import（`from channels.api.models
    import ...`）。注册命名空间包后，`channels.api.X` 自动解析到 channel_api/X.py，
    与平铺 import 并存。
    """
    import types  # noqa: PLC0415

    channels_ns = sys.modules.setdefault("channels", types.ModuleType("channels"))
    channels_ns.__path__ = []  # namespace package
    api_ns = sys.modules.setdefault("channels.api", types.ModuleType("channels.api"))
    api_ns.__path__ = [str(_CHANNEL_DIRS["api"])]


# 这几个 channels.api 路由测试依赖的路由模块（routes_missing/routes_workspaces/
# routes_search）内部仍含 0.1 式懒加载 import（from human_interaction / workspace /
# infrastructure 等），属 Phase 1d 待修源码。修好前跳过收集，避免阻塞 channels 套件。
collect_ignore_glob = []  # routes 测试已修复，不再跳过
