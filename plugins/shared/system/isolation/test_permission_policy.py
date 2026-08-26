# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation permission_policy.py 策略管理器测试（A5.3 补）。

覆盖 PermissionPolicyManager 直接面：
1. 默认五策略加载（default/subtask/root_task/system_config/readonly）与字段形状；
2. custom_policies 覆盖、未知 policy_type 回退 DEFAULT、非 dict 条目跳过；
3. get_policy：枚举/字符串入参、未知策略回退默认；
4. get_default_policy / get_readonly_policy 缺省兜底；
5. list_policies / has_policy；
6. get_policy_name_for_agent_level：None/数值/带 L 前缀/非法字符串；
7. _load_from_config_file：config_center 可用与失败两路径。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_mod() -> Any:
    mod_name = "isolation_permission_policy_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "permission_policy.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_mod()
PermissionPolicyManager = _MOD.PermissionPolicyManager
PermissionPolicyType = _MOD.PermissionPolicyType
PermissionScope = _MOD.PermissionScope
get_policy_name_for_agent_level = _MOD.get_policy_name_for_agent_level


class TestDefaultPolicies:
    def test_all_defaults_loaded(self) -> None:
        mgr = PermissionPolicyManager()
        names = mgr.list_policies()
        assert set(names) == {"default", "subtask", "root_task", "system_config", "readonly"}

    def test_default_policy_shape(self) -> None:
        mgr = PermissionPolicyManager()
        p = mgr.get_default_policy()
        assert p.policy_type == PermissionPolicyType.DEFAULT
        assert p.read.scope == PermissionScope.PROJECT and p.read.allow_all is True
        assert p.write.scope == PermissionScope.WORKSPACE and p.write.allow_outside is False

    def test_root_task_policy_requires_confirmation(self) -> None:
        mgr = PermissionPolicyManager()
        p = mgr.get_policy(PermissionPolicyType.ROOT_TASK)
        assert p.write.require_confirmation is True
        assert p.write.allow_outside is True

    def test_system_config_requires_checkpoint(self) -> None:
        mgr = PermissionPolicyManager()
        p = mgr.get_policy("system_config")
        assert p.write.require_checkpoint is True
        assert p.write.allowed_operations == ["create", "modify"]

    def test_readonly_blocks_write(self) -> None:
        mgr = PermissionPolicyManager()
        p = mgr.get_readonly_policy()
        assert p.write.scope == PermissionScope.NONE

    def test_has_policy(self) -> None:
        mgr = PermissionPolicyManager()
        assert mgr.has_policy("default") is True
        assert mgr.has_policy("ghost") is False


class TestCustomPolicies:
    def test_custom_overrides_default(self) -> None:
        mgr = PermissionPolicyManager(
            custom_policies={
                "default": {
                    "read": {"scope": "custom", "custom_paths": ["/only/here"]},
                    "write": {"scope": "none"},
                    "policy_type": "readonly",
                }
            }
        )
        p = mgr.get_policy("default")
        assert p.read.scope == PermissionScope.CUSTOM
        assert p.read.custom_paths == ["/only/here"]
        assert p.write.scope == PermissionScope.NONE

    def test_custom_invalid_policy_type_falls_back_default(self) -> None:
        mgr = PermissionPolicyManager(
            custom_policies={"weird": {"policy_type": "not-a-type", "read": {}, "write": {}}}
        )
        p = mgr.get_policy("weird")
        assert p.policy_type == PermissionPolicyType.DEFAULT

    def test_get_unknown_policy_falls_back_default(self) -> None:
        mgr = PermissionPolicyManager()
        p = mgr.get_policy("no-such-policy")
        assert p.name == "default"

    def test_get_policy_with_enum_input(self) -> None:
        mgr = PermissionPolicyManager()
        p = mgr.get_policy(PermissionPolicyType.SUBTASK)
        assert p.name == "subtask"


class TestAgentLevelPolicyName:
    def test_none_returns_root_task(self) -> None:
        assert get_policy_name_for_agent_level(None) == "root_task"

    def test_l1_and_int_inputs(self) -> None:
        assert get_policy_name_for_agent_level("L1") == "root_task"
        assert get_policy_name_for_agent_level(1) == "root_task"
        assert get_policy_name_for_agent_level("1") == "root_task"

    def test_l2_plus_returns_subtask(self) -> None:
        assert get_policy_name_for_agent_level("L2") == "subtask"
        assert get_policy_name_for_agent_level(2) == "subtask"
        assert get_policy_name_for_agent_level("L5") == "subtask"

    def test_invalid_level_returns_root_task(self) -> None:
        assert get_policy_name_for_agent_level("abc") == "root_task"
        assert get_policy_name_for_agent_level("") == "root_task"


class TestConfigFileLoad:
    def test_config_center_unavailable_falls_back_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config_center 导入失败 → 回退代码默认策略。"""
        import builtins

        real_import = builtins.__import__

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "config.config_center":
                raise ImportError("no config_center")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        mgr = PermissionPolicyManager()
        assert mgr.has_policy("default") is True
        assert mgr.list_policies() == list(PermissionPolicyManager.DEFAULT_POLICIES.keys())

    def test_config_center_with_policies_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config_center 返回带 permission_policies 的配置 → 覆盖同名默认策略。"""
        import builtins
        import types

        real = builtins.__import__

        fake_cc = types.ModuleType("config")
        fake_center = types.ModuleType("config.config_center")

        class _FakeCenter:
            def get(self, key: str) -> dict:
                return {
                    "permission_policies": {
                        "root_task_policy": {
                            "policy_type": "root_task",
                            "read": {"scope": "project", "allow_all": True},
                            "write": {"scope": "workspace", "allow_outside": False},
                        },
                        "special_directories": ["/data"],  # 非策略段应跳过
                    }
                }

        fake_center.get_config_center = lambda: _FakeCenter()
        fake_cc.config_center = fake_center

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "config.config_center":
                return fake_center
            if name == "config":
                return fake_cc
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        mgr = PermissionPolicyManager()
        p = mgr.get_policy("root_task")
        # 配置文件覆盖：write.scope 变 workspace（默认是 project）
        assert p.write.scope == PermissionScope.WORKSPACE
        assert p.read.allow_all is True

    def test_config_center_non_dict_entry_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """permission_policies 段含非 dict 条目 → 跳过不崩。"""
        import builtins
        import types

        real = builtins.__import__
        fake_center = types.ModuleType("config.config_center")

        class _FakeCenter:
            def get(self, key: str) -> dict:
                return {"permission_policies": {"broken": "not-a-dict"}}

        fake_center.get_config_center = lambda: _FakeCenter()

        def _fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "config.config_center":
                return fake_center
            return real(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _fake_import)
        mgr = PermissionPolicyManager()  # 不抛
        assert mgr.has_policy("default") is True
