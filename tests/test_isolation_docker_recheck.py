"""IsolationGuard Docker 可用性复检回归测试。

背景 BUG：IsolationGuard._docker_available 仅在插件构造时（__init__）检测一次，
之后整个进程生命周期不再刷新。当编排进程启动那一刻 Docker daemon 正好假死
（docker version 超时），_detect_docker 返回 False 被永久钉死，导致此后所有
要求容器隔离的工具（如 bash_execute）都被拦截为 docker_unavailable_container_required，
即便 daemon 已恢复、容器都在跑也无效——必须重启整个编排进程才能解除。

修复（见 src/plugins/input/isolation_guard/plugin.py）：
- 区分可用性来源：自动检测（_docker_auto=True，可复检）vs 配置显式指定（信任不刷新）。
- execute() 入口在决策前按冷却复检：仅当自动检测来源且当前为 False 时，
  在线程池重新探测，daemon 自愈后无需重启进程即可解除拦截。

本测试锁定核心契约：
1. 自动检测 False + daemon 恢复 → 复检后解除拦截，路由到 docker。
2. 冷却期内不重复探测（避免每次工具调用都 spawn subprocess）。
3. 配置显式指定的 False 永不刷新（信任配置，保护既有行为）。
4. True 状态不触发复检。
"""
import time
from unittest.mock import MagicMock, patch

from isolation.types import IsolationLevel
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
from plugins.input.isolation_guard.plugin import IsolationGuard


def _make_ctx(tool="bash_execute"):
    """创建 tool_execute 类型的 PluginContext。

    默认标 L2（子任务）：本文件用例测的是"docker 可用进容器 / 不可用 blocked"
    这类子任务场景；主 agent（L1）的 bash_execute 一律走 host，不会进容器。
    """
    return PluginContext(
        state={
            StateKeys.CORE_TYPE: "tool_execute",
            StateKeys.AGENT_LEVEL: "L2",
            StateKeys.RAW_TOOL_CALLS: [{"name": tool, "args": {}}],
        },
        config={},
        _services={},
    )


def _container_policy(plugin):
    """让 decider 对所有工具返回 container 隔离策略。"""
    mock_policy = MagicMock()
    mock_policy.isolation = IsolationLevel.CONTAINER
    plugin._decider.resolve = MagicMock(return_value=mock_policy)


def _make_auto_plugin(detected=False):
    """构造走自动检测路径的 IsolationGuard（不传 docker_available 配置）。

    Args:
        detected: __init__ 阶段 _detect_docker 的返回值（模拟启动时的检测结果）
    """
    with patch("isolation.decider.IsolationDecider"), \
         patch.object(IsolationGuard, "_detect_docker", return_value=detected):
        return IsolationGuard(config={})


# ---------------------------------------------------------------------------
# 1. 自动检测 False → daemon 恢复后复检解除拦截
# ---------------------------------------------------------------------------


async def test_auto_detected_false_recovers_after_daemon_up():
    """启动时检测到 False，daemon 恢复后复检写回 True 并解除拦截。"""
    plugin = _make_auto_plugin(detected=False)
    assert plugin._docker_auto is True
    assert plugin._docker_available is False
    _container_policy(plugin)

    # 模拟 daemon 恢复：复检返回 True；并越过冷却窗口
    plugin._detect_docker = MagicMock(return_value=True)
    plugin._docker_checked_at = time.monotonic() - 9999

    result = await plugin.execute(_make_ctx())

    contexts = result.state_updates["execution_contexts"]
    assert len(contexts) == 1
    assert contexts[0].get("provider") == "docker"
    assert not contexts[0].get("blocked")
    assert plugin._docker_available is True  # 复检结果已写回


# ---------------------------------------------------------------------------
# 2. 冷却期内不重复探测
# ---------------------------------------------------------------------------


async def test_no_recheck_within_cooldown():
    """刚检测过（冷却期内）不应再次探测，避免每次工具调用都 spawn subprocess。"""
    plugin = _make_auto_plugin(detected=False)
    _container_policy(plugin)

    probe = MagicMock(return_value=True)
    plugin._detect_docker = probe
    plugin._docker_checked_at = time.monotonic()  # 冷却期内

    await plugin.execute(_make_ctx())

    probe.assert_not_called()
    assert plugin._docker_available is False  # 未刷新，仍拦截


# ---------------------------------------------------------------------------
# 3. 配置显式指定的 False 永不刷新（信任配置，保护既有行为）
# ---------------------------------------------------------------------------


async def test_config_specified_false_never_rechecks():
    """config 显式指定 docker_available=False 时永不复检，始终拦截。"""
    with patch("isolation.decider.IsolationDecider"):
        plugin = IsolationGuard(config={"docker_available": False})
    assert plugin._docker_auto is False
    _container_policy(plugin)

    probe = MagicMock(return_value=True)
    plugin._detect_docker = probe
    plugin._docker_checked_at = time.monotonic() - 9999  # 即便越过冷却

    result = await plugin.execute(_make_ctx())

    probe.assert_not_called()  # 配置驱动，不刷新
    contexts = result.state_updates["execution_contexts"]
    assert contexts[0].get("blocked") is True  # 仍拦截


# ---------------------------------------------------------------------------
# 4. True 状态不触发复检
# ---------------------------------------------------------------------------


async def test_true_state_does_not_recheck():
    """已检测为 True 时不应复检（避免给 daemon 增加无谓探测负载）。"""
    plugin = _make_auto_plugin(detected=True)
    assert plugin._docker_available is True
    _container_policy(plugin)

    probe = MagicMock(return_value=False)
    plugin._detect_docker = probe
    plugin._docker_checked_at = time.monotonic() - 9999

    result = await plugin.execute(_make_ctx())

    probe.assert_not_called()
    contexts = result.state_updates["execution_contexts"]
    assert contexts[0].get("provider") == "docker"
