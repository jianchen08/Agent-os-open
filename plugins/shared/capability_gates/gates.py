"""能力门禁表解析器（原则 P1：插件集合三端一致，运行时降级不裁集合）。

门禁表：仓库配置树 plugins/ 下的 capability_gates.yaml（config 根拼接，
产物由 scripts/audit_plugin_deps.py 生成），顶层 mapping
``plugin_id -> {min_profile, degrade_reason}``；``min_profile`` 词表
{"phone", "desktop", "cloud"}（与 agentos_plugin_sdk.device_profile.TIERS
同一词表），``degrade_reason`` 非空字符串（前端「此端降级」提示文案）。

行为契约（tests/test_mobile_gm1_contracts.py::TestCapabilityGatesContract）：
- 文件不存在 = fail-open：返回空门禁集（全量默认不降级），不抛错；
- 字段非法（词表外/缺字段/空 reason/类型违约/未知字段）= fail-closed：
  抛 ValueError（schema 校验错），YAML 解析错在边界翻译为 ValueError，
  不以 yaml.YAMLError 形态冒充 schema 错外泄；
- 查表语义：解析结果只含被门禁点名的插件——命中=降级，未命中=不降级。

运行时消费：启动面按 detect_device_profile().tier 解析本表，命中
``min_profile`` 高于当前档的插件打降级标记（接线随运行时降级判定步落地，
本模块只负责解析面）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

#: min_profile 合法词表（档位从低到高：低于当前档的 min_profile 命中降级）。
MIN_PROFILE_VOCABULARY: frozenset[str] = frozenset({"phone", "desktop", "cloud"})

#: 门禁行允许的字段集（精确匹配，未知字段拒绝）。
_ALLOWED_FIELDS: frozenset[str] = frozenset({"min_profile", "degrade_reason"})


def _fail(plugin_id: object, detail: str) -> ValueError:
    """构造带插件定位的 schema 校验错（fail-closed 载体）。"""
    return ValueError(f"capability_gates 表项 {plugin_id!r} 非法：{detail}")


def _validate_gate(plugin_id: str, gate: Any) -> dict[str, object]:
    """校验单条门禁行，返回规范化后的 {min_profile, degrade_reason}。"""
    if not isinstance(gate, dict):
        raise _fail(plugin_id, f"门禁行必须是 mapping，得到 {type(gate).__name__}")
    unknown = set(gate) - _ALLOWED_FIELDS
    if unknown:
        raise _fail(plugin_id, f"未知字段 {sorted(unknown)}（允许字段：{sorted(_ALLOWED_FIELDS)}）")
    missing = _ALLOWED_FIELDS - set(gate)
    if missing:
        raise _fail(plugin_id, f"缺少必填字段 {sorted(missing)}")
    min_profile = gate["min_profile"]
    if not isinstance(min_profile, str) or min_profile not in MIN_PROFILE_VOCABULARY:
        raise _fail(
            plugin_id,
            f"min_profile 必须是词表 {sorted(MIN_PROFILE_VOCABULARY)} 内的字符串，得到 {min_profile!r}",
        )
    reason = gate["degrade_reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise _fail(plugin_id, "degrade_reason 必须是非空字符串")
    return {"min_profile": min_profile, "degrade_reason": reason}


def load_capability_gates(path: Path | str) -> dict[str, dict[str, object]]:
    """解析能力门禁表：缺失 fail-open 返回空集，字段非法 fail-closed 报错。

    ``path`` 指向 capability_gates.yaml；返回 {plugin_id: {min_profile,
    degrade_reason}}（查表语义：仅含被点名的插件）。
    """
    file = Path(path)
    if not file.is_file():
        return {}
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        # 边界翻译：解析错以 ValueError 形态外泄，与 schema 校验错同族，
        # 消费方无需感知 yaml 实现细节。
        raise ValueError(f"capability_gates 表 YAML 解析失败（{file}）：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"capability_gates 表顶层必须是 mapping（plugin_id -> 门禁行），得到 {type(raw).__name__}")
    gates: dict[str, dict[str, object]] = {}
    for plugin_id, gate in raw.items():
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ValueError(f"capability_gates 表插件 id 必须是非空字符串，得到 {plugin_id!r}")
        gates[plugin_id] = _validate_gate(plugin_id, gate)
    return gates
