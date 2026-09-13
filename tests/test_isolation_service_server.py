# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入 | @ci: python-coverage
"""isolation_service server.py 接口适配层测试。

覆盖：工具面参数翻译与结果投影（stub 管理器记录调用）、未初始化守卫、
checkpoint/permission 真实依赖行为、on_load/on_unload 生命周期、
配置热更新 watcher 的重建/容错路径。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ISOLATION_DIR = _REPO_ROOT / "plugins" / "shared" / "system" / "isolation"
_SERVER_PATH = _ISOLATION_DIR / "server.py"

# 平铺裸名逐出（server/manager/checkpoint/wsl_health 跨插件同名），保证本文件
# 的模块级加载确定解析到 isolation 插件目录（同 tests/_isolation_path.py 纪律）。
for _bare in ("server", "manager", "checkpoint", "wsl_health"):
    sys.modules.pop(_bare, None)

_spec = importlib.util.spec_from_file_location("isolation_service_server_under_test", _SERVER_PATH)
server = importlib.util.module_from_spec(_spec)
sys.modules["isolation_service_server_under_test"] = server
_spec.loader.exec_module(server)

from checkpoint import CheckpointManager  # noqa: E402

from agentos_plugin_sdk.isolation_types import IsolationLevel, OperationType, TaskType  # noqa: E402
from agentos_plugin_sdk.permission_checker import PermissionChecker  # noqa: E402

# ═══════════════════════════════════════════════════════════
# 替身：IsolationManager（外部容器子系统的进程边界替身）
# ═══════════════════════════════════════════════════════════


def _make_env(env_id: str = "env-1", level: str = "isolated", task_id: str = "t1") -> SimpleNamespace:
    return SimpleNamespace(
        env_id=env_id,
        level=IsolationLevel(level),
        provider_type="host",
        status="ready",
        context=SimpleNamespace(task_id=task_id),
    )


class _StubManager:
    """记录调用并返回预设环境；不触碰容器运行时。"""

    instances: list[_StubManager] = []

    def __init__(self, providers_config_override: dict | None = None) -> None:
        self.providers_config_override = providers_config_override
        self.started = False
        self.stopped = False
        self.calls: list[tuple[str, dict]] = []
        self.env = _make_env()
        self.envs: list[SimpleNamespace] = [self.env]
        self.stats: dict = {"total_environments": 0}
        _StubManager.instances.append(self)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def get_or_create_environment(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(("get_or_create", kwargs))
        return self.env

    async def execute_in_isolation(self, **kwargs: object) -> SimpleNamespace:
        self.calls.append(("execute", kwargs))
        return SimpleNamespace(to_dict=lambda: {"exit_code": 0, "stdout": "done"})

    async def destroy_by_task_id(self, task_id: str, success: bool = True) -> None:
        self.calls.append(("destroy_task", {"task_id": task_id, "success": success}))

    async def destroy_environment(self, env_id: str, success: bool = True) -> bool:
        self.calls.append(("destroy_env", {"env_id": env_id, "success": success}))
        return True

    async def list_environments(self, task_id: str | None = None, level: IsolationLevel | None = None) -> list:
        self.calls.append(("list", {"task_id": task_id, "level": level}))
        return self.envs

    def get_stats(self) -> dict:
        return self.stats


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch: pytest.MonkeyPatch):
    """每用例前后复位模块级全局，防跨用例泄漏。"""
    _StubManager.instances.clear()
    monkeypatch.setattr(server, "_manager", None)
    monkeypatch.setattr(server, "_checkpoint_mgr", None)
    monkeypatch.setattr(server, "_permission_checker", None)
    monkeypatch.setattr(server, "_config_watcher_task", None)


# ═══════════════════════════════════════════════════════════
# 未初始化守卫
# ═══════════════════════════════════════════════════════════


class TestUninitializedGuards:
    async def test_manager_tools_fail_closed_when_uninitialized(self) -> None:
        for coro in (
            server.isolation_create_env(task_id="t1"),
            server.isolation_execute(task_id="t1", operation={"cmd": "ls"}),
            server.isolation_destroy_env(env_id="e1"),
            server.isolation_list_envs(),
            server.isolation_stats(),
        ):
            result = await coro
            assert result == {"error": "隔离服务未初始化"}

    async def test_checkpoint_fails_closed_when_uninitialized(self) -> None:
        assert await server.isolation_checkpoint("create") == {"error": "检查点管理器未初始化"}

    async def test_check_policy_fails_closed_when_uninitialized(self) -> None:
        assert await server.isolation_check_policy("/x", "read") == {"error": "权限检查器未初始化"}


# ═══════════════════════════════════════════════════════════
# 工具面：参数翻译与结果投影
# ═══════════════════════════════════════════════════════════


class TestCreateEnv:
    async def test_full_params_translated_and_env_projected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        monkeypatch.setattr(server, "_manager", stub)

        result = await server.isolation_create_env(
            task_id="t9",
            task_type="project",
            operation_type="code_execution",
            workspace="/ws",
            parent_workspace="/pws",
            parent_env_id="env-p",
            isolation_level="non_isolated",
            metadata={"k": "v"},
            parent_task_id="t0",
            tool_name="bash",
        )

        assert result == {
            "env_id": "env-1",
            "level": "isolated",
            "provider_type": "host",
            "status": "ready",
            "context_task_id": "t1",
        }
        (_op, kw) = stub.calls[0]
        assert kw["task_id"] == "t9"
        assert kw["task_type"] is TaskType.PROJECT
        assert kw["operation_type"] is OperationType.CODE_EXECUTION
        assert kw["isolation_level"] is IsolationLevel.HOST
        assert kw["metadata"] == {"k": "v"}
        assert kw["parent_task_id"] == "t0"
        assert kw["tool_name"] == "bash"

    async def test_optional_enums_left_as_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        monkeypatch.setattr(server, "_manager", stub)
        await server.isolation_create_env(task_id="t1")
        (_op, kw) = stub.calls[0]
        assert kw["isolation_level"] is None
        assert kw["operation_type"] is None
        assert kw["task_type"] is TaskType.ATOMIC  # schema 默认 atomic


class TestExecuteAndStats:
    async def test_execute_returns_result_dict_verbatim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        monkeypatch.setattr(server, "_manager", stub)
        result = await server.isolation_execute(
            task_id="t1", operation={"command": "ls"}, isolation_level="isolated", task_type="module"
        )
        assert result == {"exit_code": 0, "stdout": "done"}
        (_op, kw) = stub.calls[0]
        assert kw["operation"] == {"command": "ls"}
        assert kw["task_type"] is TaskType.MODULE
        assert kw["isolation_level"] is IsolationLevel.CONTAINER

    async def test_stats_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        stub.stats = {"total_environments": 3}
        monkeypatch.setattr(server, "_manager", stub)
        assert await server.isolation_stats() == {"total_environments": 3}


class TestDestroyEnv:
    async def test_destroy_by_task_id_takes_precedence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        monkeypatch.setattr(server, "_manager", stub)
        result = await server.isolation_destroy_env(env_id="e1", task_id="t1", success=False)
        assert result == {"destroyed": True, "task_id": "t1"}
        assert stub.calls == [("destroy_task", {"task_id": "t1", "success": False})]

    async def test_destroy_by_env_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        monkeypatch.setattr(server, "_manager", stub)
        result = await server.isolation_destroy_env(env_id="e7")
        assert result == {"destroyed": True, "env_id": "e7"}
        assert stub.calls == [("destroy_env", {"env_id": "e7", "success": True})]

    async def test_destroy_without_target_is_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        monkeypatch.setattr(server, "_manager", stub)
        assert await server.isolation_destroy_env() == {"error": "必须提供 env_id 或 task_id"}
        assert stub.calls == []


class TestListEnvs:
    async def test_filters_translated_and_envs_projected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        stub.envs = [_make_env("e1", "isolated", "t1"), _make_env("e2", "non_isolated", "t2")]
        monkeypatch.setattr(server, "_manager", stub)

        result = await server.isolation_list_envs(task_id="t2", level="non_isolated")

        assert result["total"] == 2
        assert [e["env_id"] for e in result["environments"]] == ["e1", "e2"]
        assert result["environments"][1]["level"] == "non_isolated"
        (_op, kw) = stub.calls[0]
        assert kw == {"task_id": "t2", "level": IsolationLevel.HOST}

    async def test_no_filters_pass_none_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _StubManager()
        monkeypatch.setattr(server, "_manager", stub)
        await server.isolation_list_envs()
        (_op, kw) = stub.calls[0]
        assert kw == {"task_id": None, "level": None}


# ═══════════════════════════════════════════════════════════
# check_policy / checkpoint：真实依赖行为
# ═══════════════════════════════════════════════════════════


class TestCheckPolicy:
    @pytest.fixture
    def checker(self, monkeypatch: pytest.MonkeyPatch) -> PermissionChecker:
        checker = PermissionChecker()
        monkeypatch.setattr(server, "_permission_checker", checker)
        return checker

    async def test_read_inside_workspace_allowed(self, checker: PermissionChecker, tmp_path: Path) -> None:
        target = tmp_path / "a.txt"
        target.write_text("x")
        result = await server.isolation_check_policy(str(target), "read", workspace=str(tmp_path))
        assert result == {"allowed": True, "message": ""}

    async def test_write_inside_workspace_allowed(self, checker: PermissionChecker, tmp_path: Path) -> None:
        result = await server.isolation_check_policy(str(tmp_path / "b.txt"), "write", workspace=str(tmp_path))
        assert result["allowed"] is True

    async def test_write_outside_workspace_rejected(self, checker: PermissionChecker, tmp_path: Path) -> None:
        outside = tmp_path.parent / f"outside-{os.getpid()}.txt"
        result = await server.isolation_check_policy(str(outside), "write", workspace=str(tmp_path))
        assert result["allowed"] is False
        assert "权限拒绝" in result["message"]


class TestCheckpointTool:
    @pytest.fixture
    def mgr(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CheckpointManager:
        mgr = CheckpointManager(project_root=str(tmp_path / "root"))
        monkeypatch.setattr(server, "_checkpoint_mgr", mgr)
        return mgr

    async def test_create_requires_task_and_workspace(self, mgr: CheckpointManager) -> None:
        assert await server.isolation_checkpoint("create") == {"error": "create 需要 task_id 和 workspace"}
        assert await server.isolation_checkpoint("create", task_id="t1") == {
            "error": "create 需要 task_id 和 workspace"
        }

    async def test_create_restore_cleanup_lifecycle(self, mgr: CheckpointManager, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("hello")

        created = await server.isolation_checkpoint(
            "create", task_id="task-1", workspace=str(ws), files_to_backup=["a.txt"]
        )
        assert created == {"created": True, "checkpoint_id": "task-1"}

        (ws / "a.txt").write_text("changed")
        restored = await server.isolation_checkpoint("restore", task_id="task-1")
        assert restored == {"restored": True}
        assert (ws / "a.txt").read_text() == "hello"  # 真实回滚生效

        cleaned = await server.isolation_checkpoint("cleanup", task_id="task-1")
        assert cleaned == {"cleaned": True}

    async def test_restore_and_cleanup_require_task_id(self, mgr: CheckpointManager) -> None:
        assert await server.isolation_checkpoint("restore") == {"error": "restore 需要 task_id"}
        assert await server.isolation_checkpoint("cleanup") == {"error": "cleanup 需要 task_id"}

    async def test_restore_unknown_task_reports_not_restored(self, mgr: CheckpointManager) -> None:
        assert await server.isolation_checkpoint("restore", task_id="ghost") == {"restored": False}

    async def test_list_reports_checkpoints(self, mgr: CheckpointManager, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.txt").write_text("x")
        await server.isolation_checkpoint("create", task_id="task-1", workspace=str(ws), files_to_backup=["a.txt"])
        result = await server.isolation_checkpoint("list")
        assert result["total"] == 1
        assert result["checkpoints"][0]["task_id"] == "task-1"

    async def test_unknown_action_rejected(self, mgr: CheckpointManager) -> None:
        assert await server.isolation_checkpoint("rebase") == {"error": "未知操作: rebase"}


# ═══════════════════════════════════════════════════════════
# on_load / on_unload 生命周期
# ═══════════════════════════════════════════════════════════


class TestLifecycle:
    async def test_on_load_boots_all_components_and_on_unload_tears_down(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(server, "IsolationManager", _StubManager)
        monkeypatch.setattr(
            server.plugin,
            "get_config",
            lambda: {"isolation": {"isolation_config": {"providers": {"host": {"limits": {"max_environments": 2}}}}}},
        )
        keepalive_calls: list[bool] = []
        monkeypatch.setattr(server.wsl_health, "terminate_keepalive", lambda: keepalive_calls.append(True))

        await server._on_load({})

        assert len(_StubManager.instances) == 1
        mgr = server._manager
        assert isinstance(mgr, _StubManager)
        assert mgr.started
        # providers 配置经 extract_providers_config 从注入快照提取
        assert mgr.providers_config_override == {"providers": {"host": {"limits": {"max_environments": 2}}}}
        assert isinstance(server._checkpoint_mgr, CheckpointManager)
        assert isinstance(server._permission_checker, PermissionChecker)
        watcher = server._config_watcher_task
        assert watcher is not None
        assert not watcher.done()

        await server._on_unload({})

        assert mgr.stopped
        assert server._manager is None
        assert server._checkpoint_mgr is None
        assert server._permission_checker is None
        assert server._config_watcher_task is None
        assert watcher.cancelled()  # watcher 被取消收敛，不常驻事件循环
        assert keepalive_calls == [True]  # WSL 保活会话随卸载终止


# ═══════════════════════════════════════════════════════════
# 配置热更新 watcher
# ═══════════════════════════════════════════════════════════


_CFG_PATH = _REPO_ROOT / "config" / "isolation" / "isolation_config.yaml"


def _gate_sleep(n: int) -> tuple[list[dict[str, asyncio.Event]], object]:
    """把 while True 轮询的 sleep 变成双向栅栏：watcher 到达第 i 轮 sleep 时置
    barriers[i]["reached"]，测试断言完置 ["go"] 放行下一轮；轮次用尽即收敛退出。"""
    barriers = [{"reached": asyncio.Event(), "go": asyncio.Event()} for _ in range(n)]
    state = {"i": 0}

    async def fake_sleep(_seconds: float) -> None:
        i = state["i"]
        state["i"] += 1
        if i < n:
            barriers[i]["reached"].set()
            await barriers[i]["go"].wait()
        else:
            raise asyncio.CancelledError  # 轮次用尽：收敛退出

    return barriers, fake_sleep


class TestConfigWatcher:
    async def test_reloads_manager_on_config_mtime_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert _CFG_PATH.exists(), "被 watch 的隔离配置文件应存在"
        monkeypatch.setattr(server, "IsolationManager", _StubManager)

        barriers, fake_sleep = _gate_sleep(2)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        old = _StubManager()
        monkeypatch.setattr(server, "_manager", old)

        orig_ns = _CFG_PATH.stat().st_mtime_ns
        task: asyncio.Task | None = None
        try:
            task = asyncio.create_task(server._watch_config_reload())
            # 第 1 轮完成：基线 mtime 已记录，watcher 停在栅栏 0
            await asyncio.wait_for(barriers[0]["reached"].wait(), timeout=5)
            assert server._manager is old  # 未变更不重建

            bumped = orig_ns + 3_600_000_000_000  # +1h，绕开文件系统 mtime 粒度
            os.utime(_CFG_PATH, ns=(bumped, bumped))

            barriers[0]["go"].set()
            # 第 2 轮完成：重建 + 旧管理器停机，watcher 停在栅栏 1
            await asyncio.wait_for(barriers[1]["reached"].wait(), timeout=5)
            assert len(_StubManager.instances) == 2  # 旧 + 新各一
            assert server._manager is _StubManager.instances[-1]
            assert server._manager.started
            assert old.stopped  # 旧管理器被替换后停机
            barriers[1]["go"].set()
        finally:
            os.utime(_CFG_PATH, ns=(orig_ns, orig_ns))  # 还原仓库文件 mtime
            for b in barriers:
                b["go"].set()
        # 第 3 轮 sleep 抛 CancelledError，任务收敛（await 取消任务会向
        # 调用方透传 CancelledError，此处为预期收敛路径需吞掉）
        assert task is not None
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert task.cancelled()

    async def test_survives_stat_oserror_and_keeps_polling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_stat = Path.stat
        flaky = {"n": 0}

        def flaky_stat(self: Path, *a: object, **kw: object):
            if self == _CFG_PATH and flaky["n"] == 0:
                flaky["n"] += 1
                raise OSError("atomic write window")
            return real_stat(self, *a, **kw)

        monkeypatch.setattr(Path, "stat", flaky_stat)
        monkeypatch.setattr(asyncio, "sleep", _gate_sleep(0)[1])  # 首轮 sleep 即取消

        task = asyncio.create_task(server._watch_config_reload())
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
        assert task.cancelled()  # OSError 被吞掉后继续走到 sleep 栅栏退出
        assert flaky["n"] == 1
