"""容器并发创建竞态回归测试。

背景 BUG：同一 workspace 的多个并发任务同时调用 get_or_create_environment，
因「查找→创建→写缓存」无原子性保证（check-then-act 竞态），两个任务都通过
「容器不存在」检查后各自 docker create 同名容器，第二个触发
`Conflict. The container name "/cua-xxx" is already in use`。

修复（见 src/isolation/manager.py）：
- 新增 per-workspace 锁 _ws_locks，_get_ws_lock(ws_key) 对同 ws_key 返回同一把锁。
- get_or_create_environment 用 `async with self._get_ws_lock(ws_key)` 包裹整个
  「查内存缓存→查 Docker→创建→写缓存」流程，串行化同 workspace 的并发请求。

本测试锁定核心契约：同 workspace 并发创建时，provider.create_environment 只被
调用一次；不同 workspace 互不阻塞。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from isolation.manager import IsolationManager
from isolation.types import (
    EnvironmentStatus,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    OperationType,
    TaskType,
)


def _make_env(env_id: str = "cua-ws") -> IsolationEnvironment:
    return IsolationEnvironment(
        env_id=env_id,
        level=IsolationLevel.CONTAINER,
        provider_type="docker",
        status=EnvironmentStatus.READY.value,
        context=IsolationContext(
            task_id="t1", task_type=TaskType.ATOMIC, is_root_task=True,
        ),
    )


def _manager_with_provider(create_fn) -> IsolationManager:
    """构造注入 mock provider 的 IsolationManager。

    provider.is_available 返回容器可用；_find_existing_container 返回 None
    （模拟 Docker 中无该容器），迫使走 create 分支。
    """
    manager = IsolationManager(providers={})
    provider = MagicMock()
    provider.is_available = AsyncMock(return_value=(True, None))
    provider.create_environment = create_fn
    manager._providers[IsolationLevel.CONTAINER] = provider
    # 绕过 Docker 查找：直接返回 None
    manager._find_existing_container = AsyncMock(return_value=None)
    manager._check_providers_availability = AsyncMock(
        return_value={IsolationLevel.CONTAINER: (True, None)}
    )
    return manager


# ---------------------------------------------------------------------------
# 1. 同 workspace 并发：create_environment 只被调用一次（核心契约）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_same_workspace_creates_once():
    """同 workspace 的两个并发任务：provider.create_environment 只调用一次。

    若无 per-workspace 锁，两个任务会各自调 create_environment，第二个报 Conflict。
    create_fn 内含 await 模拟 docker create 的耗时（让出事件循环），
    放大竞态窗口——这是修复前必然触发冲突的场景。
    """
    call_count = 0

    async def slow_create(context, container_name=None):
        nonlocal call_count
        call_count += 1
        # 关键：await 让出事件循环，让第二个并发任务有机会进入「检查」阶段。
        # 无锁时第二个任务会在此期间通过检查并再次调用本函数。
        await asyncio.sleep(0.05)
        return _make_env(env_id=container_name)

    manager = _manager_with_provider(slow_create)

    ws = "/proj/workspace_abc__wt_2705dda2"
    # 两个任务同一 workspace 并发
    results = await asyncio.gather(
        manager.get_or_create_environment(
            task_id="t1", task_type=TaskType.ATOMIC, workspace=ws,
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash_execute",
        ),
        manager.get_or_create_environment(
            task_id="t2", task_type=TaskType.ATOMIC, workspace=ws,
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash_execute",
        ),
    )

    assert call_count == 1, f"create_environment 应只调用一次，实际 {call_count} 次（竞态未修复）"
    # 两个任务拿到同一个 env_id
    assert results[0].env_id == results[1].env_id


# ---------------------------------------------------------------------------
# 2. 不同 workspace 并发：互不阻塞，各自独立创建
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_different_workspaces_dont_block():
    """不同 workspace 的并发任务应各自独立创建容器，不被对方的锁阻塞。"""
    created_names: list[str] = []

    async def create_fn(context, container_name=None):
        created_names.append(container_name)
        await asyncio.sleep(0.05)
        return _make_env(env_id=container_name)

    manager = _manager_with_provider(create_fn)

    await asyncio.gather(
        manager.get_or_create_environment(
            task_id="t1", task_type=TaskType.ATOMIC,
            workspace="/proj/ws_a__wt_11111111",
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash_execute",
        ),
        manager.get_or_create_environment(
            task_id="t2", task_type=TaskType.ATOMIC,
            workspace="/proj/ws_b__wt_22222222",
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash_execute",
        ),
    )

    assert sorted(created_names) == sorted([
        "cua-ws_a__wt_11111111", "cua-ws_b__wt_22222222",
    ])


# ---------------------------------------------------------------------------
# 3. per-workspace 锁：同 ws_key 恒返回同一把锁
# ---------------------------------------------------------------------------


def test_get_ws_lock_returns_same_lock_for_same_key():
    """_get_ws_lock 对同一 ws_key 返回同一把锁，不同 key 返回不同锁。"""
    manager = IsolationManager(providers={})
    lock_a1 = manager._get_ws_lock("ws_a")
    lock_a2 = manager._get_ws_lock("ws_a")
    lock_b = manager._get_ws_lock("ws_b")

    assert lock_a1 is lock_a2, "同一 ws_key 必须返回同一把锁"
    assert lock_a1 is not lock_b, "不同 ws_key 必须返回不同锁"
    assert isinstance(lock_a1, asyncio.Lock)


# ---------------------------------------------------------------------------
# 4. 内存缓存命中：第二个任务不再触碰 Docker（_find_existing_container 不被调用）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_concurrent_task_hits_cache_not_docker():
    """同 workspace 第二个并发任务应命中内存缓存，不调用 _find_existing_container。"""
    find_count = 0

    async def create_fn(context, container_name=None):
        await asyncio.sleep(0.05)
        return _make_env(env_id=container_name)

    manager = _manager_with_provider(create_fn)
    # 包装 _find_existing_container 计数
    original_find = manager._find_existing_container

    async def counting_find(name):
        nonlocal find_count
        find_count += 1
        return await original_find(name)

    manager._find_existing_container = counting_find

    ws = "/proj/workspace_abc__wt_2705dda2"
    await asyncio.gather(
        manager.get_or_create_environment(
            task_id="t1", task_type=TaskType.ATOMIC, workspace=ws,
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash_execute",
        ),
        manager.get_or_create_environment(
            task_id="t2", task_type=TaskType.ATOMIC, workspace=ws,
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash_execute",
        ),
    )

    # 串行化后：第一个任务查 Docker（1 次），第二个命中内存缓存（0 次额外查找）
    assert find_count == 1, f"应只查 Docker 一次，实际 {find_count} 次"
