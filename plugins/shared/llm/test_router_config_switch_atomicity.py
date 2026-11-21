# @feature: FP-0.2.CFG 插件配置热感知 | @ci: python-test
"""配置切换期共享路由表的原子性契约（异步阻塞与共享状态审计 2026-11-12 P2-12）。

行为契约（断输入→输出/副作用，不钉实现细节）：

1. **读侧无空窗**：配置切换（reset 之后、下一代构建之前）期间，前缀映射/模型
   映射仍解析上一代完整表——不出现「表被清空 → 懒加载读到新配置 → 前缀映射
   缺失」的 fail-closed 失败（审计症状：另一管道在同一切换窗口里失败一次）。
2. **新配置整体生效**：重建完成后新表替换旧表，配置真正变更不丢。
3. **无实质变化不重置**：同一生效配置下，仅调用瞬态键（``_pipe_check`` 触发
   上下文，逐调用不同）不同的两次调用（不同租户身份）不触发 router/adapter
   重置——多管道/多租户并发不再每调用重建。

租户身份经 SDK 反向调用身份 contextvar 下发（调用边界 = 配置变更钩子之前，
见 ``agentos_plugin_sdk/server.py::_handle_tools_call``），测试按生产同序绑定。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# 配置 A / B：两个"租户"生效配置不同（provider 集与模型集都不相交）
_LLM_A: dict[str, Any] = {
    "providers": {"prov_a": {"type": "openai", "keys": [{"id": "a1", "api_key": "sk-a1"}]}},
    "models": {"m-main": {"provider": "prov_a", "model_name": "real-main"}},
}
_LLM_B: dict[str, Any] = {
    "providers": {"prov_b": {"type": "zhipu", "keys": [{"id": "b1", "api_key": "sk-b1"}]}},
    "models": {"m-b": {"provider": "prov_b", "model_name": "real-b"}},
}


class _Loader:
    """最小 llm.yaml 加载器替身（load_llm_data 返回注入配置）。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def load_llm_data(self) -> dict[str, Any]:
        return self._data


def _load_router_factory() -> Any:
    """按唯一模块名加载 router_factory（避免与其它插件同名模块互劫持）。"""
    for _m in ("key_pool",):
        sys.modules.pop(_m, None)
    mod_name = "router_factory_switch_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "router_factory.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _load_server() -> Any:
    """按显式路径加载 llm 插件 server 模块（唯一模块名隔离同名 server.py）。

    先把裸名共享根模块弹掉：server.py 与测试必须命中同一份 router_factory/
    _config_models 实例，断言才指向被测对象。
    """
    for _m in ("adapter", "exceptions", "key_pool", "router_factory", "_config_models"):
        sys.modules.pop(_m, None)
    mod_name = "llm_server_switch_test"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / "server.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _call_as_tenant(server: Any, tenant_id: str, config: dict[str, Any]) -> Any:
    """按生产次序发一次调用：绑租户身份 → 配置变更感知分发 → 复位身份。"""
    from agentos_plugin_sdk.capability import (
        bind_reverse_call_identity,
        reset_reverse_call_identity,
    )

    async def _run() -> None:
        token = bind_reverse_call_identity({"tenant_id": tenant_id})
        try:
            await server.plugin._apply_call_config(config, "llm.complete_stream")  # noqa: SLF001
        finally:
            reset_reverse_call_identity(token)

    return _run()


def _run(coro: Any) -> Any:
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _restore_flat_module_slots():
    """还原平铺裸名槽位（防跨文件污染）。

    ``_load_router_factory`` / ``_load_server`` 装载前清场逐出这五个裸名
    （server.py 与本文件测试须命中同一实例），但逐出换掉的实例可能正是
    其他测试模块**收集期绑定**的依赖——llm_core 车道实测：
    ``llm_core.plugin`` 的函数级 ``from _config_models import`` 在运行期
    解析到新实例，而对方的 monkeypatch 落在收集期旧实例上失效
    （症状 defaults.chat 为空）。退出还原装载前现场。
    """
    names = ("adapter", "exceptions", "key_pool", "router_factory", "_config_models")
    saved = {m: sys.modules.get(m) for m in names}
    yield
    for m, old in saved.items():
        if old is None:
            sys.modules.pop(m, None)
        else:
            sys.modules[m] = old


# ─────────────── 契约 1+2：切换窗口读上一代 / 重建后新表生效 ───────────────


