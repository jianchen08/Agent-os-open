"""用户空间根 —— 用户可写资产的统一落点（Rust 侧 ``kernel/crates/core/src/user_space.rs`` 的镜像）。

用户可写的东西（插件 / 配置 / 数据 / 密钥）全部住在一个根下面，使之整体位于
仓库**之外**：仓内 ``config/``（git 跟踪）与 ``data/`` 处于工作区还原的抹除风险
面内，而用户空间不受影响。目录布局::

    <USER_ROOT>/                  # AGENTOS_USER_ROOT 显式交付（零回落，见 user_root）
    ├── plugins/                  # 用户插件根（双源 BuiltinFirst 版本裁决：semver 严格新版才接管）
    ├── config/                   # 用户配置层（镜像 factory config/ 相对路径）
    ├── data/                     # 运行时数据（多租户树 / uploads / DB）
    └── .env                      # 密钥与环境变量

覆盖语义：**文件级整体替换**——用户层存在某文件时，factory 同路径文件不被读取、
不被合并（见 ``docs/decisions/2026-09-13-unified-user-root.md``）。这是文件级所有权
转移（插件双源另有 BuiltinFirst 版本裁决，ADR 2026-09-20），不是被 ADR 2026-09-02 否决的
「出厂默认 + 用户覆盖」字段级两层。

**与 Rust 侧同规则**：本模块与 ``kernel/crates/core/src/user_space.rs`` 实现同一套
解析（两侧注释互为引用，共享真值源契约）。任何一侧改解析规则，另一侧必须同步。

**Python 侧对齐副本登记**（多端平台差异普查 ⚠️-3，改动须与本模块同刀）：
- ``plugins/sdk/src/agentos_plugin_sdk/diagnostics_settings.py::user_root``
  （SDK 包边界，不可 import 本模块）
- ``plugins/shared/acp_quota_pool/scripts/acp_ledger.py``（播种技能载荷，
  播种副本对共享根裸模块不可达）
两副本与本体同输入同输出由对齐断言测试钉死
（``tests/plugins/shared/test_user_space.py`` /
``acp_quota_pool/tests/test_acp_ledger.py`` 各 ``*CopyAlignment`` 组）。

不依赖第三方库：插件 sidecar 的 venv 不保证有 ``platformdirs``（它只是 dev 传递
依赖），故副本内的 OS 目录推导用标准库手写（本体已零回落，OS 推导仅副本保留，
见 ``diagnostics_settings`` 已裁定微差）。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "user_root",
    "user_config_dir",
    "user_data_dir",
    "user_plugins_dir",
    "USER_ROOT_ENV",
    "USER_CONFIG_DIR_ENV",
    "USER_DATA_DIR_ENV",
    "USER_PLUGINS_DIR_ENV",
]

USER_ROOT_ENV = "AGENTOS_USER_ROOT"
USER_CONFIG_DIR_ENV = "AGENTOS_USER_CONFIG_DIR"
USER_DATA_DIR_ENV = "AGENTOS_DATA_DIR"
USER_PLUGINS_DIR_ENV = "AGENTOS_USER_PLUGINS_DIR"


def _env_path(name: str) -> Path | None:
    """读环境变量为路径；未设或空白视为未设（对齐 Rust 的 trim().is_empty()）。"""
    raw = os.environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return Path(stripped)


def user_root() -> Path | None:
    """用户空间根：``AGENTOS_USER_ROOT`` 显式生效；未设（含空白值）= ``None``。

    **零回落**（ADR 2026-11-07-plugin-tiered-single-copy-model §2.2，与 Rust 侧
    ``user_space::user_root`` 同规则）：用户根一律来自实例启动配置的交付链，
    不做 OS 标准目录推导——缺省回落 ``%APPDATA%\\agentos``（= 装机用户空间）
    会让未交付用户根的 dev 内核 sidecar 读到装机用户层的陈旧副本
    （2026-11-18 实证：agent_config_load 经旧回落被装机 executor 影子接管
    工具面；Rust 侧同名事故机制根已先修，本侧对齐）。
    """
    return _env_path(USER_ROOT_ENV)


def user_config_dir() -> Path | None:
    """用户配置层根：``AGENTOS_USER_CONFIG_DIR`` > ``<USER_ROOT>/config``。"""
    env = _env_path(USER_CONFIG_DIR_ENV)
    if env is not None:
        return env
    root = user_root()
    return root / "config" if root is not None else None


def user_data_dir() -> Path | None:
    """用户数据根：``AGENTOS_DATA_DIR`` > ``<USER_ROOT>/data``。

    多租户树（``{root}/{tenant}/…``）、uploads、DB 均落于此。
    """
    env = _env_path(USER_DATA_DIR_ENV)
    if env is not None:
        return env
    root = user_root()
    return root / "data" if root is not None else None


def user_plugins_dir() -> Path | None:
    """用户插件根：``AGENTOS_USER_PLUGINS_DIR`` > ``<USER_ROOT>/plugins``。"""
    env = _env_path(USER_PLUGINS_DIR_ENV)
    if env is not None:
        return env
    root = user_root()
    return root / "plugins" if root is not None else None
