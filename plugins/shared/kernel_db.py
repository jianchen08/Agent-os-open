"""内核 SQLite 库路径解析 —— 插件侧直读账本表的共享真值源。

0.2 业务账本由内核落 SQLite（kernel/crates/engine/src/storage_factory.rs），
少数插件读面（monitoring traces 聚合 / cost_control 用量统计）对同库做 SQL
聚合，库路径必须与内核实际开库路径同源，否则统计静默读空。解析序与内核
storage_factory.rs 的装载序一致：``AGENTOS_DB_PATH`` 环境变量优先（内核
storage.yaml 的 sqlite.path 缺省即项目根 ``agentos_kernel.db``，环境变量
覆盖一切），未设置时按目录指纹（``config/`` + ``config/kernel/``）向上探测
项目根后取 ``agentos_kernel.db``。**相对路径 env 与内核同规则锚定项目根**
解析（并绝对化）——内核 sidecar 与插件进程 CWD 不同也指向同一物理库；
绝对路径与 ``:memory:`` 别名原样保留。

导入形态与 ``http_json`` 先例一致：插件侧把 ``plugins/shared`` 推上 sys.path
后裸名导入（如 ``from kernel_db import kernel_db_path as _kernel_db_path``）。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["kernel_db_path"]

_DB_FILENAME = "agentos_kernel.db"


def _project_root() -> Path:
    """向上查找项目根（含 ``config/`` + ``config/kernel/`` 的目录）。

    按目录特征探测，不硬编码 parent×N（模块相对项目根的深度随布局变化不可靠）。
    """
    here = Path(__file__).resolve().parent
    for candidate in [here, *here.parents]:
        if (candidate / "config").is_dir() and (candidate / "config" / "kernel").is_dir():
            return candidate
    # 兜底：仓库布局内必有 config/ 探测命中（本模块位于 <root>/plugins/shared/）
    return Path(__file__).resolve().parents[2]  # pragma: no cover


def kernel_db_path() -> Path:
    """返回内核 SQLite 库路径：AGENTOS_DB_PATH 优先，项目根库文件兜底。

    env 为相对路径时以项目根为锚解析（与内核 storage_factory 同规则，
    两端 CWD 分叉不致读写不同库）；绝对路径 / ``:memory:`` 原样保留。
    """
    env_path = os.environ.get("AGENTOS_DB_PATH")
    if env_path:
        candidate = Path(env_path)
        if candidate.is_absolute() or env_path == ":memory:":
            return candidate
        return (_project_root() / candidate).resolve()
    return _project_root() / _DB_FILENAME
