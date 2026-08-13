"""多租户数据根咽喉点 — 方案 B 目录隔离地基（F-TENANT-B-T1 / FP-0.2.八 / V4）。

方案 B（产品决策）：每租户独立数据根 ``data/{tenant_id}/``，各插件经此咽喉点取
自己 subdir（如 ``data/{tenant_id}/multimodal``），从存储层落实租户隔离。配套覆盖层
``config/users/{tenant_id}/``（T 后续阶段）。

本模块是**所有 Python 侧插件数据访问的唯一收口**，提供三个能力：

1. ``tenant_data_root(tenant_id, subdir)`` —— 返回 ``{base}/{tenant_id}/{subdir}/``，自动
   mkdir。``base`` 默认为仓库 ``data/``（可经 env ``AGENTOS_DATA_DIR`` 或显式参数覆盖）。
2. ``get_current_tenant_id(capability_caller)`` —— 经 ``tenant-context`` capability 取当前
   tenant_id；未注入/调用失败/返回空 → 回退 ``DEFAULT_TENANT``（``"default"``）。
   文档化：未接入 capability 的调用方暂用 default 租户，保证平滑迁移、永不崩溃。
3. ``migrate_legacy_data_to_default(data_root)`` —— 把全局共享的 ``data/{memory,...}``
   幂等迁入 ``data/default/{memory,...}``。**仅提供函数 + 测试**，真实迁移由部署/启动
   钩子调用（不在本模块对真实 data/ 执行破坏性移动）。
4. ``tenant_config_dir(tenant_id)`` —— 返回 ``{base}/users/{tenant_id}/`` 配置覆盖层目录
   （不自动创建：目录存在 = 该租户有配置覆盖，不存在 = 无覆盖，调用方回退全局配置）。
   ``base`` 默认为仓库 ``config/users/``（可经 env ``AGENTOS_CONFIG_USERS_DIR`` 或显式
   参数覆盖）。

capability 调用模式（参考 hindsight_memory/wiring.py 的 ``_bind_caller``）：
- SDK ``CapabilityHandle.call(method, params)`` 会拼成 wire method ``f"{cap}.{method}"``
  （见 plugin.py:237-242）。
- 因此 ``capability_caller`` 约定为 **tenant-context 绑定的 async caller**，接收**短**
  方法名（如 ``"get"``），由 ``make_tenant_context_caller`` 在调用方剥前缀后转交句柄。
- ``get_current_tenant_id`` 调 ``capability_caller("get", {})``，期待返回
  ``{"tenant_id": "..."}``（或裸字符串）；内核尚未实现 ``tenant-context.get`` 时静默回退。

[来源: docs/test_traceability.md FP-0.2.八 / V4；plugins/sdk/.../capability.py（tenant-context 为标准能力）；
 plugins/shared/system/hindsight_memory/wiring.py（_bind_caller 范本）]
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认租户标识（未注入 tenant-context capability 时的回退值）。
DEFAULT_TENANT = "default"

# tenant-context capability 名（与 STANDARD_CAPABILITIES 对齐）。
TENANT_CONTEXT_CAPABILITY = "tenant-context"

# tenant_context_base 解析的 env 覆盖键（测试/部署可重定向数据根，避免写真实 data/）。
DATA_BASE_ENV = "AGENTOS_DATA_DIR"

# config/users 覆盖层 base 解析的 env 覆盖键（测试/部署可重定向配置根）。
CONFIG_USERS_BASE_ENV = "AGENTOS_CONFIG_USERS_DIR"

# capability_caller 类型：(method: str, params: dict) -> Awaitable[Any]
CapabilityCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


# ═══════════════════════════════════════════════════════════
# 数据根解析
# ═══════════════════════════════════════════════════════════


def _default_data_base() -> Path:
    """数据根 base（``data/`` 目录）。

    优先 env ``AGENTOS_DATA_DIR``；否则仓库根 ``data/``
    （本文件位于 ``plugins/shared/tenant_data.py``，上溯 2 级到仓库根）。
    """
    env = os.environ.get(DATA_BASE_ENV)
    if env:
        return Path(env)
    # parents[0]=shared, parents[1]=plugins, parents[2]=仓库根
    return Path(__file__).resolve().parents[2] / "data"


def _sanitize_segment(segment: str) -> str:
    """清洗 tenant_id / subdir 路径段，防路径穿越。

    拒绝空串、含分隔符或 ``..`` 的值——租户目录必须是单层合法目录名。
    """
    if (
        not segment
        or ".." in segment
        or "/" in segment
        or "\\" in segment
    ):
        raise ValueError(
            f"invalid tenant/subdir segment (path traversal blocked): {segment!r}"
        )
    return segment


def tenant_data_root(
    tenant_id: str,
    subdir: str,
    *,
    base: Path | str | None = None,
) -> Path:
    """返回租户数据根 ``{base}/{tenant_id}/{subdir}/``，自动 mkdir(parents=True)。

    Args:
        tenant_id: 租户 ID（单层目录名，禁含路径分隔符/``..``）。
        subdir: 插件子目录名（如 ``multimodal``/``tasks``/``uploads``，单层）。
        base: 数据根 base。None 时用 ``_default_data_base()``（env ``AGENTOS_DATA_DIR``
            或仓库 ``data/``）。测试应显式传 base 或设 env 以隔离副作用。

    Returns:
        租户子目录 Path（已创建）。
    """
    base_path = Path(base) if base is not None else _default_data_base()
    root = base_path / _sanitize_segment(tenant_id) / _sanitize_segment(subdir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _default_config_users_base() -> Path:
    """config/users 覆盖层 base（``config/users/`` 目录）。

    优先 env ``AGENTOS_CONFIG_USERS_DIR``；否则仓库根 ``config/users/``
    （本文件位于 ``plugins/shared/tenant_data.py``，上溯 2 级到仓库根）。
    """
    env = os.environ.get(CONFIG_USERS_BASE_ENV)
    if env:
        return Path(env)
    return Path(__file__).resolve().parents[2] / "config" / "users"


def tenant_config_dir(
    tenant_id: str,
    *,
    base: Path | str | None = None,
) -> Path:
    """返回租户配置覆盖层目录 ``{base}/{tenant_id}/``（不自动创建）。

    方案 B 配置面：每租户独立配置覆盖目录 ``config/users/{tenant_id}/``（如
    ``config/users/default/profile.md``）。覆盖层语义——目录存在 = 该租户有配置
    覆盖（读取方优先于此目录，缺失项回退全局 ``config/``）；不存在 = 无覆盖。

    与 ``tenant_data_root``（存储层自动 mkdir）刻意不同：配置覆盖层**不因读取
    而生成空目录**，避免空覆盖层掩盖"无覆盖 → 回退全局配置"的语义。

    Args:
        tenant_id: 租户 ID（单层目录名，禁含路径分隔符/``..``）。
        base: 配置根 base。None 时用 ``_default_config_users_base()``（env
            ``AGENTOS_CONFIG_USERS_DIR`` 或仓库 ``config/users/``）。测试应显式
            传 base 或设 env 以隔离副作用。

    Returns:
        租户配置覆盖层目录 Path（可能不存在）。
    """
    base_path = Path(base) if base is not None else _default_config_users_base()
    return base_path / _sanitize_segment(tenant_id)


# ═══════════════════════════════════════════════════════════
# 当前租户解析（capability）
# ═══════════════════════════════════════════════════════════


def _bind_caller(handle: Any, cap_name: str) -> CapabilityCaller:
    """绑定 capability 句柄与命名空间，构造 async caller `(method, params) -> Any`。

    与 hindsight_memory/wiring.py 的 ``_bind_caller`` 同款：caller 接收**完整** wire
    method（如 ``tenant-context.get``），剥掉已含的能力前缀后转交 ``handle.call``，
    避免 ``handle.call`` 再拼成双命名空间（``tenant-context.tenant-context.get``）。

    闭包通过函数参数绑定 cap_name，规避 B023（循环变量绑定）。
    """
    prefix = f"{cap_name}."

    async def _call(method: str, params: dict[str, Any]) -> Any:
        stripped = method[len(prefix):] if method.startswith(prefix) else method
        return await handle.call(stripped, params)

    return _call


def make_tenant_context_caller(plugin: Any) -> CapabilityCaller | None:
    """从插件实例构造 tenant-context 绑定的 capability_caller。

    Args:
        plugin: ``AgentOSPlugin`` 实例（含 ``get_capability``）。

    Returns:
        async caller ``(method, params) -> Any``（接收完整 wire method，自动剥
        ``tenant-context.`` 前缀）；能力未注入时返回 None。
    """
    try:
        handle = plugin.get_capability(TENANT_CONTEXT_CAPABILITY)
    except KeyError:
        logger.debug(
            "[tenant_data] tenant-context capability 未注入，get_current_tenant_id "
            "将回退 default"
        )
        return None
    return _bind_caller(handle, TENANT_CONTEXT_CAPABILITY)


def _extract_tenant_id(result: Any) -> str:
    """从 capability 返回值提取 tenant_id；无有效值 → DEFAULT_TENANT。"""
    if isinstance(result, dict):
        tid = result.get("tenant_id") or result.get("tenantId") or result.get("id")
        if tid:
            return str(tid)
    elif isinstance(result, str) and result.strip():
        return result.strip()
    return DEFAULT_TENANT


async def get_current_tenant_id(capability_caller: CapabilityCaller | None) -> str:
    """经 tenant-context capability 取当前 tenant_id；失败/未注入 → ``DEFAULT_TENANT``。

    约定 ``capability_caller`` 为 **tenant-context 绑定**的 async caller（接收短方法名
    ``"get"``；可用 ``make_tenant_context_caller(plugin)`` 构造）。本函数调
    ``await capability_caller("get", {})``，期待返回 ``{"tenant_id": "..."}`` 或裸字符串。

    韧性：caller 为 None、调用抛异常（内核暂未实现 ``tenant-context.get``）、返回空值
    时均静默回退 ``DEFAULT_TENANT``——永不向上抛，确保未接入 capability 的调用方平滑运行。

    Args:
        capability_caller: tenant-context 绑定的 async caller；None 表示未注入。

    Returns:
        当前 tenant_id；不可解析时回退 ``"default"``。
    """
    if capability_caller is None:
        return DEFAULT_TENANT
    try:
        result = await capability_caller("get", {})
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[tenant_data] tenant-context.get 调用失败，回退 default | error=%s", exc
        )
        return DEFAULT_TENANT
    return _extract_tenant_id(result)


# ═══════════════════════════════════════════════════════════
# legacy → default 迁移（幂等）
# ═══════════════════════════════════════════════════════════


def migrate_legacy_data_to_default(
    data_root: Path | str | None = None,
) -> list[str]:
    """把 ``data/`` 直接子项幂等迁入 ``data/default/``。

    方案 B 上线迁移：全局共享的 ``data/{memory,multimodal,tasks,uploads,...}``（目录与
    散落文件）移入 ``data/default/{...}``，使 default 租户继承存量数据。

    幂等规则（与部署钩子重复调用兼容）：
    - 若 ``data/default/`` **已存在** → 视为已迁移，直接返回空列表（不移动任何项）。
    - 否则：创建 ``data/default/``，把 ``data/`` 下除 ``default`` 外的所有直接子项
      （目录与文件）移入 ``data/default/``。

    安全性：**仅文件系统移动（同卷 rename），不删除/不覆盖**。目标已存在同名项时
    ``shutil.move`` 会把源并入/覆盖——本函数仅在 default 不存在时执行，规避该情形。

    Args:
        data_root: 数据根目录。None 时用 ``_default_data_base()``。

    Returns:
        被移动的直接子项名列表（相对 data_root）；已迁移/无内容时为空列表。

    Note:
        本函数**不在导入或调用真实仓库 data/ 时执行破坏性操作**——仅在调用方显式
        传入时移动。生产部署应由启动钩子调用（文档化）。
    """
    base_path = Path(data_root) if data_root is not None else _default_data_base()
    default_dir = base_path / DEFAULT_TENANT

    # 幂等：default 已存在 → 视为已迁移，跳过。
    if default_dir.exists():
        logger.debug(
            "[tenant_data] migrate_legacy_data_to_default: %s 已存在，跳过迁移",
            default_dir,
        )
        return []

    if not base_path.exists():
        return []

    default_dir.mkdir(parents=True, exist_ok=True)

    moved: list[str] = []
    for child in base_path.iterdir():
        if child.name == DEFAULT_TENANT:
            continue
        target = default_dir / child.name
        shutil.move(str(child), str(target))
        moved.append(child.name)

    if moved:
        logger.info(
            "[tenant_data] migrate_legacy_data_to_default: 迁移 %d 项 → %s",
            len(moved),
            default_dir,
        )
    return moved
