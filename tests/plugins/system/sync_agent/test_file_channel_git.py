# @feature: FP-0.2.多端同步 T2 文件类通道 git 化 | @ci: python-coverage
"""file 通道 git 仓库化测试（拍板③b，台账 R16 设计定稿）。

四件 git 内置能力的行为断言（全部走真实 git 子进程，零 mock 内部依赖）：
变更检测=commit（含删除即墓碑）、版本历史=log、增量传输=bundle 前置依赖、
拉取恒 fast-forward（非 ff 原生拒绝=零合并结构性执法）。属主写他端只读
以读者路径无 commit 入口实证。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from _root_api import ASSET_CHANNELS, asset_channel, is_owner_scoped
from agent_core import SUPPORTED_ASSET_TYPES, AgentConfig, SyncAgentEngine
from agent_state import AgentStateStore
from file_channel import (
    FILE_CHANNEL_SCOPES,
    FileChannelConfig,
    GitChannelError,
    GitFileChannel,
)
from outbox_store import OutboxStore

pytestmark = pytest.mark.unit

OWNER = "desktop-a"
READER = "phone-b"


def _run_git(git_dir: Path, *args: str) -> str:
    """测试观察面：直接以 git CLI 读仓库可观察状态（仓库即被测物的外部状态）。"""
    proc = subprocess.run(
        ["git", "--git-dir", str(git_dir), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"git {args} failed: {proc.stderr}"
    return proc.stdout


def _owner_config(tmp_path: Path, scope_roots: dict[str, Path]) -> FileChannelConfig:
    return FileChannelConfig(
        repo_root=tmp_path / "fc",
        owner_roots=dict(scope_roots),
        reader_scopes=frozenset(),
        device_id=OWNER,
    )


def _reader_config(tmp_path: Path, scopes: frozenset[str]) -> FileChannelConfig:
    return FileChannelConfig(
        repo_root=tmp_path / "fc-reader",
        owner_roots={},
        reader_scopes=scopes,
        device_id=READER,
    )


# ── 注册面（路由中间件）──────────────────────────────────────


@pytest.mark.parametrize("asset_type", ["plugins", "skills", "artifact_index"])
def test_git_asset_types_registered_to_file_channel(asset_type: str) -> None:
    """T2 三资产注册进 file 通道；属主语义不进 relay 词表（T4 让位面零漂移）。"""
    assert ASSET_CHANNELS[asset_type] == "file"
    assert asset_channel(asset_type) == "file"
    assert not is_owner_scoped(asset_type)


def test_asset_channel_registry_zero_regression() -> None:
    """既有四注册项零改动（config 面与 db 面零回归的注册面前提）。"""
    assert ASSET_CHANNELS["config"] == "file"
    assert ASSET_CHANNELS["session"] == "db"
    assert ASSET_CHANNELS["message"] == "db"
    assert ASSET_CHANNELS["task"] == "db"
    assert is_owner_scoped("session")
    assert is_owner_scoped("task")
    assert not is_owner_scoped("config")


def test_supported_types_advertise_git_scopes() -> None:
    """设备注册 capabilities 广告面 = 通道注册表全集（git scope 随入）。"""
    for scope in FILE_CHANNEL_SCOPES:
        assert scope in SUPPORTED_ASSET_TYPES


# ── 变更检测 = commit（含墓碑）────────────────────────────────


def test_capture_commit_add_modify_noop(tmp_path: Path) -> None:
    """加文件→commit、改文件→新 commit、零变更→零 commit（三态有区分度）。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "plug_a.py").write_text("a=1\n", encoding="utf-8")
    head1 = channel.capture_commit("plugins")
    assert head1 is not None
    assert len(head1) == 40
    (src / "plug_a.py").write_text("a=2\n", encoding="utf-8")
    head2 = channel.capture_commit("plugins")
    assert head2 is not None
    assert head2 != head1
    assert channel.capture_commit("plugins") is None  # 无变更零 commit


