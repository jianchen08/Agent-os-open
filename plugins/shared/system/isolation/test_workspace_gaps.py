# @feature: FP-0.2.〇 管道引擎 | @ci: none-local
"""workspace.py / workspace_lifecycle.py 分支补测（覆盖率批九）。

workspace.py：
- validate_workspace_path：__fspath__ 协议抛错、两字符盘符根；
- _isolation_config_path / find_project_root：无环境变量且无祖先命中时的推导回退；
- _load_isolation_config：config_center 命中优先 / center 空回退文件 / 文件也失败返回 {}；
- get_workspace_config_root：配置空 → 缺省 .ai_workspaces；
- ensure_workspace_git_ignored：旧 exclude 条目无尾换行的续写 / exclude 不可读 fail-honest /
  基目录即项目根拒绝排除；
- get_workspace_base_dir：空白 root 归一化缺省 / 排除保障异常不阻断返回；
- resolve_workspace：相对 root/parent 坐标的前缀透传（绝对坐标由既有测试覆盖，
  前缀分支只对**相对**坐标可达）。

workspace_lifecycle.py：
- _start_subtask 显式 worktree 源目录不存在 → 由服务创建；
- on_session_start：会话工作区就绪 + skills 快照同步（无源不建空目录）；
- _downgrade_plain_if_not_git_repo / _start_root_task：有效 git 仓库放行不降级，
  非 git 项目根降级 plain 空目录（不污染项目根）；
- _prepare_root_repo 初始化锁：持锁 + 仓库就绪跳过 / 等待超时失败闭合 /
  等待中锁释放继续（fake clock 注入，无真实延迟）；锁释放失败吞 OSError；
- _root_repo_ready 三态（无 .git / 有 .git 无提交 / 有提交）。

git 一律在 tmp 目录真实执行（唯一外部依赖 git 子进程）；时钟注入 fake；
不触碰 docker（本两模块无容器路径）。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import logging
import subprocess
import sys
import tempfile
import types
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent  # plugins/shared/system/isolation/
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_WS_MOD_NAME = "isolation_workspace_gaps_test"
_LIFE_MOD_NAME = "isolation_workspace_gaps_lifecycle_test"


def _load_module(mod_name: str, filename: str) -> Any:
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_WS = _load_module(_WS_MOD_NAME, "workspace.py")
_LIFE = _load_module(_LIFE_MOD_NAME, "workspace_lifecycle.py")

WorkspaceLifecycleManager = _LIFE.WorkspaceLifecycleManager


# ═══════════════════════════════════════════════════════════
# workspace.py：validate_workspace_path 边缘
# ═══════════════════════════════════════════════════════════


class _FailingFspath:
    """__fspath__ 抛错的病态路径对象（normpath 经 os.fspath 触发异常）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __fspath__(self) -> str:
        raise self._exc

    def __repr__(self) -> str:
        return f"<fspath-{type(self._exc).__name__}>"


class TestValidatePathNormFailure:
    @pytest.mark.parametrize(
        "exc",
        [ValueError("embedded null"), TypeError("not a path")],
        ids=["value_error", "type_error"],
    )
    def test_fspath_failure_returns_invalid_error(self, exc: Exception) -> None:
        err = _WS.validate_workspace_path(_FailingFspath(exc))
        assert err is not None
        assert "无效" in err
        assert "<fspath-" in err  # 错误消息回显原始输入


class TestValidateDriveRoot:
    @pytest.mark.parametrize("workspace", ["C:", "z:"], ids=["uppercase_drive", "lowercase_drive"])
    def test_two_char_drive_root_rejected(self, workspace: str) -> None:
        """两字符盘符（无分隔符）按根目录拒绝。"""
        err = _WS.validate_workspace_path(workspace)
        assert err is not None
        assert "根目录" in err

    def test_drive_subdirectory_allowed(self) -> None:
        """同盘符的具体子目录放行（对照，防止一刀切拒绝盘符形态）。"""
        assert _WS.validate_workspace_path("C:/work/projects") is None


