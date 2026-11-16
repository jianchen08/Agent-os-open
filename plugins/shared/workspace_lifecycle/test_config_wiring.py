# @feature: FP-0.2.〇 任务执行驱动 | @vision: V3 可嵌入 | @ci: python-coverage
"""workspace_lifecycle 配置接线测试（manifest 声明 → manager 消费）。

锁 2026-10-26 修复的接线断点：插件的 manager 消费键是
``config["workspace_config"]``（kernel build_injected_config 以
manifest ``config_files[].id`` 为键注入 yaml 内容），但 manifest 此前
未声明 config_files——manager 恒收空 dict，
``_link_worktree_dependencies`` 的 ``worktree_link_patterns`` 恒空
静默早退，任务 worktree 永远零依赖（每次 pnpm install 冷起，
2026-10-26 任务 1080ba04240c 冷启动实证）。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_SHARED_ROOT = _PLUGIN_DIR.parents[0]  # plugins/shared（isolation_path 等平铺裸模块解析根）
_REPO_ROOT = _PLUGIN_DIR.parents[2]  # 仓库根（config/ 真值文件定位）
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

_MOD_NAME = "workspace_lifecycle_wiring_test"


def _load_module() -> Any:
    if _MOD_NAME in sys.modules:
        return sys.modules[_MOD_NAME]
    spec = importlib.util.spec_from_file_location(_MOD_NAME, _PLUGIN_DIR / "plugin.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MOD_NAME] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[_MOD_NAME]
        raise
    return module


def _manifest() -> dict[str, Any]:
    return json.loads((_PLUGIN_DIR / "plugin.json").read_text(encoding="utf-8"))


def _workspace_config_entry() -> dict[str, Any]:
    entries = {cf["id"]: cf for cf in _manifest().get("config_files") or []}
    assert "workspace_config" in entries, (
        "manifest 未声明 config_files[id=workspace_config]——"
        "plugin.py _get_manager 的消费键拿不到注入，worktree 依赖链接永不生效"
    )
    return entries["workspace_config"]


def test_manifest_declares_workspace_config_wired_to_isolation_yaml() -> None:
    """引用形态声明指向真实存在的隔离配置，且其内含链接模式真值。"""
    entry = _workspace_config_entry()
    ref = _REPO_ROOT / entry["path"]
    assert ref.exists(), f"config_files 指向的文件不存在: {ref}"
    data = yaml.safe_load(ref.read_text(encoding="utf-8"))
    patterns = (data or {}).get("workspace", {}).get("worktree_link_patterns") or []
    assert "node_modules" in patterns
    assert "frontend/node_modules" in patterns


def test_injected_config_reaches_manager_and_links_runtime_deps(tmp_path: Path) -> None:
    """内核注入形（id 键 → yaml 内容）贯通 manager，真 worktree 链接可透明访问。"""
    import isolation_path

    isolation_path.ensure_isolation_path()
    mod = _load_module()

    entry = _workspace_config_entry()
    ref = _REPO_ROOT / entry["path"]
    injected = {"workspace_config": yaml.safe_load(ref.read_text(encoding="utf-8"))}
    plugin = mod.WorkspaceLifecyclePlugin(config=injected)
    manager = plugin._get_manager(base_path_hint=str(tmp_path))
    assert manager is not None, "manager 实例化失败，配置链无从谈起"
    patterns = (manager._config.get("workspace") or {}).get("worktree_link_patterns") or []
    assert patterns, "manager 未拿到 worktree_link_patterns——配置链仍断"

    # 源仓：tracked 文件 + gitignored 形态的 frontend/node_modules（运行时依赖）
    repo = tmp_path / "proj"
    (repo / "frontend" / "node_modules" / "demo").mkdir(parents=True)
    (repo / "frontend" / "node_modules" / "demo" / "marker.txt").write_text(
        "x", encoding="utf-8"
    )

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    (repo / "README.md").write_text("r", encoding="utf-8")
    git("add", "README.md")
    git("-c", "user.name=t", "-c", "user.email=t@example", "commit", "-qm", "init")

    ws_dir = tmp_path / "ws" / "wt_wiring"
    manager._worktree_add_with_repair(repo, "task/wiringtest", ws_dir, "wiringtest")
    linked = ws_dir / "frontend" / "node_modules" / "demo" / "marker.txt"
    assert linked.exists(), "worktree 内读不到链接的 node_modules——链接未创建"
