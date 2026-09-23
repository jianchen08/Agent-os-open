# @feature: FP-MIGR 用户空间迁移 | @ci: python-coverage
"""scripts/sync_user_root.py 单向同步契约测试（repo plugins/shared → user_root/plugins）。

用户裁决（2026-09-19）：开发者只改仓内，仓内更新自动到用户空间；方向**单向**
（repo→user_root），绝不反向覆盖、绝不删除 user_root 独有内容（R158 教训——
用户自有插件/用户数据是活跃副本，不是仓的从属物）。本文件用 tmp 目录模拟
双侧现状，断言行为契约：

- user_root 缺失的插件目录整目录复制（覆盖顶层 ``shared/<n>``、
  ``system|tools/<n>``、``pipeline/<phase>/<n>`` 三种布局），排
  ``__pycache__``/``.venv``；
- 增量覆盖只发生在**仓内较新**的文件上（mtime/size）；用户侧较新文件不被
  仓内旧版本冲掉（单向性）；
- 永不删除：用户独有文件/子目录/独有插件（如 mode_learning）一律保留，
  仓内已删的文件也不得反向删除；
- 幂等：重复执行零复制、mtime 不变；
- 不碰插件根之外的用户内容（config/data/.env）；
- 目标为 junction/symlink 的插件跳过（不写入链接目标树）；
- **写目标必须显式**（``--user-root`` > ``AGENTOS_USER_PLUGINS_DIR`` >
  ``AGENTOS_USER_ROOT``）：均未给定时拒绝执行（OS 默认空间是休眠旧用户空间
  + 装机版用户空间，静默写入违反装机/开发零共享）；``--dry-run`` 不受限，
  可按 OS 默认位置展示计划且不落盘。
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "sync_user_root.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "sync_user_root_under_test", _SCRIPT_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def sync_mod() -> ModuleType:
    return _load_script()


@pytest.fixture
def sides(tmp_path: Path) -> dict[str, Path]:
    """模拟仓内 plugins/shared 与 user_root/plugins 两侧根。"""
    repo_shared = tmp_path / "repo" / "plugins" / "shared"
    user_plugins = tmp_path / "user_root" / "plugins"
    repo_shared.mkdir(parents=True)
    user_plugins.mkdir(parents=True)
    return {"repo": repo_shared, "user": user_plugins}


def _write(path: Path, content: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------
# 缺失插件目录：整目录复制 + 排除项 + 三种布局发现
# ---------------------------------------------------------------


def test_missing_plugin_dirs_copied_whole_across_layouts(
    sync_mod: ModuleType, sides: dict[str, Path]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "db_admin" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "tools" / "file_read" / "plugin.json", "{}")
    _write(repo / "pipeline" / "input" / "context_build" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "VERSION = 1")
    _write(repo / "system" / "llm" / "sub" / "impl.py", "IMPL = 1")
    _write(repo / "system" / "llm" / "__pycache__" / "core.pyc", "junk")
    _write(repo / "system" / "llm" / ".venv" / "bin" / "python", "junk")

    outcome = sync_mod.sync_repo_plugins_to_user_root(repo, user)

    for rel in (
        "db_admin/plugin.json",
        "llm/plugin.json",
        "file_read/plugin.json",
        "context_build/plugin.json",
        "llm/core.py",
        "llm/sub/impl.py",
    ):
        assert (user / rel).is_file(), rel
    assert (user / "llm" / "core.py").read_text(encoding="utf-8") == "VERSION = 1"
    assert (user / "llm" / "sub" / "impl.py").read_text(encoding="utf-8") == "IMPL = 1"
    # 排除项按名字精确匹配，全树生效
    assert not (user / "llm" / "__pycache__").exists()
    assert not (user / "llm" / ".venv").exists()
    assert outcome.copied, "首次同步必须发生实际复制"


def test_discovery_finds_only_plugin_marker_dirs(
    sync_mod: ModuleType, sides: dict[str, Path]
) -> None:
    repo = sides["repo"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "data" / "nested.json", "{}")
    _write(repo / "tools" / "plain_dir" / "readme.md", "no marker")
    _write(repo / "atomic_io.py", "shared module, not a plugin")
    # 深度上限：第 4 层的 plugin.json 不在发现范围（shared/pipeline/<phase>/<n> 为最深）
    _write(repo / "deep" / "a" / "b" / "c" / "plugin.json", "{}")
    # 隐藏目录与排除项不下钻，其内标记文件不算插件根
    _write(repo / "system" / ".hidden" / "plugin.json", "{}")
    _write(repo / "system" / "__pycache__" / "plugin.json", "{}")

    found = {p.relative_to(repo).as_posix() for p in sync_mod.iter_plugin_dirs(repo)}
    assert found == {"system/llm"}


def test_sync_bootstraps_missing_user_plugins_root(
    sync_mod: ModuleType, sides: dict[str, Path]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "VERSION = 1")

    outcome = sync_mod.sync_repo_plugins_to_user_root(repo, user / "plugins")

    assert (user / "plugins" / "llm" / "core.py").is_file()
    assert outcome.copied


# ---------------------------------------------------------------
# 增量覆盖：仓内较新覆盖；仓内较旧不冲掉用户侧较新（单向）
# ---------------------------------------------------------------


def test_repo_newer_file_overwrites_user_copy(
    sync_mod: ModuleType, sides: dict[str, Path]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "VERSION = 2", mtime=2_000)
    _write(user / "llm" / "core.py", "VERSION = 1", mtime=1_000)

    sync_mod.sync_repo_plugins_to_user_root(repo, user)

    assert (user / "llm" / "core.py").read_text(encoding="utf-8") == "VERSION = 2"
    # copy2 保留 mtime：同步后两侧时间一致（幂等前提）
    assert (user / "llm" / "core.py").stat().st_mtime == pytest.approx(2_000)


def test_user_newer_file_survives_older_repo(
    sync_mod: ModuleType, sides: dict[str, Path]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "repo old", mtime=1_000)
    _write(user / "llm" / "core.py", "user fix", mtime=2_000)
    # 同 size 同 mtime 的文件不重拷（增量的判定是 size 或 mtime 任一差异）
    _write(repo / "system" / "llm" / "same.bin", "xxxx", mtime=1_000)
    _write(user / "llm" / "same.bin", "xxxx", mtime=1_000)

    outcome = sync_mod.sync_repo_plugins_to_user_root(repo, user)

    assert (user / "llm" / "core.py").read_text(encoding="utf-8") == "user fix"
    assert not any("core.py" in c for c in outcome.copied)
    assert not any("same.bin" in c for c in outcome.copied)


# ---------------------------------------------------------------
# 永不删除：用户独有内容一律保留
# ---------------------------------------------------------------


def test_user_only_content_never_deleted(
    sync_mod: ModuleType, sides: dict[str, Path]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "VERSION = 1")
    # 用户侧：独有文件、独有子目录、独有插件（mode_learning 形态）、
    # 以及仓内已删除的文件
    _write(user / "llm" / "user_notes.md", "user's own")
    _write(user / "llm" / "data" / "game_ledger.json", "{}")
    _write(user / "mode_learning" / "plugin.json", "{}")
    _write(user / "mode_learning" / "data" / "progress.json", "{}")
    _write(user / "llm" / "removed_in_repo.py", "orphan")

    sync_mod.sync_repo_plugins_to_user_root(repo, user)

    assert (user / "llm" / "user_notes.md").read_text(encoding="utf-8") == "user's own"
    assert (user / "llm" / "data" / "game_ledger.json").is_file()
    assert (user / "mode_learning" / "plugin.json").is_file()
    assert (user / "mode_learning" / "data" / "progress.json").is_file()
    assert (user / "llm" / "removed_in_repo.py").is_file()
    # 同步在用户插件根内进行，且只增不改删
    assert all(Path(c).is_relative_to(user) for c in sync_mod.sync_repo_plugins_to_user_root(repo, user).copied)


def test_non_plugin_user_content_untouched(
    sync_mod: ModuleType, tmp_path: Path
) -> None:
    user_root = tmp_path / "user_root"
    repo_shared = tmp_path / "repo" / "plugins" / "shared"
    repo_shared.mkdir(parents=True)
    (user_root / "plugins").mkdir(parents=True)
    _write(repo_shared / "system" / "llm" / "plugin.json", "{}")
    env_txt = _write(user_root / ".env", "KEY=secret")
    cfg = _write(user_root / "config" / "agents" / "a.yaml", "x: 1")
    dat = _write(user_root / "data" / "t1" / "db.sqlite", "bytes")

    sync_mod.sync_repo_plugins_to_user_root(repo_shared, user_root / "plugins")

    assert env_txt.read_text(encoding="utf-8") == "KEY=secret"
    assert cfg.read_text(encoding="utf-8") == "x: 1"
    assert dat.is_file()


# ---------------------------------------------------------------
# 幂等：重复执行零副作用
# ---------------------------------------------------------------


def test_second_run_is_noop(sync_mod: ModuleType, sides: dict[str, Path]) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "VERSION = 1", mtime=1_000)

    first = sync_mod.sync_repo_plugins_to_user_root(repo, user)
    assert first.copied
    mtime_after_first = (user / "llm" / "core.py").stat().st_mtime

    second = sync_mod.sync_repo_plugins_to_user_root(repo, user)

    assert second.copied == []
    assert (user / "llm" / "core.py").stat().st_mtime == mtime_after_first


# ---------------------------------------------------------------
# 安全守卫：链接目标、源即目标、同名冲突
# ---------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="junction 探测仅 win32 有意义")
def test_junction_target_plugin_skipped(
    sync_mod: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_shared = tmp_path / "repo" / "plugins" / "shared"
    user_plugins = tmp_path / "user_root" / "plugins"
    elsewhere = tmp_path / "elsewhere"
    repo_shared.mkdir(parents=True)
    user_plugins.mkdir(parents=True)
    elsewhere.mkdir()
    _write(repo_shared / "system" / "linked" / "plugin.json", "{}")
    _write(repo_shared / "system" / "linked" / "core.py", "repo")
    sentinel = _write(elsewhere / "sentinel.txt", "do not touch")
    link = user_plugins / "linked"
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(elsewhere)],
        capture_output=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"junction 创建失败：{proc.stderr!r}")

    rc = sync_mod.main(
        ["--repo-root", str(repo_shared.parents[1]), "--user-root", str(user_plugins.parents[0])]
    )

    assert rc == 0
    assert "linked" in capsys.readouterr().out
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert not (elsewhere / "core.py").exists()
    outcome = sync_mod.sync_repo_plugins_to_user_root(repo_shared, user_plugins)
    assert outcome.link_skipped == ["linked"]


def test_same_root_is_noop(sync_mod: ModuleType, sides: dict[str, Path]) -> None:
    outcome = sync_mod.sync_repo_plugins_to_user_root(sides["repo"], sides["repo"])
    assert outcome.copied == []


def test_duplicate_plugin_name_synced_once(
    sync_mod: ModuleType, sides: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "dup" / "plugin.json", "{}")
    _write(repo / "tools" / "dup" / "plugin.json", "{}")

    rc = sync_mod.main(
        ["--repo-root", str(repo.parents[1]), "--user-root", str(user.parents[0])]
    )

    assert rc == 0
    assert (user / "dup" / "plugin.json").is_file()
    assert "跨类别同名" in capsys.readouterr().out


# ---------------------------------------------------------------
# main()：CLI 端到端与失败路径
# ---------------------------------------------------------------


def test_main_explicit_dirs_syncs_and_logs(
    sync_mod: ModuleType, sides: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "VERSION = 1")

    argv = ["--repo-root", str(repo.parents[1]), "--user-root", str(user.parents[0])]
    rc = sync_mod.main(argv)

    assert rc == 0
    out = capsys.readouterr().out
    assert "core.py" in out
    assert "llm" in out

    rc = sync_mod.main(argv)

    assert rc == 0
    assert "无文件变动" in capsys.readouterr().out


def test_main_resolves_user_plugins_dir_env(
    sync_mod: ModuleType,
    sides: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, user = sides["repo"], sides["user"]
    monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
    monkeypatch.setenv("AGENTOS_USER_PLUGINS_DIR", str(user))
    _write(repo / "system" / "llm" / "plugin.json", "{}")

    rc = sync_mod.main(["--repo-root", str(repo.parents[1])])

    assert rc == 0
    assert (user / "llm" / "plugin.json").is_file()


def test_main_resolves_user_root_from_env(
    sync_mod: ModuleType,
    sides: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, user = sides["repo"], sides["user"]
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user.parents[0]))
    _write(repo / "system" / "llm" / "plugin.json", "{}")

    rc = sync_mod.main(["--repo-root", str(repo.parents[1])])

    assert rc == 0
    assert (user / "llm" / "plugin.json").is_file()


def test_main_refuses_write_without_explicit_target(
    sync_mod: ModuleType,
    sides: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """裸跑必须拒绝：即使 OS 默认空间可得也不得静默写入（装机/开发零共享）。"""
    repo = sides["repo"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    os_default = tmp_path / "appdata" / "agentos"
    monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
    monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))

    rc = sync_mod.main(["--repo-root", str(repo.parents[1])])

    assert rc == 1
    out = capsys.readouterr().out
    assert "AGENTOS_USER_ROOT" in out
    assert "--user-root" in out
    # 拒绝执行 = 零写入（OS 默认空间的 plugins 根不得被创建）
    assert not (os_default / "plugins").exists()


def test_dry_run_previews_without_explicit_target(
    sync_mod: ModuleType,
    sides: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run 不受限：无显式目标时按 OS 默认位置展示计划，不落盘。"""
    repo = sides["repo"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "VERSION = 1")
    appdata = tmp_path / "appdata"
    monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
    monkeypatch.delenv("AGENTOS_USER_PLUGINS_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))

    rc = sync_mod.main(["--repo-root", str(repo.parents[1]), "--dry-run"])

    assert rc == 0
    out = capsys.readouterr().out
    assert str(appdata / "agentos" / "plugins") in out
    assert "core.py" in out
    assert not (appdata / "agentos" / "plugins" / "llm").exists()


def test_dry_run_lists_plan_without_writing(
    sync_mod: ModuleType, sides: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "core.py", "VERSION = 1")

    rc = sync_mod.main(
        [
            "--repo-root",
            str(repo.parents[1]),
            "--user-root",
            str(user.parents[0]),
            "--dry-run",
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "将写入" in out
    assert not (user / "llm").exists()


def test_dry_run_function_level_plans_without_fs_writes(
    sync_mod: ModuleType, sides: dict[str, Path]
) -> None:
    repo, user = sides["repo"], sides["user"]
    _write(repo / "system" / "llm" / "plugin.json", "{}")
    _write(repo / "system" / "llm" / "sub" / "core.py", "VERSION = 1")

    outcome = sync_mod.sync_repo_plugins_to_user_root(
        repo, user / "plugins", dry_run=True
    )

    assert outcome.copied
    assert not (user / "plugins").exists()


def test_main_fails_without_repo_shared(
    sync_mod: ModuleType,
    tmp_path: Path,
    sides: dict[str, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()
    rc = sync_mod.main(
        ["--repo-root", str(empty_repo), "--user-root", str(sides["user"].parents[0])]
    )
    assert rc == 1