# ═══════════════════════════════════════════════════════════
# workspace.py：配置定位 / 加载回退链
# ═══════════════════════════════════════════════════════════


class TestConfigPathFallback:
    def test_no_env_no_ancestor_returns_derived_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量缺失且祖先链无配置 → 返回按模块位置推导的路径（不 panic）。"""
        monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
        monkeypatch.setattr(
            _WS, "__file__", str(tmp_path / "plugins" / "shared" / "system" / "isolation" / "workspace.py")
        )
        path = _WS._isolation_config_path()
        assert path.parts[-4:] == ("config", "plugins", "isolation", "isolation_config.yaml")
        assert str(path).startswith(str(tmp_path))
        assert not path.exists()


class TestFindProjectRootFallback:
    def test_no_env_no_ancestor_returns_fourth_parent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """无环境变量且祖先链无 config/plugins/isolation → 回退 parents[3] 推导（不 panic）。"""
        monkeypatch.delenv("AGENTOS_CONFIG_ROOT", raising=False)
        monkeypatch.setattr(_WS, "__file__", str(tmp_path / "a" / "b" / "c" / "workspace.py"))
        root = _WS.find_project_root()
        assert root == tmp_path
        assert not (root / "config" / "isolation").exists()


class _FakeCenter:
    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg

    def get(self, key: str) -> dict:
        return self._cfg


class TestLoadIsolationConfig:
    def test_config_center_hit_skips_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """config_center 命中 → 直接返回 center 配置（文件不可读也不影响）。"""
        center_cfg = {"workspace": {"root": "/from/center/root"}}
        fake_pkg = types.ModuleType("config")
        fake_center = types.ModuleType("config.config_center")
        fake_center.get_config_center = lambda: _FakeCenter(center_cfg)  # type: ignore[attr-defined]
        fake_pkg.config_center = fake_center  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "config", fake_pkg)
        monkeypatch.setitem(sys.modules, "config.config_center", fake_center)
        # center 命中时即使文件定位也不可读，仍返回 center 配置（优先级行为）
        monkeypatch.setattr(_WS, "_isolation_config_path", lambda: tmp_path / "missing" / "isolation_config.yaml")
        assert _WS._load_isolation_config() == center_cfg
        assert _WS.get_workspace_config_root() == "/from/center/root"

    def test_config_center_empty_falls_back_to_file(self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
        """center 返回空 → 回退磁盘真身配置（文件回退主读路径）。"""
        fake_pkg = types.ModuleType("config")
        fake_center = types.ModuleType("config.config_center")
        fake_center.get_config_center = lambda: _FakeCenter({})  # type: ignore[attr-defined]
        fake_pkg.config_center = fake_center  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "config", fake_pkg)
        monkeypatch.setitem(sys.modules, "config.config_center", fake_center)
        with caplog.at_level(logging.WARNING):
            assert _WS.get_workspace_config_root() == ".ai_workspaces"  # 仓库真身配置值
        assert "文件回退" in caplog.text

    def test_center_and_file_both_fail_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """center 不可用 + 文件定位失败 → 返回 {}（缺省兜底，不 panic）。"""
        monkeypatch.setattr(_WS, "_isolation_config_path", lambda: tmp_path / "nope" / "isolation_config.yaml")
        with caplog.at_level(logging.WARNING):
            assert _WS._load_isolation_config() == {}
        assert "文件回退" in caplog.text
        assert _WS.get_workspace_config_root() == ".ai_workspaces"


# ═══════════════════════════════════════════════════════════
# workspace.py：git 本地排除
# ═══════════════════════════════════════════════════════════


class TestEnsureGitIgnored:
    def _make_project(self, tmp_path: Path, exclude_initial: str | None) -> tuple[Path, Path]:
        proj = tmp_path / "proj"
        (proj / ".git" / "info").mkdir(parents=True)
        exclude = proj / ".git" / "info" / "exclude"
        if exclude_initial is not None:
            exclude.write_text(exclude_initial, encoding="utf-8")
        return proj, exclude

    @pytest.mark.parametrize(
        "initial",
        ["existing_entry", "existing_entry\n"],
        ids=["no_trailing_newline", "trailing_newline"],
    )
    def test_appends_pattern_with_newline_separation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial: str
    ) -> None:
        """追加前确保旧条目换行隔离（无尾换行先补），幂等不重复。"""
        proj, exclude = self._make_project(tmp_path, initial)
        monkeypatch.setattr(_WS, "find_project_root", lambda: proj)
        base = proj / "ws_root"
        assert _WS.ensure_workspace_git_ignored(base) is True
        content = exclude.read_text(encoding="utf-8")
        pattern = "/ws_root/"
        assert content.startswith(initial)  # 旧条目原样保留
        assert "\n\n" not in content  # 不引入空行
        assert content.endswith(f"{pattern}\n")
        assert content.count(pattern) == 1
        # 幂等：再次调用不重复追加
        assert _WS.ensure_workspace_git_ignored(base) is True
        assert exclude.read_text(encoding="utf-8") == content

    def test_unwritable_exclude_fail_honest_false(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """exclude 不可读（目录占位）→ 告警并返回 False（fail-honest，不 panic）。"""
        proj = tmp_path / "proj"
        (proj / ".git" / "info" / "exclude").mkdir(parents=True)  # exclude 是目录 → 读/写必然失败
        monkeypatch.setattr(_WS, "find_project_root", lambda: proj)
        with caplog.at_level(logging.WARNING):
            assert _WS.ensure_workspace_git_ignored(proj / "ws") is False
        assert "git 本地排除写入失败" in caplog.text

    def test_base_equals_project_root_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """基目录即项目根 → 拒绝排除（排除整个仓库等于瘫痪 git），不写任何条目。"""
        proj, exclude = self._make_project(tmp_path, None)
        monkeypatch.setattr(_WS, "find_project_root", lambda: proj)
        with caplog.at_level(logging.WARNING):
            assert _WS.ensure_workspace_git_ignored(proj) is False
        assert "项目根" in caplog.text
        assert not exclude.exists()


# ═══════════════════════════════════════════════════════════
# workspace.py：get_workspace_base_dir 边缘
# ═══════════════════════════════════════════════════════════


class TestWorkspaceBaseDirGaps:
    @pytest.mark.parametrize("blank_root", ["   ", " \t\n "], ids=["spaces", "mixed_whitespace"])
    def test_blank_root_normalized_to_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blank_root: str) -> None:
        """配置 root 为非空白字符的纯空白串 → strip 后为空，归一化为缺省值。"""
        monkeypatch.setattr(_WS, "_load_isolation_config", lambda: {"workspace": {"root": blank_root}})
        monkeypatch.setattr(_WS, "find_project_root", lambda: tmp_path)
        assert _WS.get_workspace_base_dir() == tmp_path / ".ai_workspaces"

    def test_named_root_still_resolved(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照：具名相对 root 正常解析（blank 归一化不误伤正常值）。"""
        monkeypatch.setattr(_WS, "_load_isolation_config", lambda: {"workspace": {"root": "named_ws"}})
        monkeypatch.setattr(_WS, "find_project_root", lambda: tmp_path)
        assert _WS.get_workspace_base_dir() == tmp_path / "named_ws"

    def test_git_ignore_guard_failure_does_not_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """排除保障环节抛异常 → 告警并照常返回基目录（不阻断）。"""
        monkeypatch.setattr(_WS, "_load_isolation_config", lambda: {"workspace": {"root": "guard_ws"}})
        monkeypatch.setattr(_WS, "find_project_root", lambda: tmp_path)

        def _boom(base: Path) -> bool:
            raise RuntimeError("guard exploded")

        monkeypatch.setattr(_WS, "ensure_workspace_git_ignored", _boom)
        with caplog.at_level(logging.WARNING):
            base = _WS.get_workspace_base_dir()
        assert base == tmp_path / "guard_ws"
        assert "git 本地排除保障异常" in caplog.text


