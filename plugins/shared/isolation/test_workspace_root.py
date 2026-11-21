# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""isolation 插件（工作空间基目录解析）单元测试。

覆盖（对齐 plugins/shared/isolation/workspace.py 2026-08-24 收口）：
1. get_workspace_base_dir：配置驱动——isolation_config.yaml 的 workspace.root
   （绝对路径原样 / 相对路径相对项目根 / 配置缺失回退 .ai_workspaces）
2. _isolation_config_path：AGENTOS_CONFIG_ROOT 优先，回退祖先目录查找
   （不硬编码父目录层数；sidecar venv 无 config.config_center，直读会断，
   文件回退是主读路径）
3. 找不到配置时缺省回退，不 panic

测试不依赖真实内核——直接加载 workspace.py。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 仓库根 = 插件目录向上 4 层（plugins/shared/isolation → 仓库根）
_REPO_ROOT = _PLUGIN_DIR.parents[2]
_CFG_FILE = _REPO_ROOT / "config" / "plugins" / "isolation" / "isolation_config.yaml"


def _load_ws(mod_name: str = "isolation_workspace_root_test", source: Path | None = None) -> Any:
    """动态加载 workspace.py（每次新建，隔离模块级状态）。

    Args:
        mod_name: 模块名（同一次运行内不同临时副本用不同名字，避免 sys.modules 串扰）
        source: 源文件路径，缺省为真实插件目录的 workspace.py
    """
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    src = source or (_PLUGIN_DIR / "workspace.py")
    spec = importlib.util.spec_from_file_location(mod_name, src)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_MOD = _load_ws()
_default_ws_root = _MOD._DEFAULT_WORKSPACE_ROOT
_load_isolation_config = _MOD._load_isolation_config
_isolation_config_path = _MOD._isolation_config_path
get_workspace_config_root = _MOD.get_workspace_config_root
get_workspace_base_dir = _MOD.get_workspace_base_dir
find_project_root = _MOD.find_project_root


class TestIsolationConfigPath:
    def test_resolves_to_repo_root_config_file(self) -> None:
        """定位函数返回的路径存在且指向仓库根 config/plugins/isolation/isolation_config.yaml。"""
        path = _isolation_config_path()
        assert path.exists(), f"配置文件不存在: {path}"
        assert path == _CFG_FILE, f"期望 {_CFG_FILE}，实际 {path}"

    def test_ancestor_walk_not_hardcoded(self) -> None:
        """路径通过祖先目录查找得到（不依赖固定父目录层数）。"""
        path = _isolation_config_path()
        # 仓库根是包含 config/plugins/isolation/isolation_config.yaml 的祖先目录
        assert _REPO_ROOT in path.parents
        assert path.parent == _REPO_ROOT / "config" / "plugins" / "isolation"

    def test_env_override_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT 指向的配置根优先于祖先目录推导。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "config"))
        path = _isolation_config_path()
        assert path == _CFG_FILE
        assert path.exists()

    def test_env_override_missing_file_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT 指向的路径不存在时回退祖先目录查找。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "nonexistent-config-root"))
        path = _isolation_config_path()
        assert path == _CFG_FILE
        assert path.exists()

    def test_no_ancestor_found_returns_fallback_without_panic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """任何祖先目录都找不到时返回推导路径（加载失败走缺省回退，不 panic）。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "nonexistent-config-root"))
        # 模拟 workspace.py 位于无 config/plugins/isolation 祖先的目录：用临时目录重新加载模块
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_plugin_dir = Path(tmp) / "plugins" / "shared" / "system" / "isolation"
            fake_plugin_dir.mkdir(parents=True)
            # 复制 workspace.py 到临时目录（无依赖的同目录平铺模块）
            (fake_plugin_dir / "workspace.py").write_text(
                (_PLUGIN_DIR / "workspace.py").read_text(encoding="utf-8"), encoding="utf-8"
            )
            module = _load_ws("isolation_workspace_root_fallback_test", fake_plugin_dir / "workspace.py")

            path = module._isolation_config_path()
            # 不 panic，返回推导路径（不存在也允许——调用方降级缺省）
            assert isinstance(path, Path)
            assert not path.exists()

            # 加载失败 → get_workspace_base_dir 回退缺省 .ai_workspaces（相对项目根）
            base = module.get_workspace_base_dir()
            assert str(base).endswith(_default_ws_root), f"缺省基目录应为 .ai_workspaces 结尾，实际: {base}"


class TestWorkspaceConfigRoot:
    def test_default_when_config_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置读取失败（无祖先配置）时返回缺省值。"""
        # 用临时目录加载的子模块验证：其 _isolation_config_path 指向不存在的路径
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            fake_plugin_dir = Path(tmp) / "plugins" / "shared" / "system" / "isolation"
            fake_plugin_dir.mkdir(parents=True)
            (fake_plugin_dir / "workspace.py").write_text(
                _PLUGIN_DIR.joinpath("workspace.py").read_text(encoding="utf-8"), encoding="utf-8"
            )
            module = _load_ws("isolation_workspace_root_missing_test", fake_plugin_dir / "workspace.py")
            assert module.get_workspace_config_root() == _default_ws_root


