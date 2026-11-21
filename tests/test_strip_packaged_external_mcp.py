# @feature: FP-DEPLOY external MCP 受管生成目录出包剥离 | @ci: python-coverage
"""strip_packaged_external_mcp 行为测试：派生受管目录出包剥离。

覆盖：带 .mcp-managed 标记的 mcp_* 目录全剥离 / 手写 mcp_* 插件（无标记）
不动 / 非插件目录与普通插件不动 / 无受管目录幂等（退出 0）/ dry-run 只报告
不删除 / 资源根缺失退出 1。

背景（2026-11-20 启动优化 C）：受管目录是内核 discover 从 config/mcp.json
派生的生成物，dev 侧内核跑过即落进仓内插件树（未跟踪），extraResources 整
目录照搬把它打进安装包——装机 tier1 随包带陈旧派生副本，与用户根副本构成
同 id 双源抖动。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "strip_packaged_external_mcp", REPO / "scripts" / "strip_packaged_external_mcp.py"
)
assert _SPEC is not None
assert _SPEC.loader is not None
S = importlib.util.module_from_spec(_SPEC)
sys.modules["strip_packaged_external_mcp"] = S
_SPEC.loader.exec_module(S)

pytestmark = pytest.mark.unit


def _make_managed_dir(base: Path, name: str, with_marker: bool = True) -> Path:
    """构造 external MCP 受管目录形态（plugin.json + 可选 .mcp-managed 标记）。"""
    plugin = base / "plugins" / "shared" / name
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text('{"id": "%s"}' % name, encoding="utf-8")
    if with_marker:
        (plugin / ".mcp-managed").write_text("generated from config/mcp.json", encoding="utf-8")
    return plugin


def _make_product(root: Path) -> tuple[Path, Path, Path]:
    """最小产物：受管生成目录 + 手写 mcp_ 前缀插件 + 普通插件。"""
    managed = _make_managed_dir(root, "mcp_windows_mcp", with_marker=True)
    handmade = _make_managed_dir(root, "mcp_handmade", with_marker=False)
    normal = root / "plugins" / "shared" / "bash"
    normal.mkdir(parents=True)
    (normal / "plugin.json").write_text('{"id": "bash"}', encoding="utf-8")
    return managed, handmade, normal


def test_strips_managed_dirs_keeps_handmade(tmp_path):
    """带标记的受管目录全剥离；手写 mcp_ 前缀插件与普通插件不动。"""
    root = tmp_path / "res"
    managed, handmade, normal = _make_product(root)

    assert S.main([str(root)]) == 0
    assert not managed.exists(), "受管生成目录必须剥离（派生面不进包）"
    assert handmade.is_dir(), "手写 mcp_ 前缀插件（无标记）不动"
    assert (handmade / "plugin.json").is_file()
    assert normal.is_dir(), "普通插件不动"
    assert (root / "plugins" / "shared").is_dir(), "共享根本体不动"


def test_no_managed_dirs_idempotent(tmp_path):
    """产物内无受管目录：退出 0（幂等，剥离后复跑形态）。"""
    root = tmp_path / "res"
    (root / "plugins" / "shared" / "bash").mkdir(parents=True)
    assert S.main([str(root)]) == 0


def test_missing_shared_dir_idempotent(tmp_path):
    """resources 根在但无 plugins/shared：退出 0。"""
    root = tmp_path / "res"
    root.mkdir()
    assert S.main([str(root)]) == 0


def test_dry_run_keeps_dirs(tmp_path):
    """--dry-run 只报告不删除。"""
    root = tmp_path / "res"
    managed, handmade, _ = _make_product(root)

    assert S.main([str(root), "--dry-run"]) == 0
    assert managed.is_dir()
    assert (managed / ".mcp-managed").is_file()
    assert handmade.is_dir()


def test_missing_resources_root_fails(tmp_path):
    """资源根缺失退出 1。"""
    assert S.main([str(tmp_path / "nonexistent")]) == 1