# ═══════════════════════════════════════════════════════════
# workspace.py：resolve_workspace 相对坐标前缀透传
# ═══════════════════════════════════════════════════════════


class TestResolveRelativePrefixPassthrough:
    """前缀分支只对相对坐标可达（绝对坐标在更早的 is_absolute 分支返回）。"""

    def test_root_task_relative_root_prefix_returned_as_is(self) -> None:
        assert _WS.resolve_workspace("t1", "wsroot/child", config_root="wsroot") == "wsroot/child"
        assert _WS.resolve_workspace("t1", "wsroot", config_root="wsroot") == "wsroot"

    def test_root_task_relative_without_prefix_joined(self) -> None:
        """对照：无前缀的相对空间拼到 root 下（透传不误伤拼接语义）。"""
        assert _WS.resolve_workspace("t1", "child", config_root="wsroot") == "wsroot/child"

    def test_child_relative_parent_prefix_returned_as_is(self) -> None:
        assert (
            _WS.resolve_workspace("kid", "parent/sub", parent_resolved_workspace="parent", config_root="wsroot")
            == "parent/sub"
        )
        assert (
            _WS.resolve_workspace("kid", "parent", parent_resolved_workspace="parent", config_root="wsroot")
            == "parent"
        )

    def test_child_relative_root_prefix_returned_as_is(self) -> None:
        assert (
            _WS.resolve_workspace("kid", "wsroot/sib", parent_resolved_workspace="other", config_root="wsroot")
            == "wsroot/sib"
        )

    def test_child_relative_no_prefix_joined_to_parent(self) -> None:
        """对照：无任何前缀的相对空间拼到父路径下。"""
        assert (
            _WS.resolve_workspace("kid", "sub", parent_resolved_workspace="parent", config_root="wsroot")
            == "parent/sub"
        )


