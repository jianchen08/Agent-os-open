# @feature: FP-MIGR 用户空间迁移 | @ci: python-coverage
"""scripts/migrate_to_user_root.py 插件迁移段（%APPDATA% → user_root）测试。

R158/BUG-48 缺口收口：脚本此前只覆盖仓内旧物（DB/data/.env/dirty config），
没有 ``%APPDATA%\\agentos\\plugins`` → ``user_root/plugins`` 的插件迁移路径，
六个用户插件丢失。本文件用 tmp 目录模拟两侧现状，断言行为契约：

- user_root 缺失的插件目录被迁，目录内 ``data/`` 用户数据随迁；
- user_root 已有同名目录不覆盖（保留 user_root 现状——它是当前活跃副本）；
- 排除项生效：``__pycache__``/``.venv``/``node_modules`` 不随迁（.venv 由 launcher
  uv sync 重建），``uv.lock`` 保留（可复现构建）；
- 链接项（junction/symlink，如旧布局的 ``_host``）跳过——copytree 解引用会把
  被指目录复制成实体树；
- ``--dry-run`` 只列计划不落盘；执行后输出迁移前后插件目录清单对比。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "migrate_to_user_root.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "migrate_to_user_root_under_test", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mig(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """加载被测脚本并把平台钉为 win32（%APPDATA% 源段仅 win32 生效）。"""
    module = _load_script()
    monkeypatch.setattr(sys, "platform", "win32")
    return module


@pytest.fixture
def legacy_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Path]:
    """模拟旧布局 %APPDATA%\\agentos\\plugins 与已钉定的 user_root/plugins。"""
    appdata = tmp_path / "appdata"
    user_root = tmp_path / "user_root"
    src = appdata / "agentos" / "plugins"
    dst = user_root / "plugins"
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    return {
        "tmp": tmp_path,
        "appdata": appdata,
        "user_root": user_root,
        "src": src,
        "dst": dst,
    }


def _write_plugin(
    root: Path,
    name: str,
    *,
    manifest_body: str = "legacy",
    with_data: str | None = None,
    with_uv_lock: bool = False,
    with_pycache: bool = False,
    with_venv: bool = False,
    with_node_modules: bool = False,
) -> Path:
    """在 root 下造一个最小插件目录，返回插件目录路径。"""
    pdir = root / name
    pdir.mkdir(parents=True)
    (pdir / "plugin.json").write_text(manifest_body, encoding="utf-8")
    if with_data is not None:
        (pdir / "data").mkdir()
        (pdir / "data" / "game_ledger.json").write_text(with_data, encoding="utf-8")
    if with_uv_lock:
        (pdir / "uv.lock").write_text("lock", encoding="utf-8")
    if with_pycache:
        (pdir / "__pycache__").mkdir()
        (pdir / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00")
    if with_venv:
        (pdir / ".venv").mkdir()
        (pdir / ".venv" / "pyvenv.cfg").write_text("home = x", encoding="utf-8")
    if with_node_modules:
        (pdir / "node_modules" / "pkg").mkdir(parents=True)
        (pdir / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    return pdir


def _plan(
    mig: ModuleType, dst: Path
) -> tuple[list[tuple[str, Path, Path]], list[str], list[str]]:
    result: tuple[list[tuple[str, Path, Path]], list[str], list[str]] = (
        mig._legacy_plugin_plan(dst)
    )
    return result


class TestLegacyPluginPlan:
    def test_missing_plugins_are_planned(self, mig: ModuleType, legacy_layout: dict) -> None:
        """user_root 缺失的插件目录进计划（两个有区分度输入：带 data 与纯清单）。"""
        _write_plugin(legacy_layout["src"], "mode_learning", with_data='{"xp": 1065}')
        _write_plugin(legacy_layout["src"], "omnisearch")
        # 目标侧独有插件：不参与计划（计划只看源侧条目），迁移后应原样保留
        _write_plugin(legacy_layout["dst"], "target_only")

        moves, skipped_existing, skipped_links = _plan(mig, legacy_layout["dst"])

        assert [m[0] for m in moves] == ["mode_learning", "omnisearch"]
        # 源指向 %APPDATA% 侧、目标指向 user_root 侧
        assert all(m[1] == legacy_layout["src"] / m[0] for m in moves)
        assert all(m[2] == legacy_layout["dst"] / m[0] for m in moves)
        assert skipped_existing == []
        assert skipped_links == []

    def test_existing_plugin_dirs_not_in_moves(self, mig: ModuleType, legacy_layout: dict) -> None:
        """user_root 已有同名目录不进计划（方向口径：保留 user_root 现状）。"""
        _write_plugin(legacy_layout["src"], "godot_mcp", manifest_body="legacy")
        _write_plugin(legacy_layout["src"], "omnisearch", manifest_body="legacy-2")
        _write_plugin(legacy_layout["dst"], "godot_mcp", manifest_body="user-current")
        _write_plugin(legacy_layout["dst"], "omnisearch", manifest_body="user-current-2")

        moves, skipped_existing, skipped_links = _plan(mig, legacy_layout["dst"])

        assert moves == []
        assert skipped_existing == ["godot_mcp", "omnisearch"]
        assert skipped_links == []

    def test_source_missing_yields_no_plan(self, mig: ModuleType, legacy_layout: dict) -> None:
        """旧布局不存在（从无 %APPDATA%\\agentos\\plugins）→ 无计划。"""
        assert _plan(mig, legacy_layout["dst"]) == ([], [], [])

    def test_source_equals_target_yields_no_plan(self, mig: ModuleType, legacy_layout: dict) -> None:
        """源即目标（AGENTOS_USER_ROOT 未钉、user_root 仍指 %APPDATA%）→ 无计划。"""
        legacy_layout["src"].mkdir(parents=True)
        _write_plugin(legacy_layout["src"], "mode_learning")
        assert _plan(mig, legacy_layout["src"]) == ([], [], [])

    def test_non_windows_has_no_legacy_source(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        module = _load_script()
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
        assert module._legacy_plugins_source() is None

    def test_junction_entry_skipped(self, mig: ModuleType, legacy_layout: dict) -> None:
        """链接项（旧布局 plugins/_host 是 junction）跳过，不解引用成实体树。"""
        _write_plugin(legacy_layout["src"], "mode_learning")
        host_target = legacy_layout["tmp"] / "shared_host"
        host_target.mkdir()
        link = legacy_layout["src"] / "_host"
        try:
            import _winapi  # noqa: PLC0415 - 仅 Windows 存在

            _winapi.CreateJunction(str(host_target), str(link))
        except (ImportError, OSError):
            try:
                link.symlink_to(host_target, target_is_directory=True)
            except (OSError, NotImplementedError):
                pytest.skip("当前环境不能创建 junction/symlink")

        moves, skipped_existing, skipped_links = _plan(mig, legacy_layout["dst"])

        assert [m[0] for m in moves] == ["mode_learning"]
        assert skipped_existing == []
        assert skipped_links == ["_host"]


class TestMigratePluginDirs:
    def test_missing_migrated_with_data(self, mig: ModuleType, legacy_layout: dict, capsys: pytest.CaptureFixture[str]) -> None:
        """缺失插件被迁：data/ 用户数据随迁；输出迁移前后清单核验。"""
        _write_plugin(
            legacy_layout["src"],
            "mode_learning",
            with_data='{"xp": 1065, "settles": 3}',
        )
        _write_plugin(legacy_layout["src"], "video_gen")
        legacy_layout["dst"].mkdir(parents=True)
        moves = [
            ("mode_learning", legacy_layout["src"] / "mode_learning", legacy_layout["dst"] / "mode_learning"),
            ("video_gen", legacy_layout["src"] / "video_gen", legacy_layout["dst"] / "video_gen"),
        ]

        mig._migrate_plugin_dirs(moves, legacy_layout["dst"])

        ledger = legacy_layout["dst"] / "mode_learning" / "data" / "game_ledger.json"
        assert json.loads(ledger.read_text(encoding="utf-8")) == {"xp": 1065, "settles": 3}
        assert (legacy_layout["dst"] / "video_gen" / "plugin.json").is_file()
        # 性质：目标清单 ⊇ 本次迁移名集
        after = {p.name for p in legacy_layout["dst"].iterdir()}
        assert {"mode_learning", "video_gen"} <= after
        out = capsys.readouterr().out
        assert "迁移前" in out
        assert "迁移后" in out
        assert "mode_learning" in out

    def test_migrates_into_plugins_dir_that_does_not_exist_yet(self, mig: ModuleType, legacy_layout: dict) -> None:
        """全新 user_root（plugins 目录尚不存在）也能落位。"""
        _write_plugin(legacy_layout["src"], "video_gen")
        assert not legacy_layout["dst"].exists()

        mig._migrate_plugin_dirs(
            [("video_gen", legacy_layout["src"] / "video_gen", legacy_layout["dst"] / "video_gen")],
            legacy_layout["dst"],
        )

        assert (legacy_layout["dst"] / "video_gen" / "plugin.json").is_file()

    def test_exclusions_and_retentions(self, mig: ModuleType, legacy_layout: dict) -> None:
        """__pycache__/.venv/node_modules 不随迁（任意深度）；uv.lock 与 data/ 保留。"""
        _write_plugin(
            legacy_layout["src"],
            "mode_learning",
            with_data="{}",
            with_uv_lock=True,
            with_pycache=True,
            with_venv=True,
            with_node_modules=True,
        )
        legacy_layout["dst"].mkdir(parents=True)

        mig._migrate_plugin_dirs(
            [("mode_learning", legacy_layout["src"] / "mode_learning", legacy_layout["dst"] / "mode_learning")],
            legacy_layout["dst"],
        )

        target = legacy_layout["dst"] / "mode_learning"
        assert (target / "uv.lock").is_file()
        assert (target / "data" / "game_ledger.json").is_file()
        # 性质：排除在整棵子树生效，不只在顶层
        for p in target.rglob("*"):
            assert p.name not in {"__pycache__", ".venv", "node_modules"}


class TestMainDryRun:
    def test_dry_run_lists_plugin_plan_and_writes_nothing(
        self, mig: ModuleType, legacy_layout: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_plugin(legacy_layout["src"], "mode_learning", with_data="{}")
        _write_plugin(legacy_layout["src"], "stream_control")
        _write_plugin(legacy_layout["src"], "omnisearch", manifest_body="legacy")
        _write_plugin(legacy_layout["dst"], "omnisearch", manifest_body="user")
        monkeypatch.setattr(sys, "argv", ["migrate_to_user_root.py", "--dry-run"])

        assert mig.main() == 0

        out = capsys.readouterr().out
        assert str(legacy_layout["src"]) in out
        assert "mode_learning" in out
        assert "stream_control" in out
        assert "omnisearch" in out  # 已存在跳过项也列出
        # 不落盘：目标只含原有插件
        assert [p.name for p in legacy_layout["dst"].iterdir()] == ["omnisearch"]


class TestMainExecute:
    def test_execute_migrates_missing_and_keeps_existing(
        self, mig: ModuleType, legacy_layout: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """全量 main()：缺失插件（含 data、排除项）落位，已有插件保留现状，旧源保留。"""
        _write_plugin(
            legacy_layout["src"],
            "mode_learning",
            with_data='{"xp": 1065}',
            with_pycache=True,
        )
        _write_plugin(legacy_layout["src"], "godot_mcp", manifest_body="legacy")
        _write_plugin(legacy_layout["dst"], "godot_mcp", manifest_body="user-current")
        monkeypatch.setattr(sys, "argv", ["migrate_to_user_root.py"])

        assert mig.main() == 0

        target = legacy_layout["dst"] / "mode_learning"
        assert (target / "data" / "game_ledger.json").is_file()
        assert not (target / "__pycache__").exists()
        # 已有插件保留 user_root 现状（不被旧副本覆盖——R158 角色卡回退教训）
        assert (
            (legacy_layout["dst"] / "godot_mcp" / "plugin.json").read_text(encoding="utf-8")
            == "user-current"
        )
        # 旧源保留（copy 而非 move，可回滚）
        assert (legacy_layout["src"] / "mode_learning" / "plugin.json").is_file()
        out = capsys.readouterr().out
        assert "godot_mcp" in out