def test_switch_window_keeps_previous_generation_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reset 之后、重建之前：映射表仍是上一代完整内容（旧实现被 clear → 空表）。

    旧实现读侧路径：空表 → 懒加载读「当前已下发的配置 B」→ 查无 prov_a →
    ValueError("前缀映射缺失")，即审计记录的「窗口内失败一次」。
    """
    rf = _load_router_factory()
    import _config_models  # 加载器之后取同一个 sys.modules 实例

    monkeypatch.setattr(_config_models, "_config", {})

    rf.build_router(_Loader(_LLM_A))
    assert rf.get_provider_prefix("prov_a") == "openai"
    assert rf.get_provider_for_model("m-main") == "prov_a"

    # 租户 B 的配置已下发到全局 shim（内核每调用现算），router 尚未重建
    _config_models.set_config({"llm": _LLM_B})
    rf.reset_router()

    # 窗口内另一管道读：必须仍解析上一代（旧实现此处 ValueError）
    assert rf.get_provider_prefix("prov_a") == "openai"
    assert rf.get_provider_for_model("m-main") == "prov_a"

    # 重建后新配置整体生效：A 的 provider 不再声明（配置真正变更不丢）
    rf.build_router(_Loader(_LLM_B))
    with pytest.raises(ValueError, match="前缀映射缺失"):
        rf.get_provider_prefix("prov_a")
    assert rf.get_provider_for_model("m-b") == "prov_b"
    assert rf.get_provider_for_model("m-main") == ""


def test_hook_reset_window_readable_across_tenants(monkeypatch: pytest.MonkeyPatch) -> None:
    """经 server 配置变更钩子的同一切换窗口：租户 B 的调用不打断在途的 A 读。

    交错构造：A 的上一代表可读 → B 的调用携新配置进入（钩子 reset + adapter
    置空）→ 在 B 重建 adapter 之前，A 的在途调用读前缀/模型映射。
    """
    server = _load_server()
    import _config_models  # server 装载后取同一个 sys.modules 实例
    import router_factory as rf

    monkeypatch.setattr(_config_models, "_config", {})

    rf.build_router(_Loader(_LLM_A))  # 上一代（租户 A 的生效配置）

    _run(_call_as_tenant(server, "tenant-b", {"llm": _LLM_B}))

    # B 的钩子已 reset（adapter 待懒重建），A 在途调用读窗口内表
    assert server._adapter is None  # noqa: SLF001 — 重建仍待发生（窗口成立）
    assert rf.get_provider_prefix("prov_a") == "openai"
    assert rf.get_provider_for_model("m-main") == "prov_a"

    # B 的重建落定后新表生效
    server._ensure_adapter()  # noqa: SLF001
    assert rf.get_provider_for_model("m-b") == "prov_b"
    assert rf.get_provider_for_model("m-main") == ""


# ─────────────── 契约 3：无实质变化不重置 ───────────────


def test_transient_trigger_payload_across_tenants_does_not_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """仅 ``_pipe_check``（触发上下文）不同的两次跨租户调用不重置 router/adapter。

    旧实现：配置视图含 ``_pipe_check``（SDK 瞬态键表未含该键）→ 每次触发调用都
    判「配置变更」→ reset + adapter 置空 → 下一调用重建（重复构建）。
    """
    server = _load_server()
    import _config_models  # server 装载后取同一个 sys.modules 实例

    monkeypatch.setattr(_config_models, "_config", {})

    applied: list[dict[str, Any]] = []
    real_set_config = server.set_config

    def _spy_set_config(cfg: dict[str, Any]) -> None:
        applied.append(cfg)
        real_set_config(cfg)

    monkeypatch.setattr(server, "set_config", _spy_set_config)

    view = {"llm": _LLM_A}
    _run(_call_as_tenant(server, "tenant-a", {**view, "_pipe_check": {"at": "pre:s1", "epoch": 1}}))
    assert len(applied) == 1  # 首调：生效

    _run(_call_as_tenant(server, "tenant-b", {**view, "_pipe_check": {"at": "post:s2", "epoch": 9}}))
    assert len(applied) == 1  # 旧实现此处 = 2（触发上下文被误判为配置变更）

    # 配置实质变化仍必须生效（reset 外部语义保留）
    changed = {"llm": {**_LLM_A, "providers": {"prov_a": {"type": "openai", "keys": [{"id": "a2", "api_key": "sk-a2"}]}}}}
    _run(_call_as_tenant(server, "tenant-a", changed))
    assert len(applied) == 2
