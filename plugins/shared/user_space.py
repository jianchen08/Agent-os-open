"""用户空间根 —— 用户可写资产的统一落点（Rust 侧 ``kernel/crates/core/src/user_space.rs`` 的镜像）。

用户可写的东西（插件 / 配置 / 数据 / 密钥）全部住在一个根下面，使之整体位于
仓库**之外**：仓内 ``config/``（git 跟踪）与 ``data/`` 处于工作区还原的抹除风险
面内，而用户空间不受影响。目录布局::

    <USER_ROOT>/                  # 默认按 OS 不同（见 user_root）
    ├── plugins/                  # 用户插件根（覆盖内置根：同 id 用户赢）
    ├── config/                   # 用户配置层（镜像 factory config/ 相对路径）
    ├── data/                     # 运行时数据（多租户树 / uploads / DB）
    └── .env                      # 密钥与环境变量

覆盖语义：**文件级整体替换**——用户层存在某文件时，factory 同路径文件不被读取、
不被合并（见 ``docs/decisions/2026-09-13-unified-user-root.md``）。这是文件级所有权
转移（与插件双根「同 id 用户赢」同构），不是被 ADR 2026-09-02 否决的
「出厂默认 + 用户覆盖」字段级两层。

**与 Rust 侧同规则**：本模块与 ``kernel/crates/core/src/user_space.rs`` 实现同一套
解析（两侧注释互为引用，共享真值源契约），与 ``kernel_db.py`` ↔ ``storage_factory.rs``
的先例同构。任何一侧改解析规则，另一侧必须同步。

不依赖第三方库：插件 sidecar 的 venv 不保证有 ``platformdirs``（它只是 dev 传递
依赖），故 OS 目录推导用标准库手写，与 ``dirs::data_dir()`` 的语义逐一对齐。
"""

from __future__ import annotations

import os
import sys
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


def _os_data_dir() -> Path | None:
    """OS 标准「用户数据目录」——对齐 ``dirs::data_dir()``。

    - Windows：``%APPDATA%``（Roaming；**非** ``%LOCALAPPDATA%``——配置与密钥
      应随用户漫游，与 Rust 侧同裁定）
    - macOS：``~/Library/Application Support``
    - 其他（Linux/BSD）：``$XDG_DATA_HOME``，未设时 ``~/.local/share``
    """
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) if appdata else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and xdg.strip():
        return Path(xdg.strip())
    return Path.home() / ".local" / "share"


def user_root() -> Path | None:
    """用户空间根：``AGENTOS_USER_ROOT`` > OS 标准目录下的 ``agentos/``。

    OS 目录不可得（极端环境）时返回 ``None``——调用方各自回退既有行为，
    与 Rust 侧返回 ``Option<PathBuf>`` 同形。
    """
    env = _env_path(USER_ROOT_ENV)
    if env is not None:
        return env
    base = _os_data_dir()
    return base / "agentos" if base is not None else None


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
