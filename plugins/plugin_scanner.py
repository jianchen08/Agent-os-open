"""统一插件扫描器。

负责扫描 plugins/ 目录下的全局插件和租户插件，
按功能分类（system / pipeline / tools）发现插件并返回位置信息。

目录结构约定::

    plugins/
    ├── shared/          # 全局共享插件（所有租户共享）
    │   ├── system/      # 系统服务插件
    │   ├── pipeline/    # 管道插件
    │   │   ├── input/
    │   │   ├── output/
    │   │   └── core/
    │   └── tools/       # 工具插件
    ├── tenants/         # 租户级插件
    │   └── {tenant_id}/
    │       ├── system/
    │       ├── pipeline/
    │       │   ├── input/
    │       │   ├── output/
    │       │   └── core/
    │       └── tools/
    └── sdk/             # 插件开发 SDK（不扫描）

用法::

    from plugins.plugin_scanner import scan_pipeline_plugins, scan_sidecar_plugins

    # 扫描全局管道插件
    locations = scan_pipeline_plugins()

    # 扫描指定租户的管道插件（合并全局 + 租户）
    locations = scan_pipeline_plugins(tenant_id="default")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

# plugins/ 根目录（本文件位于 plugins/plugin_scanner.py）
_PLUGINS_ROOT = Path(__file__).resolve().parent

# 管道插件的权威 Python 包源（src/plugins/shared/）
# 与 _PLUGINS_ROOT 不同：Sidecar 插件在 plugins/shared/，管道插件在 src/plugins/shared/
_PIPELINE_SRC_ROOT = _PLUGINS_ROOT.parent / "src" / "plugins"

# 全局共享目录名（避免使用 Python 关键字 global）
_SHARED_DIR = "shared"


class PluginScope(str, Enum):
    """插件作用域：全局或租户级。"""

    SHARED = "shared"
    TENANT = "tenant"


class PluginCategory(str, Enum):
    """插件功能分类。"""

    SYSTEM = "system"
    PIPELINE_INPUT = "pipeline_input"
    PIPELINE_OUTPUT = "pipeline_output"
    PIPELINE_CORE = "pipeline_core"
    TOOLS = "tools"


# 管道子分类到子目录名的映射
_PIPELINE_SUBDIRS: dict[str, str] = {
    PluginCategory.PIPELINE_INPUT.value: "input",
    PluginCategory.PIPELINE_OUTPUT.value: "output",
    PluginCategory.PIPELINE_CORE.value: "core",
}

# 扫描时整体跳过的非插件目录（不含 tenants，tenants 由扫描函数显式处理）
_SKIP_DIRS = {"sdk", "__pycache__", ".git"}


@dataclass(frozen=True)
class PluginLocation:
    """描述一个插件的物理位置。

    Attributes:
        scope: 全局共享还是租户级
        tenant_id: 租户标识，scope=SHARED 时为 None
        category: 功能分类
        plugin_name: 插件名（目录名）
        path: 插件目录的绝对路径
    """

    scope: PluginScope
    category: PluginCategory
    plugin_name: str
    path: Path
    tenant_id: str | None = None


def _scan_category_dir(
    base_dir: Path,
    category: PluginCategory,
    scope: PluginScope,
    tenant_id: str | None = None,
) -> list[PluginLocation]:
    """扫描某个分类目录下的插件子目录。

    Args:
        base_dir: 分类目录的绝对路径（如 .../shared/system）
        category: 功能分类
        scope: 全局共享还是租户
        tenant_id: 租户标识

    Returns:
        发现的插件位置列表
    """
    if not base_dir.is_dir():
        return []

    locations: list[PluginLocation] = []
    for child in sorted(base_dir.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            continue
        # 必须含 __init__.py 或 plugin.py 或 plugin.json 才算插件目录
        has_marker = (
            (child / "__init__.py").exists()
            or (child / "plugin.py").exists()
            or (child / "plugin.json").exists()
            or (child / "server.py").exists()
        )
        if not has_marker:
            continue

        locations.append(
            PluginLocation(
                scope=scope,
                category=category,
                plugin_name=child.name,
                path=child,
                tenant_id=tenant_id,
            )
        )

    return locations


def _scan_pipeline_subdir(
    pipeline_dir: Path,
    scope: PluginScope,
    tenant_id: str | None = None,
) -> list[PluginLocation]:
    """扫描 pipeline 目录下的 input/output/core 三个子分类。

    Args:
        pipeline_dir: pipeline 目录路径
        scope: 全局共享还是租户
        tenant_id: 租户标识

    Returns:
        发现的插件位置列表
    """
    if not pipeline_dir.is_dir():
        return []

    category_map = {
        "input": PluginCategory.PIPELINE_INPUT,
        "output": PluginCategory.PIPELINE_OUTPUT,
        "core": PluginCategory.PIPELINE_CORE,
    }

    locations: list[PluginLocation] = []
    for subdir_name, category in category_map.items():
        subdir = pipeline_dir / subdir_name
        locations.extend(_scan_category_dir(subdir, category, scope, tenant_id))

    return locations


def _scan_scope_dir(
    scope_dir: Path,
    scope: PluginScope,
    tenant_id: str | None = None,
) -> list[PluginLocation]:
    """扫描某个作用域目录（shared/ 或 tenants/{id}/）下的所有分类。

    Args:
        scope_dir: 作用域根目录
        scope: 全局共享还是租户
        tenant_id: 租户标识

    Returns:
        发现的插件位置列表
    """
    if not scope_dir.is_dir():
        return []

    locations: list[PluginLocation] = []

    # system 分类
    locations.extend(
        _scan_category_dir(scope_dir / "system", PluginCategory.SYSTEM, scope, tenant_id)
    )

    # pipeline 分类（含 input/output/core 子目录）
    locations.extend(_scan_pipeline_subdir(scope_dir / "pipeline", scope, tenant_id))

    # tools 分类
    locations.extend(
        _scan_category_dir(scope_dir / "tools", PluginCategory.TOOLS, scope, tenant_id)
    )

    return locations
def scan_pipeline_plugins(
    tenant_id: str | None = None,
    plugins_root: Path | None = None,
) -> list[PluginLocation]:
    """扫描管道插件（input/output/core）。

    先扫描共享目录，再扫描租户目录（如果指定了 tenant_id 且非 default）。
    租户插件与共享插件同名时，租户插件优先（覆盖语义）。

    Args:
        tenant_id: 租户标识。None 或 "default" 时只扫共享目录。
            "default" 是占位符目录，不包含实际插件。
            指定具体租户时，合并共享 + 该租户的结果。
        plugins_root: 插件根目录，默认为 plugins/

    Returns:
        插件位置列表，租户插件覆盖同名的共享插件
    """
    root = plugins_root or _PLUGINS_ROOT

    # 共享管道插件：搜索两个来源
    locations: list[PluginLocation] = []

    # 1. 权威 Python 包源（src/plugins/shared/{input,output,core}/）
    #    仅在未传入 plugins_root 时搜索（生产场景）；测试场景由 plugins_root 隔离
    if plugins_root is None:
        locations.extend(
            _scan_pipeline_subdir(_PIPELINE_SRC_ROOT / _SHARED_DIR, PluginScope.SHARED)
        )

    # 2. Sidecar 目录中的 pipeline 子目录（plugins/shared/pipeline/ 或 plugins_root/shared/pipeline/）
    sidecar_pipeline = _scan_pipeline_subdir(root / _SHARED_DIR / "pipeline", PluginScope.SHARED)
    locations = _merge_with_override(locations, sidecar_pipeline)

    # 3. 测试场景：从 plugins_root/shared/ 直接搜索（不通过 pipeline 子目录）
    if plugins_root is not None:
        locations.extend(
            _scan_pipeline_subdir(root / _SHARED_DIR, PluginScope.SHARED)
        )

    # 租户管道插件（default 是占位符，跳过）
    if tenant_id and tenant_id != "default":
        tenant_dir = root / "tenants" / tenant_id
        tenant_locations = _scan_pipeline_subdir(
            tenant_dir / "pipeline", PluginScope.TENANT, tenant_id
        )
        locations = _merge_with_override(locations, tenant_locations)

    return locations


def scan_sidecar_plugins(
    tenant_id: str | None = None,
    plugins_root: Path | None = None,
) -> list[PluginLocation]:
    """扫描 Sidecar 插件（system + tools 功能分类）。

    先扫描共享目录，再扫描租户目录（如果指定了 tenant_id 且非 default）。
    租户插件与共享插件同名时，租户插件优先。

    Args:
        tenant_id: 租户标识。None 或 "default" 时只扫共享目录。
            "default" 是占位符目录，不包含实际插件。
        plugins_root: 插件根目录，默认为 plugins/

    Returns:
        插件位置列表
    """
    root = plugins_root or _PLUGINS_ROOT

    # 共享 Sidecar 插件（system + tools）
    locations: list[PluginLocation] = []
    locations.extend(_scan_category_dir(root / _SHARED_DIR / "system", PluginCategory.SYSTEM, PluginScope.SHARED))
    locations.extend(_scan_category_dir(root / _SHARED_DIR / "tools", PluginCategory.TOOLS, PluginScope.SHARED))

    # 租户 Sidecar 插件（default 是占位符，跳过）
    if tenant_id and tenant_id != "default":
        tenant_dir = root / "tenants" / tenant_id
        tenant_locations: list[PluginLocation] = []
        tenant_locations.extend(
            _scan_category_dir(tenant_dir / "system", PluginCategory.SYSTEM, PluginScope.TENANT, tenant_id)
        )
        tenant_locations.extend(
            _scan_category_dir(tenant_dir / "tools", PluginCategory.TOOLS, PluginScope.TENANT, tenant_id)
        )
        locations = _merge_with_override(locations, tenant_locations)

    return locations


def scan_all_plugins(
    tenant_id: str | None = None,
    plugins_root: Path | None = None,
) -> list[PluginLocation]:
    """扫描所有插件（管道 + Sidecar）。

    Args:
        tenant_id: 租户标识
        plugins_root: 插件根目录

    Returns:
        所有插件位置列表
    """
    return scan_pipeline_plugins(tenant_id, plugins_root) + scan_sidecar_plugins(
        tenant_id, plugins_root
    )


def resolve_pipeline_plugin_module(
    plugin_name: str,
    category: str,
    tenant_id: str | None = None,
    plugins_root: Path | None = None,
) -> str | None:
    """解析管道插件的 Python 模块路径。

    按优先级搜索：租户 → 共享 → 旧路径回退。

    Args:
        plugin_name: 插件名（如 "context_build"）
        category: 插件分类（"input" / "output" / "core"）
        tenant_id: 租户标识
        plugins_root: 插件根目录

    Returns:
        模块路径字符串（如 "plugins.shared.pipeline.input.context_build"），
        未找到返回 None
    """
    root = plugins_root or _PLUGINS_ROOT

    # 1. 租户插件
    if tenant_id:
        tenant_path = root / "tenants" / tenant_id / "pipeline" / category / plugin_name
        if tenant_path.is_dir():
            return f"plugins.tenants.{tenant_id}.pipeline.{category}.{plugin_name}"

    # 2. 共享插件
    shared_path = root / _SHARED_DIR / "pipeline" / category / plugin_name
    if shared_path.is_dir():
        return f"plugins.{_SHARED_DIR}.pipeline.{category}.{plugin_name}"

    # 3. 旧路径回退（src/plugins/ 下的原位置）
    legacy_path = root.parent / "src" / "plugins" / category / plugin_name
    if legacy_path.is_dir():
        return f"plugins.{category}.{plugin_name}"

    return None


def _merge_with_override(
    base: list[PluginLocation],
    overrides: list[PluginLocation],
) -> list[PluginLocation]:
    """用 overrides 覆盖 base 中同名的插件。

    同名判断：category + plugin_name 相同。

    Args:
        base: 基础列表（通常是共享插件）
        overrides: 覆盖列表（通常是租户插件）

    Returns:
        合并后的列表
    """
    override_keys = {(loc.category, loc.plugin_name) for loc in overrides}
    filtered_base = [loc for loc in base if (loc.category, loc.plugin_name) not in override_keys]
    return filtered_base + overrides
