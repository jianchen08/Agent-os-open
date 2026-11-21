# @feature: FP-0.2.grants_ledger 授权账本 P3 运营面 | @ci: python-coverage
"""grants trust_profile 三档（P3 运营面）测试——API 契约由本文件定义（TDD Red 先行）。

完成判据 2（任务书）：三档各一升降档断言 + 默认 balanced 零行为变化断言 +
终审/block 恒不可豁免断言。

设计真值：ADR D2（trust profile 作用于 classify 升降档矩阵）+ 执行方案 §5：
strict 把 task_grant 降为 per_call_approval；open 放宽 domain_grant 覆盖面
（task_grant → domain_grant）；balanced（默认）= P1 行为等价（零行为变化红线）。

红线锚（ADR D3）：deny / per_call_approval / auto 三类 verdict 任何档不可改写
——deny 恒 deny（block 语义数据面）、per_call_approval 恒逐次（审批面恒在，
永不放宽）、auto 恒 auto（读面 v1 不动档）；规则面 block/final_approval 恒先于
grant（执法面 P1 语义，本模块不触碰该面）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_ROOT = str(Path(__file__).resolve().parents[1])
for _p in (_SHARED_ROOT, _THIS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import policy as grants_policy  # noqa: E402 — 平铺插件模块

if TYPE_CHECKING:
    from policy import PolicyEngine  # 仅注解面类型名；运行时经 grants_policy 命名空间取

TrustProfile = grants_policy.TrustProfile
resolve_trust_profile_path = grants_policy.resolve_trust_profile_path

_TABLE = {
    "version": 1,
    "defaults": {"uncategorized": "deny"},
    "tools": {
        "memory.retrieve": {"effect": "read", "verdict": "auto"},
        "memory.store": {"effect": "write", "verdict": "task_grant"},
        "memory.delete": {"effect": "write", "verdict": "per_call_approval"},
        "bash.execute": {"effect": "execute", "verdict": "deny"},
        "browser.navigate": {"effect": "network", "verdict": "domain_grant"},
    },
}


def _engine(profile: object = None) -> PolicyEngine:
    return grants_policy.PolicyEngine(dict(_TABLE), profile=profile)  # type: ignore[arg-type]


class TestBalancedIdentity:
    """默认 balanced = P1 行为等价（零行为变化红线）。"""

    def test_profile_none_defaults_to_balanced_identity(self) -> None:
        engine = grants_policy.PolicyEngine(dict(_TABLE))
        assert engine.classify("memory", "store")["verdict"] == "task_grant"
        assert engine.classify("browser", "navigate")["verdict"] == "domain_grant"

    def test_balanced_verdicts_identical_to_no_profile_engine(self) -> None:
        plain = grants_policy.PolicyEngine(dict(_TABLE))
        balanced = _engine(TrustProfile.balanced())
        for tool, action in (("memory", "store"), ("browser", "navigate"), ("memory", "retrieve"), ("bash", "execute")):
            assert balanced.classify(tool, action) == plain.classify(tool, action)

    def test_balanced_result_carries_no_trust_profile_key(self) -> None:
        got = _engine(TrustProfile.balanced()).classify("memory", "store")
        assert "trust_profile" not in got


class TestTierTransitions:
    """三档各一升降档断言（任务书判据 2）。"""

    def test_strict_downgrades_task_grant_to_per_call_approval(self) -> None:
        got = _engine(TrustProfile.from_dict({"default_profile": "strict"})).classify("memory", "store")
        assert got["verdict"] == "per_call_approval"
        assert got["policy_key"] == "memory.store"
        assert got["effect"] == "write"
        assert got["trust_profile"] == "strict"

    def test_strict_downgrades_domain_grant_to_task_grant(self) -> None:
        got = _engine(TrustProfile.from_dict({"default_profile": "strict"})).classify("browser", "navigate")
        assert got["verdict"] == "task_grant"

    def test_open_widens_task_grant_to_domain_grant(self) -> None:
        got = _engine(TrustProfile.from_dict({"default_profile": "open"})).classify("memory", "store")
        assert got["verdict"] == "domain_grant"
        assert got["trust_profile"] == "open"

    def test_open_keeps_domain_grant(self) -> None:
        got = _engine(TrustProfile.from_dict({"default_profile": "open"})).classify("browser", "navigate")
        assert got["verdict"] == "domain_grant"
        assert "trust_profile" not in got  # 未发生改写 → 不加档位标注


class TestInvariantsNeverExempted:
    """终审/block 恒不可豁免：deny / per_call_approval / auto 任何档不可改写。"""

    @pytest.mark.parametrize("profile_name", ["balanced", "strict", "open"])
    def test_deny_stays_deny_any_tier(self, profile_name: str) -> None:
        engine = _engine(TrustProfile.from_dict({"default_profile": profile_name}))
        assert engine.classify("bash", "execute")["verdict"] == "deny"

    @pytest.mark.parametrize("profile_name", ["balanced", "strict", "open"])
    def test_per_call_approval_stays_per_call_any_tier(self, profile_name: str) -> None:
        engine = _engine(TrustProfile.from_dict({"default_profile": profile_name}))
        got = engine.classify("memory", "delete")
        assert got["verdict"] == "per_call_approval"
        assert "trust_profile" not in got

    @pytest.mark.parametrize("profile_name", ["balanced", "strict", "open"])
    def test_auto_stays_auto_any_tier(self, profile_name: str) -> None:
        engine = _engine(TrustProfile.from_dict({"default_profile": profile_name}))
        got = engine.classify("memory", "retrieve")
        assert got["verdict"] == "auto"
        assert "trust_profile" not in got

    def test_uncategorized_deny_unaffected_by_open(self) -> None:
        engine = _engine(TrustProfile.from_dict({"default_profile": "open"}))
        got = engine.classify("totally_unknown", "run")
        assert got["verdict"] == "deny"
        assert got["reason"] == "unclassified"


class TestProfileParsing:
    """配置解析：闭集档名 + fail-closed 方向（坏配置 → strict 收紧）。"""

    def test_closed_set_profile_names(self) -> None:
        assert grants_policy.TRUST_PROFILES == ("strict", "balanced", "open")

    def test_balanced_dict_is_identity(self) -> None:
        profile = TrustProfile.from_dict({"default_profile": "balanced"})
        assert profile.name == "balanced"
        assert profile.apply("task_grant") == "task_grant"

    @pytest.mark.parametrize("bad", [None, "strict", [], {"default_profile": "yolo"}, {}])
    def test_malformed_config_falls_back_to_strict(self, bad: object) -> None:
        assert TrustProfile.from_dict(bad).name == "strict"

    def test_malformed_config_tightens_task_grant(self) -> None:
        assert TrustProfile.from_dict({"default_profile": "yolo"}).apply("task_grant") == "per_call_approval"

    def test_profiles_section_not_consumed(self) -> None:
        """矩阵为代码固定（ADR D2 语义由测试锁定），配置数据不可改写升降档矩阵。"""
        data = {
            "default_profile": "strict",
            "profiles": {"strict": {"task_grant": "auto"}},  # 试图把 strict 的收紧改松动
        }
        profile = TrustProfile.from_dict(data)
        assert profile.apply("task_grant") == "per_call_approval"


class TestProfileFileResolution:
    """文件解析：settings 覆盖 > 仓库 config 缺省；缺文件 = balanced（P1 等价）。"""

    def test_settings_override_wins(self, tmp_path: Path) -> None:
        override = tmp_path / "trust_profile.yaml"
        override.write_text("default_profile: strict\n", encoding="utf-8")
        got = resolve_trust_profile_path({"trust_profile_file": str(override)})
        assert got == str(override)

    def test_default_points_at_repo_config(self) -> None:
        got = resolve_trust_profile_path(None)
        assert Path(got).name == "trust_profile.yaml"
        assert "config" in Path(got).parts

    def test_missing_file_resolves_to_balanced(self, tmp_path: Path) -> None:
        profile = TrustProfile.from_yaml_file(str(tmp_path / "absent.yaml"))
        assert profile.name == "balanced"

    def test_empty_file_resolves_to_balanced(self, tmp_path: Path) -> None:
        # 空文件/非映射 = 未配置面（与文件缺失同判），回落出厂默认
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        assert TrustProfile.from_yaml_file(str(p)).name == "balanced"

    def test_shipped_yaml_defaults_to_balanced(self) -> None:
        profile = TrustProfile.from_yaml_file(resolve_trust_profile_path(None))
        assert profile.name == "balanced"

    def test_yaml_file_with_strict_takes_effect(self, tmp_path: Path) -> None:
        p = tmp_path / "trust_profile.yaml"
        p.write_text("version: 1\ndefault_profile: strict\n", encoding="utf-8")
        engine = grants_policy.PolicyEngine(dict(_TABLE), profile=TrustProfile.from_yaml_file(str(p)))
        assert engine.classify("memory", "store")["verdict"] == "per_call_approval"


class TestServerTrustProfileWiring:
    """server._build_policy 消费 config 注入 trust_profile 命名空间（生效点唯一性）。"""

    @pytest.fixture
    def server_mod(self) -> object:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "grants_p3_server_tp", str(Path(_THIS_DIR) / "server.py")
        )
        assert spec is not None
        assert spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules["grants_p3_server_tp"] = mod
        spec.loader.exec_module(mod)
        mod.reset_singletons()
        return mod

    def test_injected_trust_profile_applies(self, server_mod: object) -> None:
        policy = server_mod._build_policy(
            {
                "tool_policy": {"tools": {"memory.store": {"effect": "write", "verdict": "task_grant"}}},
                "trust_profile": {"default_profile": "strict"},
            }
        )
        assert policy.classify("memory", "store")["verdict"] == "per_call_approval"

    def test_no_injection_uses_shipped_file_balanced(self, server_mod: object) -> None:
        policy = server_mod._build_policy(
            {"tool_policy": {"tools": {"memory.store": {"effect": "write", "verdict": "task_grant"}}}}
        )
        assert policy.classify("memory", "store")["verdict"] == "task_grant"

    def test_settings_file_override_missing_file_is_balanced(self, server_mod: object, tmp_path: Path) -> None:
        policy = server_mod._build_policy(
            {
                "tool_policy": {"tools": {"memory.store": {"effect": "write", "verdict": "task_grant"}}},
                "settings": {"trust_profile_file": str(tmp_path / "absent.yaml")},
            }
        )
        assert policy.classify("memory", "store")["verdict"] == "task_grant"