# ═══════════════════════════════════════════════════════════
# workspace_lifecycle.py：测试基建
# ═══════════════════════════════════════════════════════════


class _FakeTask:
    def __init__(self, task_id: str, parent_task_id: str | None = None, metadata: dict | None = None):
        self.id = task_id
        self.parent_task_id = parent_task_id
        self.metadata = metadata or {}


class _FakeTree:
    def get_task(self, task_id: str) -> _FakeTask | None:
        return None

    async def save_task(self, task: _FakeTask) -> None:  # pragma: no cover - 本文件不触发持久化
        return None


def _make_manager(tmp_path: Path, ws_root: str | Path) -> WorkspaceLifecycleManager:  # type: ignore[valid-type]
    return WorkspaceLifecycleManager(
        resource_merge=None,
        config={"workspace": {"default_mode": "worktree", "root": str(ws_root)}},
        task_tree=_FakeTree(),
        ws_meta_store={},
        base_path=str(tmp_path),
    )


def _git_init(repo: Path, with_commit: bool = True) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True, text=True)
    # CI runner 无全局 git 身份——repo 级补身份（与生产 _ensure_git_user 同口径）
    subprocess.run(["git", "config", "user.name", "Agent OS"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "agent@agent-os.local"], cwd=repo, check=True, capture_output=True, text=True
    )
    if with_commit:
        (repo / "README.md").write_text("proj", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True)