def test_capture_commit_tombstone_is_commit(tmp_path: Path) -> None:
    """删除已提交文件 = 墓碑即提交（禁止手写墓碑的 git 内置路径）。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("a\n", encoding="utf-8")
    (src / "b.py").write_text("b\n", encoding="utf-8")
    channel.capture_commit("plugins")
    (src / "a.py").unlink()
    head = channel.capture_commit("plugins")
    assert head is not None
    assert channel.commit_count("plugins") == 2
    # 墓碑在版本树可查：HEAD 树已无该文件。
    tree = _run_git(channel.repo_git_dir("plugins"), "ls-tree", "-r", "--name-only", "HEAD")
    assert "a.py" not in tree
    assert "b.py" in tree


@pytest.mark.parametrize(
    "rel_path",
    [".venv/lib/x.py", "__pycache__/m.pyc", "node_modules/pkg/i.js", "target/r.o"],
)
def test_capture_commit_excludes_runtime_dirs(tmp_path: Path, rel_path: str) -> None:
    """运行时/依赖目录零 commit（info/exclude 内置排除，非手写指纹）。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    excluded = src / rel_path
    excluded.parent.mkdir(parents=True)
    excluded.write_text("junk\n", encoding="utf-8")
    assert channel.capture_commit("plugins") is None
    (src / "real.py").write_text("real\n", encoding="utf-8")
    assert channel.capture_commit("plugins") is not None  # 对照组：正常文件照常入库


def test_capture_commit_reader_scope_forbidden(tmp_path: Path) -> None:
    """属主写他端只读：读者域捕获路径结构性无 commit 入口。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_reader_config(tmp_path, frozenset({"plugins"})))
    with pytest.raises(GitChannelError):
        channel.capture_commit("plugins")


def test_head_and_commit_count_lifecycle(tmp_path: Path) -> None:
    """版本历史观测面： unborn→None/0，两轮提交→head+计数单调。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    assert channel.head("plugins") is None
    assert channel.commit_count("plugins") == 0
    (src / "a.py").write_text("1\n", encoding="utf-8")
    first = channel.capture_commit("plugins")
    (src / "a.py").write_text("2\n", encoding="utf-8")
    second = channel.capture_commit("plugins")
    assert first is not None
    assert second is not None
    assert second != first
    assert channel.head("plugins") == second
    assert channel.commit_count("plugins") == 2


# ── 增量传输 = bundle + fetch（恒 ff）────────────────────────


