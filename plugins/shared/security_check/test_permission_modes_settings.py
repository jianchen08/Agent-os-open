# @feature: FP-0.2.二 | @vision: V2 安全
# @ci: python-coverage
"""security_check 权限模式落点 settings 注入测试（回退路径配置化）。

契约（manifest config_files id=settings → get_config()["settings"] 命名空间）：
- settings.permission_modes_file 配置覆盖：用户空间不可用的回落分支取配置值
- 缺省/留空：回落历史缺省 shared/data/permission_modes.json（默认行为不变）
- 用户空间可用：主落点仍是 user_data_dir（覆盖不越主分支，读写同源公理）
- apply_permission_modes_settings 重绑模块落点（读写同源单键，on_load 到站时序）
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_THIS_DIR = str(Path(__file__).resolve().parent)
_SHARED_DIR = str(Path(__file__).resolve().parents[1])  # plugins/shared/
if _SHARED_DIR not in sys.path:
    sys.path.insert(0, _SHARED_DIR)

_spec = importlib.util.spec_from_file_location(
    "security_check_plugin_modes_settings", str(Path(_THIS_DIR) / "plugin.py")
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

pytestmark = pytest.mark.unit


def _without_user_space(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 user_space 模块缺失（安装形态外的回落分支触发条件）。"""
    monkeypatch.setitem(sys.modules, "user_space", None)


def _with_user_space(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> None:
    """模拟用户空间可用（主落点分支）。"""
    fake = types.ModuleType("user_space")
    fake.user_data_dir = lambda: data_dir  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "user_space", fake)


class TestPermissionModesFileResolution:
    """回落落点配置化：settings.permission_modes_file 覆盖 ImportError 回退路径。"""

    def test_settings_override_wins_in_fallback_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户空间不可用 + 配置覆盖 → 落点取配置值（不再硬编码 shared/data/）。"""
        _without_user_space(monkeypatch)
        override = tmp_path / "custom" / "permission_modes.json"
        resolved = _mod._resolve_permission_modes_file(
            {"permission_modes_file": str(override)}
        )
        assert resolved == str(override)

    def test_no_settings_keeps_historical_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未注入 settings（None/缺键）→ 历史缺省 shared/data/（默认行为不变）。"""
        _without_user_space(monkeypatch)
        for settings in (None, {}, {"other_key": 1}):
            resolved = _mod._resolve_permission_modes_file(settings)
            assert resolved == str(
                Path(_mod._SHARED_ROOT) / "data" / "permission_modes.json"
            ), f"settings={settings!r} 应回落历史缺省"

    def test_blank_setting_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置留空（settings.yaml 缺省形态）→ 历史缺省，不吃空串当路径。"""
        _without_user_space(monkeypatch)
        resolved = _mod._resolve_permission_modes_file({"permission_modes_file": "  "})
        assert resolved == str(
            Path(_mod._SHARED_ROOT) / "data" / "permission_modes.json"
        )

    def test_user_space_primary_beats_settings(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户空间可用 → 主落点恒为 user_data_dir（配置只管回落分支）。"""
        data_dir = tmp_path / "user-data"
        _with_user_space(monkeypatch, data_dir)
        resolved = _mod._resolve_permission_modes_file(
            {"permission_modes_file": str(tmp_path / "override.json")}
        )
        assert resolved == str(data_dir / "permission_modes.json")


class TestApplySettingsRebindsModuleFile:
    """apply_permission_modes_settings：on_load 到站重解并重绑模块落点。"""

    def test_apply_rebinds_module_level_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """重绑后读写单键 _PERMISSION_MODES_FILE 指向配置落点；传 None 回缺省。"""
        _without_user_space(monkeypatch)
        override = tmp_path / "pm.json"
        _mod.apply_permission_modes_settings({"permission_modes_file": str(override)})
        assert _mod._PERMISSION_MODES_FILE == str(override)

        _mod.apply_permission_modes_settings(None)
        assert _mod._PERMISSION_MODES_FILE == str(
            Path(_mod._SHARED_ROOT) / "data" / "permission_modes.json"
        )

    def test_apply_with_user_space_available_is_noop_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户空间可用时 settings 不改落点（主分支优先，重绑仍得用户空间值）。"""
        data_dir = tmp_path / "ud"
        _with_user_space(monkeypatch, data_dir)
        _mod.apply_permission_modes_settings(
            {"permission_modes_file": str(tmp_path / "x.json")}
        )
        assert _mod._PERMISSION_MODES_FILE == str(data_dir / "permission_modes.json")


class TestSettingsManifestDeclaration:
    """manifest 声明面：settings config_files 条目与 yaml 文件互证（形状契约）。"""

    def test_settings_config_file_declared_and_present(self) -> None:
        import json  # noqa: PLC0415

        manifest = json.loads(
            (Path(_THIS_DIR) / "plugin.json").read_text(encoding="utf-8")
        )
        settings_entries = [
            cf for cf in manifest["config_files"] if cf.get("id") == "settings"
        ]
        assert len(settings_entries) == 1, "settings 命名空间必须且只需声明一次"
        rel = settings_entries[0]["path"]
        yaml_path = Path(_THIS_DIR).parents[2] / rel
        assert yaml_path.is_file(), f"声明路径不存在: {yaml_path}"
        assert "permission_modes_file" in yaml_path.read_text(encoding="utf-8")
