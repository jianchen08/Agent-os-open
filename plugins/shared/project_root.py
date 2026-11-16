"""项目根解析单源 —— plugins/shared 各插件共用的根目录定位器。

F-2 收敛前 ``_resolve_project_root`` 同名实现散布多处（cost_control /
llm×3 / tool_args_inject / monitoring），探测特征与兜底各自
漂移（复制必漂移）；本模块把「env 优先 → 向上探测 → 缓存」三步收敛为
单点，消费方按自己的目录指纹调用：

- ``("config", "config/kernel")``：cost_control、llm/routes_thinking_mode
- ``("config",)``：llm/routes_llm_config
- ``("config/kernel",)``：llm/_config_models、tool_args_inject
- ``("config/models",)``：monitoring

与 repo_anchor 的分工：repo_anchor 是文件读/写面的**安全判定**锚（拒绝集、
读面裁决，ADR 2026-09-12 语义独立，不并入本模块）；本模块只做路径解析。
env 语义与 repo_anchor 同源：``AGENTOS_CONFIG_ROOT``（内核启动发布，指向
``<根>/config``）优先，值本身或其父目录按指纹校验命中即采用，不中回落探测
（env 指向别处不能劫持解析）；探测锚点（start）默认本模块位置，消费方按
原实现锚定各自文件位置传入（迁移前后逐位同结果）。

兜底（fallback）不进缓存：同一指纹的不同消费方兜底各异（各自文件位置的
parent×N），缓存只存 env/探测的解析结果，兜底由消费方在 miss 时自持。

导入形态与 ``http_json`` 先例一致：插件侧把 ``plugins/shared``
推上 sys.path 后裸名导入（如 ``from project_root import resolve_project_root``）。
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["resolve_project_root", "reset_cache"]

#: 内核启动发布的 config 根环境变量（值指向 ``<项目根>/config``，也可能直接
#: 指向项目根本身——两种形态都按指纹校验后采用）。
ENV_CONFIG_ROOT = "AGENTOS_CONFIG_ROOT"

# 按 (指纹, 探测锚点) 分别缓存（不同指纹/锚点结论独立）；值为 None = 已解析且未命中。
_cache: dict[tuple[tuple[str, ...], Path], Path | None] = {}


def _matches(candidate: Path, fingerprint: tuple[str, ...]) -> bool:
    """候选目录是否包含指纹的全部相对标记路径。"""
    return all((candidate / part).is_dir() for part in fingerprint)


def _from_env(fingerprint: tuple[str, ...]) -> Path | None:
    """env 优先：``AGENTOS_CONFIG_ROOT`` 本身或其父目录按指纹校验。

    两种合法形态：值 = ``<根>/config``（内核发布形态，取其父）或值 = 根本身。
    皆不中（非本项目布局）= 不采用，回落探测——与探测位置无关的单步。
    """
    env = os.environ.get(ENV_CONFIG_ROOT)
    if not env:
        return None
    p = Path(env)
    for candidate in (p.parent, p):
        if _matches(candidate, fingerprint):
            return candidate
    return None


def _probe(fingerprint: tuple[str, ...], start: Path) -> Path | None:
    """自 ``start`` 向上探测：第一个包含全部指纹标记的祖先目录（含 start 本身）。"""
    for candidate in [start, *start.parents]:
        if _matches(candidate, fingerprint):
            return candidate
    return None


def resolve_project_root(
    *fingerprint: str,
    start: Path | None = None,
    fallback: Path | None = None,
) -> Path | None:
    """解析项目根：env 优先 → 自锚点向上探测 → 按 (指纹, 锚点) 缓存。

    Args:
        fingerprint: 目录指纹——候选根须**全部**包含的相对路径标记
            （如 ``("config", "config/kernel")``）；至少一个，空指纹是调用
            方契约错误（ValueError）。
        start: 探测锚点目录（含自身向上）；缺省 = 本模块所在目录。消费方
            迁移时按原实现锚定各自文件位置传入，保证结果逐位不变。
        fallback: env/探测皆未命中时返回的兜底（None = 直接返回 None）。
            不参与缓存（见模块 docstring）。

    Returns:
        解析到的项目根；未命中且无兜底时为 None。结果按 (指纹, 锚点) 缓存，
        测试改环境后经 :func:`reset_cache` 重置。
    """
    if not fingerprint:
        raise ValueError("fingerprint 至少一个目录标记（空指纹无法定位项目根）")
    anchor = start if start is not None else Path(__file__).resolve().parent
    key = (fingerprint, anchor)
    if key not in _cache:
        root = _from_env(fingerprint)
        if root is None:
            root = _probe(fingerprint, anchor)
        _cache[key] = root
    root = _cache[key]
    return root if root is not None else fallback


def reset_cache() -> None:
    """清空解析缓存（测试在改 ``AGENTOS_CONFIG_ROOT`` 后调用）。"""
    _cache.clear()
