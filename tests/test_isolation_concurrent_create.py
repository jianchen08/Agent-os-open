# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入
import tests._isolation_path  # noqa: F401

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
from agentos_plugin_sdk.isolation_types import (
    EnvironmentStatus,
    IsolationContext,
    IsolationEnvironment,
    IsolationLevel,
    OperationType,
    TaskType,
)
from manager import IsolationManager


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

    provider.is_available 返回容器可用；_find_existing_container 模拟真 daemon
    语义：容器创建前查无（None）、创建后按名可寻——复用快路径的每轮验活
    （B5 收尾）依赖此真值：登记 READY 而 daemon 查无 = 死登记，须清登记重建。
    """
    manager = IsolationManager(providers={})
    provider = MagicMock()
    provider.is_available = AsyncMock(return_value=(True, None))
    daemon_names: set[str] = set()

    async def create_and_register(context, container_name=None):
        env = await create_fn(context, container_name=container_name)
        daemon_names.add(env.env_id)
        return env

    provider.create_environment = create_and_register
    manager._providers[IsolationLevel.CONTAINER] = provider

    async def find_by_name(name):
        return _make_env(env_id=name) if name in daemon_names else None

    manager._find_existing_container = AsyncMock(side_effect=find_by_name)
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
    放大竞态窗口——两个任务都会进入「检查」阶段再调用本函数。
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
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash",
        ),
        manager.get_or_create_environment(
            task_id="t2", task_type=TaskType.ATOMIC, workspace=ws,
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash",
        ),
    )

    # provider 是外部 docker 边界：同 workspace 恰好一次 create_environment
    # （一个容器）即防竞态契约的外部可观察面。
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
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash",
        ),
        manager.get_or_create_environment(
            task_id="t2", task_type=TaskType.ATOMIC,
            workspace="/proj/ws_b__wt_22222222",
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash",
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
# 4. 内存缓存命中：第二个任务仅做一次 daemon 验活探针（B5 收尾每轮验活契约）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_second_concurrent_task_reuses_via_liveness_probe():
    """同 workspace 第二个并发任务命中内存登记后仅做一次 daemon 验活探针。

    B5 收尾后复用快路径每轮验活（存在+running+探针通过才复用）：任务 1 建
    环境 1 次 docker 查找（未命中→创建），任务 2 复用时 1 次验活探活，共
    2 次、create 仅 1 次——若复用快路径失效（回归），任务 2 会重走查找+创建。
    """
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
    results = await asyncio.gather(
        manager.get_or_create_environment(
            task_id="t1", task_type=TaskType.ATOMIC, workspace=ws,
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash",
        ),
        manager.get_or_create_environment(
            task_id="t2", task_type=TaskType.ATOMIC, workspace=ws,
            operation_type=OperationType.CODE_EXECUTION, tool_name="bash",
        ),
    )

    # 串行化后：任务 1 建 env 查 1 次，任务 2 复用验活探 1 次（2 次查找、
    # 1 次创建）；两次调用拿到同一 env_id（复用而非重建）。
    assert find_count == 2, f"应共 2 次 daemon 查找（建 env 1 + 验活 1），实际 {find_count} 次"
    assert results[0].env_id == results[1].env_id
