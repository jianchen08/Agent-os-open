# @feature: FP-0.2.二 isolation 工作空间 | @ci: python-coverage
r"""跨盘 worktree 退化（ADR 草稿 2026-10-17-workspace-crossdrive-fallback）。

git 的 worktree 硬约束：worktree 目录与源仓库必须同一文件系统，跨盘报
`path is on mount 'D:', start on mount 'C:'`（git 二进制自身 fatal，仓内
无预检查）。契约：

- 同盘（含盘符大小写差异、任一侧无盘符形态）→ 基目录原样返回，不退化；
- 跨盘 → 退化到 `<项目所在盘符根>/<可配置根名，默认 .agentos-workspaces>`
  并告警（有意偏离 fail-closed 口径，ADR 草稿已声明）；
- 跨盘是确定性错误 → 禁止进入 prune + `branch -D` 重试分支；
- 判定口径 = 路径形态而非运行平台（与 workspace.validate_workspace_path
  同款），POSIX 绝对路径无盘符形态 → 恒同盘。

判定与退化点组装按路径形态构造 Windows 形态路径在任意平台断行为（不依赖
本机盘符布局）；退化落点的关键路径走真实 git（Windows 双盘实跑）。
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))
_SHARED_DIR = _PLUGIN_DIR.parent  # plugins/shared/（isolation.workspace 包定位根）
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))
_REPO_ROOT = _PLUGIN_DIR.parents[2]
_DEFAULT_FALLBACK_NAME = ".agentos-workspaces"

import isolation.workspace as _ws  # noqa: E402


def _load_mod(file_name: str, mod_name: str) -> Any:
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / file_name)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_GITOPS = _load_mod("_workspace_git_ops.py", "isolation_crossdrive_git_ops")
_LIFECYCLE = _load_mod("workspace_lifecycle.py", "isolation_crossdrive_lifecycle")

# 真实 git（Windows 双盘实跑）需本机存在两个盘符；单盘/无盘符形态的机器
# 跳过这些用例，形态判定面（TestCrossDriveBase）仍全覆盖。
_REPO_DRIVE = os.path.splitdrive(str(_REPO_ROOT))[0]
_TMP_DRIVE = os.path.splitdrive(tempfile.gettempdir())[0]
_WINDOWS_TWO_DRIVES = os.name == "nt" and bool(_REPO_DRIVE) and _REPO_DRIVE.upper() != _TMP_DRIVE.upper()
# 与 tmp_path（通常用户盘）不同盘的可写落点：data/ 已 gitignore
_OTHER_DRIVE_ROOT = Path(_REPO_ROOT) / "data"


def _git(args: list[str], cwd: Path) -> str:
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch", branch], path)
    _git(["config", "user.email", "agent@agent-os.local"], path)
    _git(["config", "user.name", "Agent OS"], path)
    (path / "README.md").write_text("proj", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "init"], path)
    return path


def _rmtree_force(path: Path) -> None:
    """删测试临时仓（git 对象在 Windows 上是只读文件，需 chmod 放开）。"""

    def _unlock(func: Any, target: str, _exc: Any) -> None:
        import stat

        os.chmod(target, stat.S_IWRITE)  # noqa: PTH101
        func(target)

    shutil.rmtree(path, onerror=_unlock)  # type: ignore[arg-type]


class _GitOpsHost(_GITOPS._GitOpsMixin):  # type: ignore[name-defined]
    """满足 Mixin 宿主属性约定的最小宿主（与既有测试同式）。"""

    def __init__(self, base_path: Path, config: dict[str, Any] | None = None) -> None:
        self._config = config if config is not None else {}
        self._base_path = Path(base_path)
        self._main_branch = ""
        self._merge_locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()


class _Manager(_LIFECYCLE.WorkspaceLifecycleManager):  # type: ignore[name-defined]
    """生命周期管理器（构造即真实 git 记录主分支，无需额外替身）。"""

    def __init__(self, base_path: Path, config: dict[str, Any]) -> None:
        super().__init__(
            resource_merge=None,
            config=config,
            task_tree=None,
            ws_meta_store={},
            base_path=str(base_path),
            mode_skill_sources=[],
        )


# ============================================================
# 盘符判定与退化点组装（按路径形态，任意平台）
# ============================================================


class TestCrossDriveBase:
    @pytest.mark.parametrize(
        ("base", "root_path"),
        [
            (Path("D:/ws"), Path("D:/proj")),  # 同盘
            (Path("D:/ws"), Path("d:/proj")),  # 盘符大小写差异仍同盘
            (Path("C:/Users/t/ws"), Path("/srv/proj")),  # POSIX 无盘符侧 → 不判跨盘
            (Path("D:/ws"), Path("/mnt/d/proj")),  # 同上（WSL 形态）
            (Path("/srv/ws"), Path("/srv/proj")),  # POSIX 两端无盘符
            (Path("D:/ws"), Path("proj")),  # 相对源目录 → 无盘符形态
        ],
    )
    def test_same_drive_returns_base_unchanged(self, base: Path, root_path: Path) -> None:
        """同盘 → 原样返回配置基目录（默认路径行为零改动）。"""
        assert _ws.resolve_workspace_base(base, root_path) is base

    @pytest.mark.parametrize(
        ("base", "root_path"),
        [
            (Path("C:/Users/t/AppData/ws"), Path("D:/myproj")),
            (Path("D:/ws"), Path("C:/proj")),
        ],
    )
    def test_cross_drive_falls_back_to_root_drive(self, base: Path, root_path: Path) -> None:
        """跨盘 → 退化到「项目所在盘符根 + 缺省根名」。

        盘符根按路径形态取（`as_posix()` 首段 = `D:`，两平台同构）：Path.drive
        是平台文法属性（POSIX 恒空串），不能作期望值。根前缀比大小写折衷
        （POSIX Path 比较大小写敏感，实现侧 normcase 归一；同文件
        configurable 用例同款惯用法）。
        """
        fallback = _ws.resolve_workspace_base(base, root_path)
        assert fallback != base
        drive_root = root_path.as_posix().split("/", 1)[0]
        assert str(fallback.parent).lower().startswith(drive_root.lower())
        assert fallback.name == _DEFAULT_FALLBACK_NAME

    def test_cross_drive_fallback_root_name_is_configurable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """退化点根名可配（盘符根不可写时可指到别处的同名目录）。"""
        monkeypatch.setattr(
            _ws, "_load_isolation_config", lambda: {"workspace": {"crossdrive_root_name": "ws_drive"}}
        )
        fallback = _ws.resolve_workspace_base(Path("C:/ws"), Path("D:/proj"))
        assert fallback.name == "ws_drive"
        assert str(fallback.parent).lower().startswith("d:")

    @pytest.mark.parametrize("cfg", [{}, {"workspace": {"crossdrive_root_name": "  "}}])
    def test_default_fallback_name_when_unconfigured(
        self, monkeypatch: pytest.MonkeyPatch, cfg: dict[str, Any]
    ) -> None:
        """配置缺失/空串 → 缺省根名 .agentos-workspaces。"""
        monkeypatch.setattr(_ws, "_load_isolation_config", lambda: cfg)
        assert _ws.resolve_workspace_base(Path("C:/ws"), Path("D:/proj")).name == _DEFAULT_FALLBACK_NAME


# ============================================================
# worktree add 跨盘 fatal：确定性错误不进 prune + branch -D
# ============================================================


class TestCrossDriveFatalNoRepair:
    def test_cross_mount_failure_preserves_branch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """跨盘 fatal 抛错且分支/目录零损伤：绝不走 prune + `branch -D`。"""
        repo = _init_repo(tmp_path / "repo")
        branch = "task/crossdrive0001"
        _git(["branch", branch], repo)
        ws_dir = tmp_path / "other_mount_ws"
        ws_dir.mkdir()
        (ws_dir / "keep.txt").write_text("现场保留", encoding="utf-8")

        real_run = subprocess.run
        cross_mount = "fatal: 'other_mount_ws' is on mount 'D:', start on mount 'C:'"

        def _fake_run(cmd: list[str], **kwargs: Any) -> Any:
            if "worktree" in cmd and "add" in cmd:
                return subprocess.CompletedProcess(cmd, 128, "", cross_mount)
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(_GITOPS.subprocess, "run", _fake_run)
        host = _GitOpsHost(base_path=tmp_path, config={})
        with pytest.raises(RuntimeError) as exc:
            host._worktree_add_with_repair(repo, branch, ws_dir, "crossdrive0001")
        assert "跨盘" in str(exc.value)
        assert branch in _git(["branch", "--list", branch], repo), "跨盘是确定性错误，分支不得被删"
        assert (ws_dir / "keep.txt").read_text(encoding="utf-8") == "现场保留"

    def test_non_cross_mount_failure_still_repairs(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """非跨盘的既有失败原因仍走 prune 修复重试（既有重试语义不退化）。"""
        repo = _init_repo(tmp_path / "repo")
        branch = "task/stale000001"
        _git(["branch", branch], repo)
        ws_dir = tmp_path / "ws"
        real_run = subprocess.run
        attempts: list[int] = []

        def _flaky_run(cmd: list[str], **kwargs: Any) -> Any:
            if "worktree" in cmd and "add" in cmd:
                attempts.append(1)
                if len(attempts) == 1:
                    return subprocess.CompletedProcess(cmd, 128, "", "fatal: stale worktree registration")
            return real_run(cmd, **kwargs)

        monkeypatch.setattr(_GITOPS.subprocess, "run", _flaky_run)
        host = _GitOpsHost(base_path=tmp_path, config={})
        host._worktree_add_with_repair(repo, branch, ws_dir, "stale000001")
        assert ws_dir.is_dir()
        assert (ws_dir / "README.md").exists()
        assert branch in _git(["branch", "--list", branch], repo)


# ============================================================
# 退化落点端到端（真实 git，Windows 双盘）
# ============================================================


@pytest.mark.skipif(not _WINDOWS_TWO_DRIVES, reason="需 Windows 且本机存在两个盘符（跨盘实跑）")
class TestWorktreeRootCrossDriveRealGit:
    @pytest.fixture
    def fallback_name(self, monkeypatch: pytest.MonkeyPatch) -> str:
        """退化根名取本次运行唯一名（盘符根下不留残留、可整目录回收）。"""
        unique = f".agentos-workspaces-test-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            _ws, "_load_isolation_config", lambda: {"workspace": {"crossdrive_root_name": unique}}
        )
        return unique

    def test_worktree_created_on_project_drive(self, tmp_path: Path, fallback_name: str) -> None:
        """源仓在 D:、配置基目录在 C: → worktree 实际落到 D: 并建树成功。"""
        repo = _init_repo(_OTHER_DRIVE_ROOT / f"crossdrive_proj_{uuid.uuid4().hex[:8]}")
        drive_root = Path(f"{repo.drive}{os.sep}")
        ws_base = tmp_path / "wsroot"
        manager = _Manager(base_path=repo, config={"workspace": {"root": str(ws_base)}})
        assert repo.drive.lower() != ws_base.drive.lower(), "本用例前提：源仓与配置基目录确实跨盘"
        try:
            meta = manager._create_worktree_root(ws_base, repo, "xdrv0001abcd")
            ws_dir = Path(meta["path"])
            assert ws_dir.parent == drive_root / fallback_name, f"退化点应为项目盘符根下的存放点，实际: {ws_dir}"
            assert ws_dir.drive.lower() == repo.drive.lower()
            assert ws_dir.is_dir()
            assert (ws_dir / "README.md").exists()
            assert meta["branch"] in _git(["branch", "--list", meta["branch"]], repo)
            assert not ws_base.exists(), "跨盘时不得在配置的跨盘基目录下建目录"
        finally:
            _rmtree_force(drive_root / fallback_name)
            _rmtree_force(repo)

    def test_same_drive_keeps_configured_base(self, tmp_path: Path) -> None:
        """同盘 → 仍落配置基目录，不触发退化（默认行为守住）。"""
        repo = _init_repo(tmp_path / "repo_same")
        ws_base = tmp_path / "wsroot_same"
        manager = _Manager(base_path=repo, config={"workspace": {"root": str(ws_base)}})
        try:
            meta = manager._create_worktree_root(ws_base, repo, "same0001abcd")
            assert Path(meta["path"]).parent == ws_base
            assert Path(meta["path"]).is_dir()
        finally:
            _rmtree_force(ws_base)


# ============================================================
# 入口路径全链（on_task_start → root/subtask → 收口）跨盘退化
# ============================================================


@pytest.mark.skipif(not _WINDOWS_TWO_DRIVES, reason="需 Windows 且本机存在两个盘符（跨盘实跑）")
class TestOnTaskStartCrossDriveRouting:
    """2026-10-17 装机 18:32/18:46 mount 复发的事故入口是 on_task_start 全链。

    上面 TestWorktreeRootCrossDriveRealGit 只钉收口点 `_create_worktree_root`
    本体；若入口（_start_root_task / _start_subtask）绕开收口自行拼落点，
    该测试面不设防。本组从服务公共入口 on_task_start 实跑（真实 git 跨盘），
    钉「配置基目录跨盘时任务工作区必须落项目所在盘」——收口点丢了 resolve
    这组即红（git 跨挂载 fatal）。
    """

    @pytest.fixture
    def fallback_name(self, monkeypatch: pytest.MonkeyPatch) -> str:
        """退化根名取本次运行唯一名（盘符根下不留残留、可整目录回收）。"""
        unique = f".agentos-workspaces-test-{uuid.uuid4().hex[:8]}"
        monkeypatch.setattr(
            _ws, "_load_isolation_config", lambda: {"workspace": {"crossdrive_root_name": unique}}
        )
        return unique

    def test_root_task_worktree_lands_on_project_drive(
        self, tmp_path: Path, fallback_name: str
    ) -> None:
        """根任务（is_root，显式 worktree）：工作区落项目盘退化点，配置基目录零落笔。"""
        repo = _init_repo(_OTHER_DRIVE_ROOT / f"crossdrive_root_{uuid.uuid4().hex[:8]}")
        ws_base = tmp_path / "wsroot"
        manager = _Manager(base_path=repo, config={"workspace": {"root": str(ws_base)}})
        drive_root = Path(f"{repo.drive}{os.sep}")
        try:
            meta = manager.on_task_start(
                "xent0001abcd",
                str(repo),
                {"is_root": True, "workspace_mode": "worktree", "_has_explicit_workspace": True},
            )
            ws_dir = Path(meta["path"])
            assert ws_dir.parent == drive_root / fallback_name, f"应退化到项目盘存放点，实际: {ws_dir}"
            assert ws_dir.is_dir()
            assert (ws_dir / "README.md").exists()
            assert meta["branch"] in _git(["branch", "--list", meta["branch"]], repo)
            assert not ws_base.exists(), "跨盘时不得在配置的跨盘基目录下建目录"
        finally:
            _rmtree_force(drive_root / fallback_name)
            _rmtree_force(repo)

    def test_explicit_worktree_subtask_lands_on_project_drive(
        self, tmp_path: Path, fallback_name: str
    ) -> None:
        """显式 worktree 子任务（is_root=False）：同口径落项目盘（_start_subtask 分支）。"""
        repo = _init_repo(_OTHER_DRIVE_ROOT / f"crossdrive_sub_{uuid.uuid4().hex[:8]}")
        ws_base = tmp_path / "wsroot"
        manager = _Manager(base_path=repo, config={"workspace": {"root": str(ws_base)}})
        drive_root = Path(f"{repo.drive}{os.sep}")
        try:
            meta = manager.on_task_start(
                "xent0002abcd",
                str(repo),
                {"is_root": False, "workspace_mode": "worktree", "_has_explicit_workspace": True},
            )
            ws_dir = Path(meta["path"])
            assert ws_dir.parent == drive_root / fallback_name, f"应退化到项目盘存放点，实际: {ws_dir}"
            assert ws_dir.is_dir()
            assert (ws_dir / "README.md").exists()
            assert meta["branch"] in _git(["branch", "--list", meta["branch"]], repo)
            assert not ws_base.exists(), "跨盘时不得在配置的跨盘基目录下建目录"
        finally:
            _rmtree_force(drive_root / fallback_name)
            _rmtree_force(repo)
