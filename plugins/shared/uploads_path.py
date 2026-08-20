"""上传目录解析——多模态附件索引（/uploads/{filename}）→ 文件系统路径。

三方对齐的单一解析点（ADR 2026-08-21）：
- channel_api artifacts 上传落盘：``tenant_data_root(tenant, "uploads")``
  （``UPLOADS_DIR`` 环境变量覆盖，最高优先级）；
- 内核 ``/uploads/{filename}`` 静态服务：``data/default/uploads``；
- multimodal_preprocessor / llm_core 引用解析：本模块。

修复前 preprocessor 用 ``./data/uploads`` 默认值（相对 CWD），与实际上传落盘
目录不一致——附件文件存在却解析失败（warning "文件不存在" 后静默跳过）。

安全：``/uploads/`` 引用只取 basename 拼接（天然拒绝 ``..`` 目录穿越）；
绝对路径引用不由本模块处理（调用方自担存在性检查）。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 多租户数据根咽喉点（plugins/shared/tenant_data.py）。本文件位于
# plugins/shared/uploads_path.py，与 tenant_data.py 同级，直接上溯 0 级。
# 参考 multimodal/storage.py 的 sys.path 自举模式。
_SHARED_ROOT = os.path.dirname(os.path.abspath(__file__))
if _SHARED_ROOT not in sys.path:
    sys.path.insert(0, _SHARED_ROOT)

from tenant_data import DEFAULT_TENANT, tenant_data_root  # noqa: E402

#: /uploads/ 引用前缀（内核静态路由同款，api/src/routes.rs serve_upload_handler）
UPLOADS_URL_PREFIX = "/uploads/"


def resolve_uploads_dir(tenant_id: str | None = None) -> Path:
    """解析上传目录绝对路径。

    优先级（与 channel_api routes_artifacts._get_uploads_dir 对齐）：

    1. 环境变量 ``UPLOADS_DIR``（兼容存量部署覆盖，最高优先级）；
    2. 多租户数据根 ``tenant_data_root(tenant_id or default, "uploads")``
       （方案 B 目录隔离默认值，即 ``data/{tenant_id}/uploads``）。

    Args:
        tenant_id: 租户 ID。None 则用 ``DEFAULT_TENANT``（当前单租户部署形态，
            与内核静态服务硬编码的 data/default/uploads 一致）。

    Returns:
        上传目录路径（不保证存在——读取方自行判存在并降级）。
    """
    env_dir = os.environ.get("UPLOADS_DIR")
    if env_dir:
        return Path(env_dir)
    return Path(str(tenant_data_root(tenant_id or DEFAULT_TENANT, "uploads")))


def resolve_uploads_url(url: str, tenant_id: str | None = None) -> Path | None:
    """把 ``/uploads/{filename}`` 引用解析为上传目录内的绝对路径。

    只取 basename 与上传目录拼接：``/uploads/../secret.png`` 的 basename 是
    ``secret.png``，仍落在上传目录**内**——路径穿越在形态上即被拒绝。

    Args:
        url: 附件引用 URL（如消息 content 内嵌的 ``/uploads/abc.png``）。
        tenant_id: 租户 ID（透传 :func:`resolve_uploads_dir`）。

    Returns:
        上传目录内绝对路径；非 ``/uploads/`` 形态返回 None（调用方按
        http URL / 绝对路径等其它引用形态自行处理）。
    """
    if not url.startswith(UPLOADS_URL_PREFIX):
        return None
    filename = os.path.basename(url)
    if not filename or filename in {".", ".."}:
        return None
    return resolve_uploads_dir(tenant_id) / filename