def _init_lock_path(root_path: Path) -> Path:
    """镜像 _prepare_root_repo 的锁文件命名（sha1(小写路径)[:16]），用于模拟外部持锁者。"""
    digest = hashlib.sha1(str(root_path).lower().encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"agentos_init_{digest}.lock"


# ═══════════════════════════════════════════════════════════
# workspace_lifecycle.py：子任务显式 worktree / 降级守卫
# ═══════════════════════════════════════════════════════════


class TestSubtaskExplicitWorktree:
    def test_missing_source_dir_created_then_worktree_built(self, tmp_path: Path) -> None:
        """显式 worktree 子任务的源目录不存在 → 服务负责创建目录再建隔离副本。"""
        source = tmp_path / "brand_new_src"
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        meta = m._start_subtask(
            "sub-new", str(source), {"workspace_mode": "worktree", "_has_explicit_workspace": True}
        )
        assert source.is_dir()
        assert meta["mode"] == "worktree"
        assert meta["project_root"] == str(source)
        assert Path(meta["path"]).is_dir()
        assert Path(meta["path"]) != source


class TestDowngradeGuard:
    def test_guard_passes_valid_git_repo_without_meta(self, tmp_path: Path) -> None:
        """有效 git 仓库（有 HEAD）→ 守卫放行返回 None，不落任何降级 meta。"""
        repo = tmp_path / "valid_repo"
        _git_init(repo)
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        assert m._downgrade_plain_if_not_git_repo("t-guard", repo, str(repo)) is None
        assert "t-guard" not in m._ws_meta_store

    def test_root_task_worktree_mode_passes_guard_end_to_end(self, tmp_path: Path) -> None:
        """端到端：无显式 workspace + 显式 worktree 模式 + 有效仓库 → 正常建 worktree。"""
        repo = tmp_path / "guard_repo"
        _git_init(repo)
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        meta = m._start_root_task("r1", str(repo), {"task_id": "r1", "workspace_mode": "worktree"})
        assert meta["mode"] == "worktree"
        assert meta["project_root"] == str(repo)
        assert Path(meta["path"]).is_dir()

    def test_non_git_root_downgraded_to_plain(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """端到端：worktree 模式但项目根非 git 仓库 → 降级 plain 空目录（fail-honest）。"""
        bare = tmp_path / "not_a_repo"
        bare.mkdir()
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        with caplog.at_level(logging.WARNING):
            meta = m._start_root_task("r1", str(bare), {"task_id": "r1", "workspace_mode": "worktree"})
        assert meta["mode"] == "plain"
        assert Path(meta["path"]) == tmp_path / "wsroot" / "r1"
        assert Path(meta["path"]).is_dir()
        assert "降级 plain" in caplog.text
        assert not (bare / ".git").exists()  # 降级不污染项目根（不自动 git init）


class TestOnSessionStart:
    def test_creates_workspace_and_syncs_skills(self, tmp_path: Path) -> None:
        """主会话工作区就绪：目录不存在则创建，并同步 skills 快照。"""
        (tmp_path / "skills" / "skill_a" / "scripts").mkdir(parents=True)
        (tmp_path / "skills" / "skill_a" / "SKILL.md").write_text("# a", encoding="utf-8")
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        sess = tmp_path / "wsroot" / "sessions" / "thread-1"
        m.on_session_start(str(sess))
        assert sess.is_dir()
        assert (sess / "skills" / "skill_a" / "SKILL.md").exists()

    def test_missing_skills_source_still_ready(self, tmp_path: Path) -> None:
        """对照：无 skills/ 源目录 → 目录仍就绪，不建空 skills。"""
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        sess = tmp_path / "wsroot" / "sessions" / "thread-2"
        m.on_session_start(str(sess))
        assert sess.is_dir()
        assert not (sess / "skills").exists()


# ═══════════════════════════════════════════════════════════
# workspace_lifecycle.py：初始化锁协议（fake clock，无真实延迟）
# ═══════════════════════════════════════════════════════════


class TestPrepareRootRepoLock:
    def test_lock_contention_ready_repo_skips_init(self, tmp_path: Path) -> None:
        """外部持锁 + 仓库已就绪 → 复检通过直接跳过，从不取得锁（锁文件原样保留）。"""
        root_path = tmp_path / "ready_repo"
        _git_init(root_path)
        lock_path = _init_lock_path(root_path)
        lock_path.write_text("other-task", encoding="utf-8")
        try:
            m = _make_manager(tmp_path, tmp_path / "wsroot")
            m._prepare_root_repo(root_path, "t-ready")
            assert lock_path.exists()  # 若本调用取得过锁，收尾会 unlink——文件仍在即证明跳过
            assert "t-ready" not in m._ws_meta_store
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()

    def test_lock_timeout_raises_fail_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """持锁不释放且仓库未就绪 → 到达 deadline 失败闭合抛错（不再空转睡眠）。"""
        root_path = tmp_path / "never_ready"
        lock_path = _init_lock_path(root_path)
        lock_path.write_text("other-task", encoding="utf-8")

        class _FakeClock:
            def __init__(self) -> None:
                self.values = [0.0, 100.0]
                self.sleeps: list[float] = []

            def monotonic(self) -> float:
                return self.values.pop(0) if self.values else 100.0

            def sleep(self, seconds: float) -> None:
                self.sleeps.append(seconds)

        clock = _FakeClock()
        monkeypatch.setattr(_LIFE, "time", clock)
        try:
            m = _make_manager(tmp_path, tmp_path / "wsroot")
            with pytest.raises(RuntimeError, match="锁等待超时"):
                m._prepare_root_repo(root_path, "t-timeout")
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink()
        assert clock.sleeps == []  # deadline 即刻失败，不空转

    def test_lock_released_midwait_proceeds_to_init(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """持锁方在等待期内释放 → sleep 一轮后取得锁并完成初始化，收尾释放。"""
        root_path = tmp_path / "late_ready"
        root_path.mkdir()
        lock_path = _init_lock_path(root_path)
        lock_path.write_text("other-task", encoding="utf-8")

        class _ReleasingClock:
            def __init__(self) -> None:
                self.values = [0.0, 1.0]
                self.sleeps: list[float] = []

            def monotonic(self) -> float:
                return self.values.pop(0) if self.values else 1.0

            def sleep(self, seconds: float) -> None:
                self.sleeps.append(seconds)
                lock_path.unlink()  # 持锁方在等待期内释放

        clock = _ReleasingClock()
        monkeypatch.setattr(_LIFE, "time", clock)
        try:
            m = _make_manager(tmp_path, tmp_path / "wsroot")
            m._prepare_root_repo(root_path, "t-late")
            assert clock.sleeps == [0.5]  # 恰好等待一轮
            assert not lock_path.exists()  # 本调用取得并已释放锁
            rc, out, _ = m._run_git("rev-parse", "HEAD", cwd=root_path)
            assert rc == 0 and out.strip()  # 等待后完成初始化提交
        finally:
            with contextlib.suppress(OSError):
                lock_path.unlink(missing_ok=True)

    def test_lock_release_failure_swallowed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """收尾释放锁失败（OSError）→ 吞掉不外抛，初始化结果不受影响。"""
        root_path = tmp_path / "unlink_fail"
        root_path.mkdir()
        lock_path = _init_lock_path(root_path)
        real_unlink = Path.unlink

        def _failing_unlink(self: Path, *a: Any, **k: Any) -> None:
            if self.name.startswith("agentos_init_"):
                raise PermissionError("still held elsewhere")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", _failing_unlink)
        try:
            m = _make_manager(tmp_path, tmp_path / "wsroot")
            m._prepare_root_repo(root_path, "t-unlink")  # 不抛
            assert (root_path / ".git").exists()  # 初始化已完成
            assert lock_path.exists()  # 释放失败，锁文件残留
        finally:
            monkeypatch.undo()
            with contextlib.suppress(OSError):
                real_unlink(lock_path)


class TestRootRepoReady:
    @pytest.mark.parametrize(
        ("setup", "expected"),
        [("no_git", False), ("git_no_commit", False), ("git_with_head", True)],
    )
    def test_ready_tri_state(self, tmp_path: Path, setup: str, expected: bool) -> None:
        """就绪判定三态：无 .git / 有 .git 无提交 / 有提交。"""
        root = tmp_path / setup
        if setup == "git_no_commit":
            _git_init(root, with_commit=False)
        elif setup == "git_with_head":
            _git_init(root)
        else:
            root.mkdir()
        m = _make_manager(tmp_path, tmp_path / "wsroot")
        assert m._root_repo_ready(root) is expected