class TestFindProjectRoot:
    def test_finds_repo_root(self) -> None:
        """祖先查找定位仓库根（含 config/plugins/isolation/ 的祖先目录）。"""
        root = find_project_root()
        assert (root / "config" / "plugins" / "isolation" / "isolation_config.yaml").exists()
        assert root == _REPO_ROOT, f"期望 {_REPO_ROOT}，实际 {root}"

    def test_env_config_root_parent_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENTOS_CONFIG_ROOT 的父目录 = 项目根，优先于祖先推导。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(_REPO_ROOT / "config"))
        assert find_project_root() == _REPO_ROOT


class TestRelativeRootAnchor:
    """相对 root 锚点（配置根驱动，单锚点）。

    契约（ADR 2026-11-07 §2.5：双源仲裁环境变量退役，形态感知随之收敛）：
    - AGENTOS_CONFIG_ROOT 指向的配置根含 plugins/isolation/isolation_config.yaml
      时，锚点 = 配置根父目录——dev（配置根=仓内 config）锚项目根；装机
      （配置根=用户空间 config）锚用户空间根（可写、升级不丢、卸载保留）；
    - 配置根无 isolation 配置 → 沿祖先目录链找仓根（既有回退）；
    - 绝对 root 原样使用（用户显式配置尊重）。
    """

    def test_relative_root_anchors_config_root_parent_when_seeded(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """配置根含 isolation 配置 → 锚配置根父目录（装机=用户空间根的解析形）。"""
        cfg_root = tmp_path / "uroot" / "config"
        iso = cfg_root / "plugins" / "isolation"
        iso.mkdir(parents=True)
        (iso / "isolation_config.yaml").write_text("workspace:\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(cfg_root))
        mod = _load_ws()
        monkeypatch.setattr(mod, "_load_isolation_config", lambda: {"workspace": {"root": "my_ws"}})
        assert mod.get_workspace_base_dir() == tmp_path / "uroot" / "my_ws"

    def test_relative_root_ancestor_chain_when_config_root_not_seeded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """配置根不含 isolation 配置 → 走祖先目录链回退（仓根形态）。"""
        monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_empty_dir()))
        mod = _load_ws()
        monkeypatch.setattr(mod, "_load_isolation_config", lambda: {"workspace": {"root": "my_ws"}})
        assert mod.get_workspace_base_dir() == mod.find_project_root() / "my_ws"

    def test_absolute_root_used_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """绝对 root 原样使用（用户显式配置尊重，不切锚）。"""
        mod = _load_ws()
        monkeypatch.setattr(
            mod, "_load_isolation_config", lambda: {"workspace": {"root": "D:/some/abs/dir"}}
        )
        assert mod.get_workspace_base_dir() == Path("D:/some/abs/dir")


def tmp_empty_dir() -> str:
    """配置根替身：真实存在但无 isolation 配置的空目录。"""
    import tempfile

    return tempfile.mkdtemp(prefix="ws-anchor-cfg-")


class TestWorkspaceBaseDir:
    def test_absolute_root_used_as_is(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置 root 为绝对路径 → 原样返回（不拼项目根）。"""
        mod = _load_ws()
        monkeypatch.setattr(
            mod, "_load_isolation_config", lambda: {"workspace": {"root": "D:/some/abs/dir"}}
        )
        assert mod.get_workspace_base_dir() == Path("D:/some/abs/dir")

    def test_relative_root_resolved_against_project_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置 root 为相对路径 → 相对项目根解析（不是 cwd）。"""
        mod = _load_ws()
        monkeypatch.setattr(
            mod, "_load_isolation_config", lambda: {"workspace": {"root": "my_ws"}}
        )
        assert mod.get_workspace_base_dir() == mod.find_project_root() / "my_ws"

    def test_missing_config_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """配置缺失 → 缺省 .ai_workspaces（相对项目根）。"""
        mod = _load_ws()
        monkeypatch.setattr(mod, "_load_isolation_config", lambda: {})
        assert mod.get_workspace_base_dir() == mod.find_project_root() / _default_ws_root

    def test_config_driven_change_reflected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """mock/临时配置改 root → 解析结果跟着变（配置驱动生效）。"""
        mod = _load_ws()
        for root_val, expected_name in (
            ("tmp_ws", "tmp_ws"),
            ("other_ws", "other_ws"),
        ):
            monkeypatch.setattr(
                mod, "_load_isolation_config", lambda rv=root_val: {"workspace": {"root": rv}}
            )
            base = Path(mod.get_workspace_base_dir())
            assert base.name == expected_name, f"root={root_val} 时应解析到 {expected_name}，实际 {base}"

    def test_real_repo_config_is_loaded(self) -> None:
        """真身仓库配置可被读到：解析值与磁盘 isolation_config.yaml 的 workspace.root 一致。"""
        import yaml

        raw = yaml.safe_load(_CFG_FILE.read_text(encoding="utf-8")) or {}
        expected = str((raw.get("workspace") or {}).get("root") or _default_ws_root)
        assert get_workspace_config_root() == expected, (
            "get_workspace_config_root 应等于磁盘配置的 workspace.root（文件回退链路通；"
            f"磁盘值={expected!r}，实际={get_workspace_config_root()!r}）"
        )
        base = get_workspace_base_dir()
        assert Path(base).is_absolute(), f"基目录应为绝对路径，实际: {base}"
        expected_path = Path(expected)
        if not expected_path.is_absolute():
            assert Path(base) == _REPO_ROOT / expected_path, (
                f"基目录应相对仓库根解析到 {expected}，实际: {base}"
            )
        else:
            assert Path(base) == expected_path, (
                f"绝对 root 应原样使用，实际: {base}"
            )
