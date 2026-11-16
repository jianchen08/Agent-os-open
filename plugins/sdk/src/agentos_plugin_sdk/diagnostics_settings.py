"""诊断设置 — 用户空间 settings.yaml 的 diagnostics 段，运行时可调开关的单一事实源。

优先级：``settings.yaml`` 的 ``diagnostics`` 段 > :data:`DEFAULTS`；env 不是
该配置面的通道（ADR 2026-10-10-diagnostics-settings-yaml，
``AGENTOS_PAYLOAD_DIAG`` / ``AGENTOS_LOG_PROMPT_BODY`` env 键已退役）。
文件不存在 = 全默认（零操作前提：用户不改任何东西即为正确状态，文件只在
主动改过设置后才出现）。

读取方（llm adapter：payload_diag / prompt_audit，mtime 缓存热读——改
settings 后下一次 LLM 调用即生效，无需 respawn sidecar）与读写方
（monitoring ``/ext/monitoring/diagnostics-settings`` 端点）共用本模块的
同一解析，键面与语义漂移在此单点封死。

本模块另承载 LLM 请求快照目录的单源解析（:func:`payload_diag_dir`，
``AGENTOS_LOG_DIR`` 部署覆盖 > 用户空间 ``logs/payload_diag``）——写端
adapter 与读端 monitoring 共用同一函数，读写一致性由单点保证。

用户空间根解析与内核 ``core::user_space`` 对齐：``AGENTOS_USER_ROOT`` env
优先，否则 OS 标准数据目录（Windows ``%APPDATA%``、macOS
``~/Library/Application Support``、Linux XDG data home）下的 ``agentos``。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

# 诊断开关键面（唯一注册处）：新诊断开关一律先在此登记默认值。
DEFAULTS: MappingProxyType[str, bool] = MappingProxyType(
    {
        "payload_diag": True,
        "prompt_audit": False,
    }
)

# mtime 缓存：settings_path -> (mtime_ns, size, 解析结果)。读多写少且竞争
# 最坏情形是多解析一次文件（无副作用），不加锁。
_cache: dict[Path, tuple[int, int, dict[str, bool]]] = {}


def user_root() -> Path:
    """用户空间根：AGENTOS_USER_ROOT env > OS 标准数据目录/agentos。

    与内核 ``user_space::user_root()`` 同一解析（env 显式覆盖能力归部署，
    缺省锚 OS 标准目录），两侧根必须一致——否则读写端错位（payload_diag
    双源副本目录错位的同族失败模式）。

    **对齐副本锚**：Python 侧本体 = ``plugins/shared/user_space.py``
    （``_os_data_dir``/``user_root``）。本模块是 SDK 包边界副本（user_space
    是共享根裸模块，SDK venv 无共享根可达，不可 import）——同输入同输出由
    对齐断言钉死（``tests/plugins/shared/test_user_space.py::
    TestSdkDiagnosticsCopyAlignment``），改动须与本体同刀。已知微差（裁定
    非漂移）：OS 数据目录不可得时本副本回退 ``~/AppData/Roaming`` 兜底
    （settings.yaml 必须可落位，永不 None），本体诚实返回 None。
    """
    env_val = os.environ.get("AGENTOS_USER_ROOT", "").strip()
    if env_val:
        return Path(env_val)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "agentos"


def settings_path() -> Path:
    """settings.yaml 路径（用户空间根下，用户资产区：升级/重装不销毁）。"""
    return user_root() / "settings.yaml"


def payload_diag_dir() -> Path:
    """LLM 请求快照目录（读写单源）：AGENTOS_LOG_DIR 部署覆盖 > 用户空间。

    裁定 2026-11-02：快照是用户侧诊断产物，缺省锚用户空间
    ``logs/payload_diag``；禁按插件文件位置/cwd 向上推导——装机布局下写端
    探测命中包内 resources、读端 env 命中用户空间，读写分叉致「LLM 请求」
    调试页永远看不到新快照（ADR 2026-11-02-payload-diag-user-space-anchor）。
    llm adapter（写）与 monitoring（读）都经本函数取目录，改锚定只改这里。
    """
    env_val = os.environ.get("AGENTOS_LOG_DIR", "").strip()
    if env_val:
        return Path(env_val) / "logs" / "payload_diag"
    return user_root() / "logs" / "payload_diag"


def _coerce_bools(raw: Any) -> dict[str, bool]:
    """diagnostics 段容错收敛：非 dict 段/非 bool 键值忽略，回落默认。

    bool 严格判定（``isinstance(v, bool)``）——yaml 的 ``"true"`` 字符串、
    ``1`` 整数等弱真值不认，避免手写 yaml 时的静默歧义。
    """
    if not isinstance(raw, dict):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    for key in DEFAULTS:
        val = raw.get(key)
        if isinstance(val, bool):
            merged[key] = val
    return merged


def read_diagnostics() -> dict[str, bool]:
    """读诊断开关（mtime+size 缓存；无文件/坏 yaml/坏段回落默认）。

    返回独立副本（调用方修改不影响缓存）。
    """
    path = settings_path()
    try:
        stat = path.stat()
        key = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        _cache.pop(path, None)
        return dict(DEFAULTS)

    cached = _cache.get(path)
    if cached is not None and (cached[0], cached[1]) == key:
        return dict(cached[2])

    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        # 坏文件（半截写入等）回落默认，不阻断调用方主路径
        return dict(DEFAULTS)
    diagnostics = _coerce_bools(doc.get("diagnostics") if isinstance(doc, dict) else None)
    _cache[path] = (*key, diagnostics)
    return dict(diagnostics)


def write_diagnostics(partial: dict[str, bool]) -> dict[str, bool]:
    """合并写回 diagnostics 段（保留 settings.yaml 其余段与未知键）。

    只接受 DEFAULTS 注册键（未知键拒绝——键面单点登记，防散装漂移）；
    非_bool 值拒绝。原子写（temp + replace）防半截文件。返回写后全量值。
    """
    unknown = set(partial) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"未知诊断开关键: {sorted(unknown)}；合法键 = {sorted(DEFAULTS)}")
    for key, val in partial.items():
        if not isinstance(val, bool):
            raise ValueError(f"诊断开关 {key} 须为 bool，收到 {type(val).__name__}")

    path = settings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except (OSError, yaml.YAMLError):
        doc = None
    if not isinstance(doc, dict):
        doc = {}
    # 基于原始段合并（非 _coerce_bools 重建）：段内未注册键原样保留——
    # 未来版本新增键 + 本版本写回，不静默丢用户文件里的前向键
    raw_section = doc.get("diagnostics")
    section = dict(raw_section) if isinstance(raw_section, dict) else {}
    section.update(partial)
    doc["diagnostics"] = section

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, default_flow_style=False, sort_keys=True)
    os.replace(tmp, path)
    _cache.pop(path, None)
    return _coerce_bools(section)


__all__ = ["DEFAULTS", "user_root", "settings_path", "read_diagnostics", "write_diagnostics"]
