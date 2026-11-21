"""grants 策略引擎——``tool_policy.yaml`` 动作级分级表加载缓存 + classify。

设计真值：ADR D2（分级按动作级 effect，``tool.action`` 键；未分类 fail-closed =
deny + 审计，与 CI 覆盖门禁把补级成本固定为一次）+ 执行方案 §6（schema）+
执行方案 §5（P3 trust_profile 三档升降档矩阵）。

红线锚（ADR D3）：grant 只能减少重复审批，不能绕过 block 规则与 ``final_approval``
终审——规则面优先的执法语义由 P1 在 security_check 落，本模块只在 verdict 枚举与
notes 承载该语义的数据面（如 ``bash.execute``）。

fail-closed 实现口径：``defaults.uncategorized`` 恒 deny——表数据试图把它改成
放行也不生效（D2 固化，非数据可配）；坏条目（未知 verdict/effect、非对象形态）
视同未分类 deny + 审计。

trust profile（P3）：三档（strict / balanced=默认 / open）作用于 classify 查表
之后的 verdict 升降映射——生效点唯一在 grants 插件 classify 内部，执法面零感知。
矩阵代码固定（数据不可改写）；deny / per_call_approval / auto 三类 verdict 任何
档不可改写（block 语义/逐次人审/读面恒定）；balanced 恒等映射 = P1 行为等价
（零行为变化红线）；解析失败/档名未知 → strict（fail-closed 收紧方向）；文件
缺失 → balanced（未配置 = 出厂默认）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

VERDICTS = ("auto", "task_grant", "domain_grant", "per_call_approval", "deny")
EFFECTS = (
    "read",
    "write",
    "execute",
    "network",
    "external_side_effect",
    "financial",
    "device_control",
    "admin",
)

#: deny 结果的固定形态（reason ∈ unclassified | invalid_entry）。
_DENY_UNCLASSIFIED: dict[str, Any] = {
    "verdict": "deny",
    "effect": None,
    "scope": None,
    "policy_key": None,
    "reason": "unclassified",
}


def resolve_policy_path(settings: dict[str, Any] | None = None) -> str:
    """策略表落点：config 注入 settings 的 ``tool_policy_file`` 优先；缺省
    ``<config root>/plugins/grants/tool_policy.yaml``（仓库布局与装机 resources
    同相对结构：plugins/shared/grants 的三级上级即 config 根）。运行时主路径是
    config_files 注入的解析后 dict（server 优先消费），本文件路径解析为 dev /
    测试 / 未注入兜底面。"""
    configured = str((settings or {}).get("tool_policy_file") or "").strip()
    if configured:
        return configured
    config_root = Path(__file__).resolve().parents[3]
    return str(config_root / "config" / "plugins" / "grants" / "tool_policy.yaml")


def resolve_trust_profile_path(settings: dict[str, Any] | None = None) -> str:
    """trust profile 落点：config 注入 settings 的 ``trust_profile_file`` 优先；
    缺省 ``<config root>/plugins/grants/trust_profile.yaml``（与 resolve_policy_path
    同构：config_files 注入 dict 为主路径，本文件解析为 dev / 测试 / 未注入兜底面）。"""
    configured = str((settings or {}).get("trust_profile_file") or "").strip()
    if configured:
        return configured
    config_root = Path(__file__).resolve().parents[3]
    return str(config_root / "config" / "plugins" / "grants" / "trust_profile.yaml")


# ── trust profile 三档（P3，ADR D2 + 执行方案 §5）────────────────────────────
#: 档名闭集。
TRUST_PROFILES = ("strict", "balanced", "open")

#: 升降档矩阵（代码固定，配置数据不可改写——矩阵语义由测试锁定，ADR D2）：
#: - balanced 恒等映射 = P1 行为等价（零行为变化红线）；
#: - strict 把 grant 系逐级收紧（task_grant → 逐次人审；domain_grant → 任务级）；
#: - open 把 task_grant 放宽为 domain_grant（domain_grant 覆盖面放宽：一条域级
#:   grant 覆盖更多同类调用；domain_grant 本身不动，不产生 auto 化旁路）。
#: 不在矩阵里的 verdict（auto / per_call_approval / deny）任何档恒不变——
#: block 语义、逐次人审与读面 v1 行为不受档位摆布（ADR D3 红线数据面）。
_TRUST_MATRIX: dict[str, dict[str, str]] = {
    "balanced": {},
    "strict": {"task_grant": "per_call_approval", "domain_grant": "task_grant"},
    "open": {"task_grant": "domain_grant"},
}


class TrustProfile:
    """trust profile 档位：``apply(verdict)`` 升降映射 + classify 结果改写。

    balanced 结果对象原样返回（不加档位键，字节级 P1 等价）；strict/open 在
    实际发生改写的结果上补 ``trust_profile`` 档位标注（可观测），未命中矩阵的
    verdict 原样返回不加键。
    """

    def __init__(self, name: str, matrix: dict[str, str] | None = None) -> None:
        self.name = name
        self._matrix: dict[str, str] = {} if name == "balanced" else dict(matrix or {})

    @classmethod
    def balanced(cls) -> TrustProfile:
        return cls("balanced")

    @classmethod
    def from_dict(cls, data: Any) -> TrustProfile:
        """解析注入 dict（config_files trust_profile 命名空间形态）。

        档名闭集外/形态坏 → strict（fail-closed 收紧方向：坏配置绝不产生比
        出厂默认更松的授权面）。
        """
        if not isinstance(data, dict):
            return cls("strict", _TRUST_MATRIX["strict"])
        name = data.get("default_profile")
        if name not in TRUST_PROFILES:
            return cls("strict", _TRUST_MATRIX["strict"])
        return cls(name, _TRUST_MATRIX[name])

    @classmethod
    def from_yaml_file(cls, path: str | Path) -> TrustProfile:
        """文件态解析：文件缺失/不可解析 = 未配置 → balanced（出厂默认 = P1 等价）；
        可解析但档名坏 → strict（fail-closed）。"""
        try:
            with open(path, encoding="utf-8") as fh:
                data: Any = yaml.safe_load(fh)
        except (OSError, ValueError):
            return cls.balanced()
        if not isinstance(data, dict):
            # 空文件/非映射：与「文件缺失」同判（未配置面），回落出厂默认
            return cls.balanced()
        return cls.from_dict(data)

    def apply(self, verdict: str) -> str:
        """verdict 升降映射（矩阵外 verdict 恒等）。"""
        return self._matrix.get(verdict, verdict)

    def apply_to_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """classify 结果按档改写：balanced 原样返回；实际改写时补档位标注。"""
        if self.name == "balanced":
            return result
        mapped = self._matrix.get(result.get("verdict", ""))
        if mapped is None or mapped == result.get("verdict"):
            return result
        return {**result, "verdict": mapped, "trust_profile": self.name}


class PolicyEngine:
    """动作级分级引擎：``(tool, action)`` 查表 → {verdict, effect, scope}。

    查表序：``tool.action`` 精确键 → ``tool`` 整工具键（单动作工具形态，
    执行方案 §6）→ 未分类。命中后校验条目（verdict/effect 闭集）——非法条目
    fail-closed deny + 审计，绝不按表数据放行。
    """

    def __init__(self, table: dict[str, Any], *, ledger: Any | None = None, profile: TrustProfile | None = None) -> None:
        tools = table.get("tools")
        self._tools: dict[str, Any] = tools if isinstance(tools, dict) else {}
        self._ledger = ledger
        self._profile = profile if profile is not None else TrustProfile.balanced()

    @classmethod
    def from_yaml_file(cls, path: str | Path, *, ledger: Any | None = None, profile: TrustProfile | None = None) -> PolicyEngine:
        with open(path, encoding="utf-8") as fh:
            table: Any = yaml.safe_load(fh)
        if not isinstance(table, dict):
            table = {}
        return cls(table, ledger=ledger, profile=profile)

    def classify(self, tool: str, action: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """裁决一次工具调用动作；未分类/坏条目 → deny（并按需落 deny 审计事件）。

        args 仅作未来上下文修正因子入口（调研 §3.6）；v1 不参与查表，也不入
        审计上下文（参数面可能含敏感值）。

        trust profile（P3）作用于查表成功后的 verdict 升降映射；deny 路径（未
        分类/坏条目）在映射之前返回——未分类 fail-closed 不受档位摆布。
        """
        del args  # v1 未消费（保留形参占位；上下文修正因子 P1+ 再接）
        exact_key = f"{tool}.{action}"
        entry = self._tools.get(exact_key)
        policy_key = exact_key
        if entry is None:
            whole_tool_key = str(tool)
            entry = self._tools.get(whole_tool_key)
            policy_key = whole_tool_key
        if entry is None:
            return self._deny(tool, action, reason="unclassified", policy_key=None)
        if not isinstance(entry, dict) or entry.get("verdict") not in VERDICTS or entry.get("effect") not in EFFECTS:
            return self._deny(tool, action, reason="invalid_entry", policy_key=policy_key)
        return self._profile.apply_to_result(
            {
                "verdict": entry["verdict"],
                "effect": entry["effect"],
                "scope": entry.get("scope"),
                "policy_key": policy_key,
                "reason": None,
            }
        )

    def _deny(self, tool: str, action: str, *, reason: str, policy_key: str | None) -> dict[str, Any]:
        result = dict(_DENY_UNCLASSIFIED)
        if policy_key is not None:
            result["policy_key"] = policy_key
        result["reason"] = reason
        if self._ledger is not None:
            self._ledger.append_event(
                event_type="deny",
                context={"tool": tool, "action": action, "reason": reason, "policy_key": policy_key},
            )
        return result


#: mtime 缓存：path → (mtime_ns, engine)。文件改动即失效（热发现语义同构）。
_POLICY_CACHE: dict[str, tuple[int, PolicyEngine]] = {}


def load_policy_cached(path: str) -> PolicyEngine:
    """文件态策略表加载（mtime 缓存；同一文件复用同一 engine 实例）。"""
    key = str(Path(path).resolve())
    mtime_ns = os.stat(key).st_mtime_ns
    cached = _POLICY_CACHE.get(key)
    if cached is not None and cached[0] == mtime_ns:
        return cached[1]
    engine = PolicyEngine.from_yaml_file(key)
    _POLICY_CACHE[key] = (mtime_ns, engine)
    return engine