def test_create_bundle_unborn_or_unchanged_returns_none(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    owner = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    assert owner.create_bundle("plugins", None) is None  # 零历史无可给
    (src / "a.py").write_text("1\n", encoding="utf-8")
    owner.capture_commit("plugins")
    head = owner.head("plugins")
    assert head is not None
    assert owner.create_bundle("plugins", head) is None  # 对端已最新


def test_full_bundle_apply_materializes_mirror(tmp_path: Path) -> None:
    """首拉（无 basis）全量 bundle → 读者镜像收敛 + head 对齐。"""
    src = tmp_path / "src"
    src.mkdir()
    owner = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("a\n", encoding="utf-8")
    (src / "sub").mkdir()
    (src / "sub" / "b.py").write_text("b\n", encoding="utf-8")
    owner.capture_commit("plugins")
    reader = GitFileChannel(_reader_config(tmp_path, frozenset({"plugins"})))
    bundle = owner.create_bundle("plugins", None)
    assert bundle is not None
    reader.apply_bundle("plugins", bundle)
    mirror = reader.mirror_root("plugins")
    assert (mirror / "a.py").read_text(encoding="utf-8") == "a\n"
    assert (mirror / "sub" / "b.py").read_text(encoding="utf-8") == "b\n"
    assert reader.head("plugins") == owner.head("plugins")


def test_incremental_bundle_carries_prerequisite_and_updates(tmp_path: Path) -> None:
    """增量 bundle 带 ^basis 前置头（git 内置增量标记）→ ff 更新含墓碑落地。"""
    src = tmp_path / "src"
    src.mkdir()
    owner = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("v1\n", encoding="utf-8")
    owner.capture_commit("plugins")
    reader = GitFileChannel(_reader_config(tmp_path, frozenset({"plugins"})))
    reader.apply_bundle("plugins", owner.create_bundle("plugins", None))
    basis = reader.head("plugins")
    assert basis is not None
    # 属主端追加新文件 + 删除旧文件（墓碑传播）。
    (src / "a.py").unlink()
    (src / "new.py").write_text("n\n", encoding="utf-8")
    owner.capture_commit("plugins")
    bundle = owner.create_bundle("plugins", basis)
    assert bundle is not None
    assert bundle.startswith(b"# v2 git bundle")
    assert b"\n-" + basis.encode() in bundle  # 前置依赖行 = 增量而非全量
    reader.apply_bundle("plugins", bundle)
    mirror = reader.mirror_root("plugins")
    assert not (mirror / "a.py").exists()  # 墓碑镜像落地
    assert (mirror / "new.py").read_text(encoding="utf-8") == "n\n"
    assert reader.head("plugins") == owner.head("plugins")


def test_apply_refuses_non_fast_forward(tmp_path: Path) -> None:
    """读者分叉（模拟异常世界）→ fetch 原生拒绝非 ff → 镜像与 head 原样。"""
    src = tmp_path / "src"
    src.mkdir()
    owner = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("v1\n", encoding="utf-8")
    owner.capture_commit("plugins")
    reader = GitFileChannel(_reader_config(tmp_path, frozenset({"plugins"})))
    reader.apply_bundle("plugins", owner.create_bundle("plugins", None))
    synced_base = reader.head("plugins")  # 读者最后同步位 = 属主 c1
    assert synced_base is not None
    # 属主前进一格（c2），读者未同步。
    (src / "a.py").write_text("v2\n", encoding="utf-8")
    owner.capture_commit("plugins")
    # 异常世界注入：读者仓库自 c1 分叉本地 commit（读者通道自身无此路径）。
    git_dir = reader.repo_git_dir("plugins")
    local = _run_git(git_dir, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "diverge")
    _run_git(git_dir, "update-ref", "refs/heads/plugins", local.strip())
    bundle = owner.create_bundle("plugins", synced_base)  # ^c1 → [c2]
    assert bundle is not None
    with pytest.raises(GitChannelError):
        reader.apply_bundle("plugins", bundle)
    assert reader.head("plugins") == local.strip()  # 拒绝后零副作用
    mirror = reader.mirror_root("plugins")
    assert (mirror / "a.py").read_text(encoding="utf-8") == "v1\n"


def test_reader_never_creates_local_commits(tmp_path: Path) -> None:
    """多轮拉取后读者 commit 数恒等于属主 commit 数（零本地提交）。"""
    src = tmp_path / "src"
    src.mkdir()
    owner = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    reader = GitFileChannel(_reader_config(tmp_path, frozenset({"plugins"})))
    for round_no in range(3):
        (src / f"f{round_no}.py").write_text(f"{round_no}\n", encoding="utf-8")
        owner.capture_commit("plugins")
        bundle = owner.create_bundle("plugins", reader.head("plugins"))
        if bundle is not None:
            reader.apply_bundle("plugins", bundle)
    assert reader.commit_count("plugins") == owner.commit_count("plugins") == 3


def test_create_bundle_unknown_basis_fails_closed(tmp_path: Path) -> None:
    """basis 不在属主历史（异常对端）→ 属主侧诚实失败（fail-closed）。"""
    src = tmp_path / "src"
    src.mkdir()
    owner = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("1\n", encoding="utf-8")
    owner.capture_commit("plugins")
    with pytest.raises(GitChannelError):
        owner.create_bundle("plugins", "0" * 40)


# ── 失败面（错误要么传播要么显式翻译，禁静默吞）────────────────


def test_capture_commit_missing_source_root_skips(tmp_path: Path) -> None:
    """属主源目录缺失 → 该域跳过捕获（None，零 commit 零异常）。"""
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": tmp_path / "absent"}))
    assert channel.capture_commit("plugins") is None


def test_capture_commit_stale_lock_propagates(tmp_path: Path) -> None:
    """仓库锁残留（真实故障形态）→ add 失败显式翻译，禁当零变更吞掉。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("1\n", encoding="utf-8")
    channel.capture_commit("plugins")
    (channel.repo_git_dir("plugins") / "index.lock").write_bytes(b"")
    (src / "a.py").write_text("2\n", encoding="utf-8")
    with pytest.raises(GitChannelError, match="add 失败"):
        channel.capture_commit("plugins")


def test_capture_commit_hook_rejection_propagates(tmp_path: Path) -> None:
    """pre-commit 钩子拒绝（真实故障形态）→ commit 失败显式传播。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("1\n", encoding="utf-8")
    channel.capture_commit("plugins")
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    # POSIX git 只执行带执行位的钩子（Windows 无此语义）：不 chmod 则
    # Linux 上钩子被静默跳过 → commit 成功 → 故障形态无法复现
    os.chmod(hooks / "pre-commit", 0o755)
    _run_git(channel.repo_git_dir("plugins"), "config", "core.hooksPath", str(hooks))
    (src / "a.py").write_text("2\n", encoding="utf-8")
    with pytest.raises(GitChannelError, match="commit 失败"):
        channel.capture_commit("plugins")


def test_capture_commit_status_config_corruption_propagates(tmp_path: Path) -> None:
    """仓库配置损坏（status 失败而 add 成功的真实形态）→ 显式传播禁吞。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("1\n", encoding="utf-8")
    channel.capture_commit("plugins")
    _run_git(channel.repo_git_dir("plugins"), "config", "status.showUntrackedFiles", "bogus")
    (src / "b.py").write_text("2\n", encoding="utf-8")
    with pytest.raises(GitChannelError, match="status 失败"):
        channel.capture_commit("plugins")


def test_apply_bundle_owner_scope_forbidden(tmp_path: Path) -> None:
    """属主域不走 apply 路径（结构性守卫：属主内容源不经镜像落地面）。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("1\n", encoding="utf-8")
    channel.capture_commit("plugins")
    with pytest.raises(GitChannelError, match="拉取域"):
        channel.apply_bundle("plugins", b"# v2 git bundle\n")


def test_apply_bundle_mirror_path_conflict_translates(tmp_path: Path) -> None:
    """镜像落点被文件占用（部署损坏形态）→ OSError 边界翻译 GitChannelError。"""
    src = tmp_path / "src"
    src.mkdir()
    owner = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("1\n", encoding="utf-8")
    owner.capture_commit("plugins")
    reader = GitFileChannel(_reader_config(tmp_path, frozenset({"plugins"})))
    reader.mirror_root("plugins").parent.mkdir(parents=True)
    reader.mirror_root("plugins").write_text("occupied", encoding="utf-8")
    bundle = owner.create_bundle("plugins", None)
    assert bundle is not None
    with pytest.raises(GitChannelError, match="镜像目录创建失败"):
        reader.apply_bundle("plugins", bundle)


def test_ensure_repo_path_conflict_translates(tmp_path: Path) -> None:
    """仓库路径被文件占用 → OSError 边界翻译 GitChannelError。"""
    src = tmp_path / "src"
    src.mkdir()
    channel = GitFileChannel(_owner_config(tmp_path, {"plugins": src}))
    git_dir = channel.repo_git_dir("plugins")
    git_dir.parent.mkdir(parents=True)
    git_dir.write_text("occupied", encoding="utf-8")
    (src / "a.py").write_text("1\n", encoding="utf-8")
    with pytest.raises(GitChannelError, match="仓库目录创建失败"):
        channel.capture_commit("plugins")


# ── 引擎臂失败面与退避 ───────────────────────────────────────


def test_engine_tick_runs_file_arms(tmp_path: Path) -> None:
    """单拍全臂：自举后 config 捕获与 file 双臂同拍执行（tick 接线面）。"""
    src = tmp_path / "src"
    src.mkdir()
    engine, relay = _build_engine(tmp_path, OWNER, file_channel=_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("1\n", encoding="utf-8")
    asyncio.run(engine.tick())
    channel = engine.file_channel
    assert channel is not None
    assert channel.commit_count("plugins") == 1
    assert engine.status.bootstrap_done
    assert relay.posted  # 注册 + 心跳出站发生过


def test_engine_capture_without_channel_is_zero(tmp_path: Path) -> None:
    engine, _ = _build_engine(tmp_path, OWNER)
    assert engine.capture_file_scopes() == 0
    assert "file_channel" not in engine.status_dict()


def test_engine_capture_git_failure_sets_last_error(tmp_path: Path) -> None:
    """通道故障不阻塞本拍（离线第一）：last_error 落字，零上抛。"""
    src = tmp_path / "src"
    src.mkdir()
    engine, _ = _build_engine(tmp_path, OWNER, file_channel=_owner_config(tmp_path, {"plugins": src}))
    engine.capture_file_scopes()  # 建仓库（零变更零 commit）
    channel = engine.file_channel
    assert channel is not None
    (channel.repo_git_dir("plugins") / "index.lock").write_bytes(b"")
    (src / "a.py").write_text("1\n", encoding="utf-8")
    assert engine.capture_file_scopes() == 0
    assert "file_channel/plugins" in engine.status.last_error


def test_engine_pull_transport_failure_backs_off(tmp_path: Path) -> None:
    """拉取故障独立退避：last_error 落字 + 退避期内零重试（due-gate 早退）。"""
    reader_fc = _reader_config(tmp_path, frozenset({"plugins"}))
    calls: list[tuple[str, str | None]] = []

    async def transport(scope: str, basis: str | None) -> bytes | None:
        calls.append((scope, basis))
        raise RuntimeError("relay down")

    engine, _ = _build_engine(tmp_path, READER, file_channel=reader_fc, file_transport=transport)
    asyncio.run(engine.pull_file_scopes())
    assert len(calls) == 1
    assert engine.status.last_error.startswith("file_pull/plugins")
    asyncio.run(engine.pull_file_scopes())  # 退避期内：零 transport 调用
    assert len(calls) == 1


# ── server 装配面（settings/env 解析）────────────────────────


def test_parse_scope_list_filters_unknown_and_dupes(monkeypatch: pytest.MonkeyPatch) -> None:
    import server as agent_server

    monkeypatch.delenv("AGENTOS_SYNC_FILE_OWNER_SCOPES", raising=False)
    monkeypatch.delenv("AGENTOS_SYNC_FILE_READER_SCOPES", raising=False)
    assert agent_server._parse_scope_list("plugins, skills ,plugins") == ["plugins", "skills"]
    assert agent_server._parse_scope_list("bogus") == []
    assert agent_server._parse_scope_list("") == []


def test_resolve_scope_root_defaults_and_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server as agent_server

    monkeypatch.delenv("AGENTOS_SYNC_FILE_OWNER_SCOPES", raising=False)
    monkeypatch.delenv("AGENTOS_SYNC_FILE_READER_SCOPES", raising=False)
    assert agent_server._resolve_scope_root({}, "plugins", tmp_path) == Path.cwd() / "plugins/shared"
    assert agent_server._resolve_scope_root({}, "skills", tmp_path) == Path.cwd() / "skills"
    assert agent_server._resolve_scope_root({}, "artifact_index", tmp_path) == (
        tmp_path / "sync_agent" / "artifact_index"
    )
    override = tmp_path / "tree"
    assert agent_server._resolve_scope_root(
        {"file_root_plugins": str(override)}, "plugins", tmp_path
    ) == override
    assert agent_server._resolve_scope_root(
        {"file_root_artifact_index": "rel/idx"}, "artifact_index", tmp_path
    ) == Path.cwd() / "rel/idx"


def test_build_file_channel_config_owner_reader_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server as agent_server

    monkeypatch.delenv("AGENTOS_SYNC_FILE_OWNER_SCOPES", raising=False)
    monkeypatch.delenv("AGENTOS_SYNC_FILE_READER_SCOPES", raising=False)
    src = tmp_path / "tree"
    src.mkdir()
    data_root = tmp_path / "data"
    # 属主域带现存源目录；读者域缺省 = 词表全集减属主域。
    cfg = agent_server._build_file_channel_config(
        {"file_owner_scopes": "plugins", "file_root_plugins": str(src)}, data_root, OWNER
    )
    assert cfg.owner_scopes == {"plugins"}
    assert cfg.owner_roots == {"plugins": src}
    assert cfg.reader_scopes == {"skills", "artifact_index"}
    assert cfg.repo_root == data_root / "sync_agent" / "file_channel"
    assert cfg.device_id == OWNER
    # 属主源目录缺失 = 该域不捕获（owner_roots 空）；声明属主即不转读者
    # （该域整体不活跃，等根修复，防属主端静默变镜像消费者）。
    cfg_missing = agent_server._build_file_channel_config(
        {"file_owner_scopes": "skills", "file_root_skills": str(tmp_path / "absent")},
        data_root,
        OWNER,
    )
    assert cfg_missing.owner_scopes == frozenset()
    assert cfg_missing.reader_scopes == {"plugins", "artifact_index"}
    # 显式 reader 列表过滤属主域；未知 scope 剔除。
    cfg_reader = agent_server._build_file_channel_config(
        {
            "file_owner_scopes": "plugins",
            "file_root_plugins": str(src),
            "file_reader_scopes": "plugins,skills,bogus",
        },
        data_root,
        OWNER,
    )
    assert cfg_reader.reader_scopes == {"skills"}


def test_build_file_channel_config_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server as agent_server

    monkeypatch.setenv("AGENTOS_SYNC_FILE_OWNER_SCOPES", "artifact_index")
    monkeypatch.setenv("AGENTOS_SYNC_FILE_READER_SCOPES", "")
    data_root = tmp_path / "data"
    (data_root / "sync_agent" / "artifact_index").mkdir(parents=True)
    cfg = agent_server._build_file_channel_config({}, data_root, OWNER)
    assert cfg.owner_scopes == {"artifact_index"}
    assert cfg.reader_scopes == {"plugins", "skills"}


# ── 引擎接线（config 面零回归共存）────────────────────────────


def _engine_config(tmp_path: Path, device: str, **extra: Any) -> AgentConfig:
    return AgentConfig(
        relay_url="http://relay.test/ext/sync_relay",
        token=f"tok-{device}",
        device_id=device,
        device_tier="desktop",
        app_version="1.0.0",
        watch_root=tmp_path / "config",
        poll_interval_secs=1.0,
        heartbeat_interval_secs=30.0,
        **extra,
    )


class _RelayStub:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    async def post(self, url: str, token: str, payload: dict[str, Any]) -> None:
        self.posted.append(payload)

    async def get(self, url: str, token: str) -> dict[str, Any]:
        return {"events": [], "cursor": 0}


class _SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def _build_engine(
    tmp_path: Path,
    device: str,
    *,
    file_channel: FileChannelConfig | None = None,
    file_transport: Any = None,
) -> tuple[SyncAgentEngine, _RelayStub]:
    relay = _RelayStub()
    engine = SyncAgentEngine(
        _engine_config(tmp_path, device, file_channel=file_channel),
        state=AgentStateStore(tmp_path / f"{device}-state.db"),
        outbox=OutboxStore(tmp_path / f"{device}-outbox.jsonl"),
        post_json=relay.post,
        get_json=relay.get,
        sleep_fn=_SleepRecorder(),
        clock_ms=lambda: 1_000_000,
        clock_mono=lambda: 0.0,
        now_fn=lambda: "2026-10-17T00:00:00+00:00",
        file_transport=file_transport,
    )
    return engine, relay


def test_engine_owner_tick_commits_scopes(tmp_path: Path) -> None:
    """属主臂：tick 捕获 scope 变更落 commit；零变更幂等零 commit。"""
    src = tmp_path / "src"
    src.mkdir()
    engine, _ = _build_engine(tmp_path, OWNER, file_channel=_owner_config(tmp_path, {"plugins": src}))
    (src / "a.py").write_text("1\n", encoding="utf-8")
    engine.capture_file_scopes()
    channel = engine.file_channel
    assert channel is not None
    assert channel.commit_count("plugins") == 1
    engine.capture_file_scopes()  # 零变更零 commit（幂等）
    assert channel.commit_count("plugins") == 1
    assert "plugins" in engine.status_dict()["file_channel"]


def test_engine_reader_pulls_via_transport(tmp_path: Path) -> None:
    """读者臂：transport 供给增量 bundle → 镜像落地；读者域捕获零 commit。"""
    src = tmp_path / "src"
    src.mkdir()
    owner_channel = GitFileChannel(
        FileChannelConfig(
            repo_root=tmp_path / "fc-owner",
            owner_roots={"plugins": src},
            reader_scopes=frozenset(),
            device_id=OWNER,
        )
    )
    (src / "a.py").write_text("payload\n", encoding="utf-8")
    owner_channel.capture_commit("plugins")

    reader_fc = _reader_config(tmp_path, frozenset({"plugins"}))

    async def transport(scope: str, basis: str | None) -> bytes | None:
        return owner_channel.create_bundle(scope, basis)

    engine, _ = _build_engine(tmp_path, READER, file_channel=reader_fc, file_transport=transport)
    engine.capture_file_scopes()  # 读者域：无属主域，零动作零异常
    asyncio.run(engine.pull_file_scopes())
    mirror = GitFileChannel(reader_fc).mirror_root("plugins")
    assert (mirror / "a.py").read_text(encoding="utf-8") == "payload\n"


def test_engine_transport_none_is_honest_noop(tmp_path: Path) -> None:
    reader_fc = _reader_config(tmp_path, frozenset({"plugins"}))
    engine, _ = _build_engine(tmp_path, READER, file_channel=reader_fc)
    asyncio.run(engine.pull_file_scopes())  # 无传输面：空转零异常零镜像
    assert not (tmp_path / "fc-reader" / "mirror" / "plugins").exists()


def test_engine_git_missing_degrades_config_face_intact(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """git 可执行缺失 → 通道停用告警一行；config 面捕获零受影响。"""
    src = tmp_path / "src"
    src.mkdir()
    fc = FileChannelConfig(
        repo_root=tmp_path / "fc",
        owner_roots={"plugins": src},
        reader_scopes=frozenset(),
        device_id=OWNER,
        git_exe="git-definitely-not-on-path-xyz",
    )
    with caplog.at_level(logging.WARNING):
        engine, _ = _build_engine(tmp_path, OWNER, file_channel=fc)
    assert engine.file_channel is None  # 通道停用
    assert "file_channel" not in engine.status_dict()
    assert any("git" in r.getMessage().lower() for r in caplog.records)
    # config 面零回归：git 缺失不影响 config 捕获（构造时 config 根未建=空基线）。
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "a.yaml").write_text("k: v\n", encoding="utf-8")
    engine.capture_local_changes()
    assert engine.pending_count == 1


def test_engine_dual_face_coexistence(tmp_path: Path) -> None:
    """config yaml 面与 git 通道共存一拍：各自产出、互不干扰。"""
    src = tmp_path / "src"
    src.mkdir()
    fc = FileChannelConfig(
        repo_root=tmp_path / "fc",
        owner_roots={"plugins": src},
        reader_scopes=frozenset(),
        device_id=OWNER,
    )
    engine, relay = _build_engine(tmp_path, OWNER, file_channel=fc)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "a.yaml").write_text("k: v\n", encoding="utf-8")
    (src / "a.py").write_text("1\n", encoding="utf-8")
    engine.capture_local_changes()
    engine.capture_file_scopes()
    # config 面：sync_event 上行原样（既有路径零回归）。
    config_events = [
        e
        for batch in relay.posted
        for e in batch.get("events", [])
        if e.get("asset_type") == "config"
    ]
    assert config_events == []  # 捕获只进 outbox，flush 才上行；此拍未 flush
    assert engine.pending_count == 1
    # git 面：commit 落仓库（历史/墓碑走 git）。
    assert engine.file_channel is not None
    assert engine.file_channel.commit_count("plugins") == 1
