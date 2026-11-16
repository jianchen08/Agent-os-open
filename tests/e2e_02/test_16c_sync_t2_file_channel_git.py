# @feature: FP-0.2.多端同步 T2.文件类通道git化 | @ci: none-local
"""T2 轻量车道（2026-10-17 用户裁定轻量口径；跨端完整链路留 T3 统一验收）。

单实例（属主引擎）+ 进程内模拟第二节点（读者引擎），真实依赖验证
commit→fetch→fast-forward pull 链路关键断言 + config 面零回归：

- **真实 git 子进程**全程（变更检测=commit/删除=墓碑/版本历史/增量 bundle/
  非 ff 原生拒绝，零手写指纹/版本链/墓碑）；
- **真实 SyncAgentEngine 双臂**（属主臂 capture_file_scopes 落 commit；读者臂
  pull_file_scopes 经传输缝取 bundle 物化镜像）——传输缝 = FileTransport
  端口（进程内直连属主 create_bundle；relay bundle 传输 = 下一刀同缝接线）；
- **config 面零回归**：同拍共存时 config yaml 变更仍走既有 sync_event 路径
  （outbox 事件形态不变），git scope 各自落 commit。

门控：``AGENTOS_T2=1`` 才实跑（真实 git × 多次调用；注册面/通道原语/
引擎臂单测为进程内回归面）。

实跑：``AGENTOS_T2=1 python -m pytest tests/e2e_02/test_16c_sync_t2_file_channel_git.py -v``
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT / "plugins" / "shared" / "sync_agent"), str(_REPO_ROOT / "plugins" / "shared")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agent_core import AgentConfig, SyncAgentEngine  # noqa: E402
from agent_state import AgentStateStore  # noqa: E402
from file_channel import FileChannelConfig  # noqa: E402
from outbox_store import OutboxStore  # noqa: E402

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("AGENTOS_T2") != "1",
        reason="T2 轻量车道：设 AGENTOS_T2=1 实跑（属主/读者引擎进程内 + 真实 git）",
    ),
]

OWNER = "desktop-a"
READER = "phone-b"


def _engine(
    tmp_path: Path,
    device: str,
    *,
    file_channel: FileChannelConfig,
    file_transport: Callable[[str, str | None], Awaitable[bytes | None]] | None = None,
) -> SyncAgentEngine:
    return SyncAgentEngine(
        AgentConfig(
            relay_url="http://relay.test/ext/sync_relay",
            token=f"tok-{device}",
            device_id=device,
            device_tier="desktop" if device == OWNER else "phone",
            app_version="1.0.0",
            watch_root=tmp_path / "config",
            poll_interval_secs=1.0,
            heartbeat_interval_secs=30.0,
            file_channel=file_channel,
        ),
        state=AgentStateStore(tmp_path / device / "state.db"),
        outbox=OutboxStore(tmp_path / device / "outbox.jsonl"),
        sleep_fn=_NoSleep(),
        clock_ms=lambda: 1_000_000,
        clock_mono=lambda: 0.0,
        now_fn=lambda: "2026-10-17T00:00:00+00:00",
        file_transport=file_transport,
    )


class _NoSleep:
    async def __call__(self, delay: float) -> None:
        del delay


def _owner_channel(tmp_path: Path) -> tuple[FileChannelConfig, Path, Path]:
    plugins_src = tmp_path / "tree" / "plugins" / "shared"
    skills_src = tmp_path / "tree" / "skills"
    plugins_src.mkdir(parents=True)
    skills_src.mkdir(parents=True)
    fc = FileChannelConfig(
        repo_root=tmp_path / OWNER / "file_channel",
        owner_roots={"plugins": plugins_src, "skills": skills_src},
        reader_scopes=frozenset(),
        device_id=OWNER,
    )
    return fc, plugins_src, skills_src


def _reader_channel(tmp_path: Path) -> FileChannelConfig:
    return FileChannelConfig(
        repo_root=tmp_path / READER / "file_channel",
        owner_roots={},
        reader_scopes=frozenset({"plugins", "skills"}),
        device_id=READER,
    )


def _transport_of(owner_engine: SyncAgentEngine) -> Callable[[str, str | None], Awaitable[bytes | None]]:
    channel = owner_engine.file_channel
    assert channel is not None

    async def transport(scope: str, basis: str | None) -> bytes | None:
        return channel.create_bundle(scope, basis)

    return transport


def test_t2_commit_fetch_ff_pull_chain_with_tombstone(tmp_path: Path) -> None:
    """commit→fetch→ff-pull 全链：首拉全量收敛 → 增量前置头 → 墓碑镜像落地。"""
    owner_fc, plugins_src, skills_src = _owner_channel(tmp_path)
    owner = _engine(tmp_path, OWNER, file_channel=owner_fc)
    reader_fc = _reader_channel(tmp_path)
    reader = _engine(
        tmp_path, READER, file_channel=reader_fc, file_transport=_transport_of(owner)
    )
    owner_channel = owner.file_channel
    reader_channel = reader.file_channel
    assert owner_channel is not None
    assert reader_channel is not None

    # ① 属主捕获：插件文件 + 技能文件 → 各一 commit（变更检测 = git）。
    (plugins_src / "demo_plug.py").write_text("v1\n", encoding="utf-8")
    (skills_src / "demo").mkdir()
    (skills_src / "demo" / "SKILL.md").write_text("# demo\n", encoding="utf-8")
    assert owner.capture_file_scopes() == 2
    assert owner_channel.commit_count("plugins") == 1
    assert owner_channel.commit_count("skills") == 1

    # ② 读者首拉（basis=None 全量 bundle）→ 双域镜像收敛、head 对齐。
    asyncio.run(reader.pull_file_scopes())
    for scope in ("plugins", "skills"):
        mirror = reader_channel.mirror_root(scope)
        assert mirror.is_dir()
        assert any(mirror.iterdir())
        assert reader_channel.head(scope) == owner_channel.head(scope)
    assert (reader_channel.mirror_root("plugins") / "demo_plug.py").read_text(
        encoding="utf-8"
    ) == "v1\n"

    # ③ 增量轮：改插件文件 + 删技能文件（墓碑）→ 读者 ff 更新含删除落地。
    basis = {s: reader_channel.head(s) for s in ("plugins", "skills")}
    (plugins_src / "demo_plug.py").write_text("v2\n", encoding="utf-8")
    (skills_src / "demo" / "SKILL.md").unlink()
    assert owner.capture_file_scopes() == 2
    asyncio.run(reader.pull_file_scopes())
    assert (reader_channel.mirror_root("plugins") / "demo_plug.py").read_text(
        encoding="utf-8"
    ) == "v2\n"
    assert not (reader_channel.mirror_root("skills") / "demo" / "SKILL.md").exists()
    for scope, base in basis.items():
        assert base is not None
        incremental = owner_channel.create_bundle(scope, base)
        assert incremental is not None
        assert incremental.startswith(b"# v2 git bundle")
        assert b"\n-" + base.encode() in incremental  # 前置依赖 = 增量而非全量
        assert reader_channel.head(scope) == owner_channel.head(scope)

    # ④ 零本地提交：读者历史深度恒等于属主（fetch 只移 ref，不造 commit）。
    for scope in ("plugins", "skills"):
        assert reader_channel.commit_count(scope) == owner_channel.commit_count(scope) == 2

    # ⑤ 状态面：双端 file_channel 面角色/头可观测。
    owner_status = owner.status_dict()["file_channel"]
    reader_status = reader.status_dict()["file_channel"]
    assert owner_status["plugins"] == {
        "role": "owner",
        "head": owner_channel.head("plugins")[:12],
        "commits": 2,
    }
    assert reader_status["skills"]["role"] == "reader"
    assert reader_status["skills"]["commits"] == 2


def test_t2_config_face_zero_regression_coexistence(tmp_path: Path) -> None:
    """config yaml 面与 git 通道同拍共存：config 事件形态不变 + git 各自 commit。"""
    owner_fc, plugins_src, _ = _owner_channel(tmp_path)
    owner = _engine(tmp_path, OWNER, file_channel=owner_fc)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "settings.yaml").write_bytes(b"k: v1\n")
    (plugins_src / "plug.py").write_text("p\n", encoding="utf-8")
    owner.capture_local_changes()
    owner.capture_file_scopes()
    # config 面：既有 outbox 路径原样（asset_type=config + payload.content 全文）。
    pending = owner._outbox.peek(10)  # noqa: SLF001 —— 车道观察 outbox 内容面
    config_events = [e for e in pending if e.get("asset_type") == "config"]
    assert len(config_events) == 1
    assert config_events[0]["payload"]["content"] == "k: v1\n"
    assert config_events[0]["asset_id"] == "settings.yaml"
    assert config_events[0]["device_id"] == OWNER
    # git 面：插件域 commit 落仓库（与 config 事件互不串道）。
    channel = owner.file_channel
    assert channel is not None
    assert channel.commit_count("plugins") == 1
    assert channel.head("skills") is None  # 技能源空树零 commit
    # 重复拍：双面均幂等（config 基线比对无新事件；git status 干净零 commit）。
    owner.capture_local_changes()
    owner.capture_file_scopes()
    assert owner._outbox.peek(10) == pending
    assert channel.commit_count("plugins") == 1
