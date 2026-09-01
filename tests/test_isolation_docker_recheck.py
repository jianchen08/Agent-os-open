# @feature: FP-0.2.二 内部模块manifest | @vision: V3 可嵌入
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
# isolation_guard 插件位于 plugins/shared/pipeline/input/isolation_guard/（0.2 平铺 import）
import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests._isolation_path  # noqa: F401  # 注入 isolation 插件目录到 sys.path

# 平铺共享裸名自防御：先导的测试（security_check 等 add_plugin_dir 系）会把自家
# 目录钉在 sys.path[0] 并缓存 plugin 模块；conftest 收集期逐出按残序重解析仍可能
# 劫持（实测 security_check/plugin.py 抢走 `plugin` 名）。与
# _pipeline_plugin_path.add_plugin_dir 同款：本目录置顶 + 裸名逐出后再 import。
_ISOLATION_GUARD_DIR = str(
    Path(__file__).resolve().parent.parent
    / "plugins" / "shared" / "pipeline" / "input" / "isolation_guard",
)
if _ISOLATION_GUARD_DIR in sys.path:
    sys.path.remove(_ISOLATION_GUARD_DIR)
sys.path.insert(0, _ISOLATION_GUARD_DIR)
for _bare in ("plugin", "tool", "models", "service"):
    sys.modules.pop(_bare, None)

from isolation_types import IsolationLevel
from pipeline.plugin import PluginContext
from pipeline.types import StateKeys
import plugin as plugin_module  # noqa: E402  # 模块对象引用：裸名串扰治理会 evict 后重 import，字符串 patch 会打到新对象
from plugin import IsolationGuard  # noqa: E402


@pytest.fixture(autouse=True)
def _fake_wsl_health(monkeypatch: pytest.MonkeyPatch) -> types.SimpleNamespace:
    """引擎自愈在插件内延迟 import wsl_health（路径注入后），测试环境用 fake 占位。

    复检路径触发 _ensure_engine 时命中 fake，不真实拉起 WSL/docker
    （wsl_health 自身行为由 plugins/shared/system/isolation/test_wsl_health.py 覆盖）。
    """
    fake = types.SimpleNamespace()
    fake.ensure_docker_engine = MagicMock()
    monkeypatch.setitem(sys.modules, "wsl_health", fake)
    return fake


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
    # 0.2 容器落地：provider=docker 后还需幂等获取/创建容器，失败会改标 denied。
    # 本文件测复检契约，容器创建一律 stub 成功（容器语义由 test_container_landing 覆盖）。
    plugin._get_or_create_container = AsyncMock(return_value="mock-container-1")


def _make_auto_plugin(detected=False):
    """构造走自动检测路径的 IsolationGuard（不传 docker_available 配置）。

    Args:
        detected: __init__ 阶段 _detect_docker 的返回值（模拟启动时的检测结果）

    自动检测路径的复检会触发引擎自愈（ensure_docker_engine），测试环境不
    真实拉起 WSL/docker，统一 mock 掉。
    """
    with patch("decider.IsolationDecider"), \
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
    with patch("decider.IsolationDecider"):
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


# ---------------------------------------------------------------------------
# 5. 引擎自愈：复检路径先确保引擎存活再探测（插件运行时维护引擎）
# ---------------------------------------------------------------------------


async def test_recheck_triggers_engine_self_heal(_fake_wsl_health: types.SimpleNamespace):
    """复检路径先触发引擎自愈（wsl_health.ensure_docker_engine）再探测。"""
    plugin = _make_auto_plugin(detected=False)
    _container_policy(plugin)
    plugin._detect_docker = MagicMock(return_value=False)  # 复检阶段钉死不可用
    plugin._docker_checked_at = time.monotonic() - 9999

    await plugin.execute(_make_ctx())

    _fake_wsl_health.ensure_docker_engine.assert_called_once()


async def test_engine_self_heal_error_does_not_break_recheck(_fake_wsl_health: types.SimpleNamespace):
    """自愈抛异常不阻断复检与决策（降级保持，只留日志）。"""
    plugin = _make_auto_plugin(detected=False)
    _container_policy(plugin)
    plugin._detect_docker = MagicMock(return_value=False)  # 复检仍不可用
    plugin._docker_checked_at = time.monotonic() - 9999
    _fake_wsl_health.ensure_docker_engine.side_effect = RuntimeError("boom")

    result = await plugin.execute(_make_ctx())

    contexts = result.state_updates["execution_contexts"]
    assert contexts[0].get("blocked") is True  # 仍拦截（docker 不可用），未崩
