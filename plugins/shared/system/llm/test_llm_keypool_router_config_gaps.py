# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""llm 簇缺口补测批二：key_pool / router_factory / _config_models 未覆盖分支。

覆盖面（按行为分组）：
- ``PrioritySemaphore._wake_next``（131、136-137）：队列含「死 future」（被 cancel
  的 waiter）时跳过继续唤醒活 waiter（不吞许可）；队列全为死 waiter 时许可回填
  池 ``_value``；``_value`` 已达 ``_capacity`` 时不回填（容量不变量）；
- ``KeyPool.select``（427）：全部 slot ``is_exhausted`` 且无冷却、rpm 有余时
  返回首个（并发满场景——acquire_slot 随后排队等信号量，而非判死）；
- ``router_factory._ensure_provider_type_map_loaded``（98）：映射非空（已加载）
  时直接返回，不重建（懒加载幂等，不覆盖既有映射）；
- ``_config_models._resolve_project_root``（50）：祖先链无 ``config/kernel``
  目录 → 返回 None（纯环境变量展开行为，不比原来差）；
- ``_config_models._env_file_vars``（83）：stat 成功后文件被删（读取期竞态）
  → FileNotFoundError 静默返回空表（首启/清理竞态正常语义）。

不可达说明（逐条）：
- 无——全部目标行均可达（见上）；无留白防御分支。

外部依赖：文件系统（monkeypatch Path.stat 注入竞态）/ asyncio 事件循环；
不触网、不 sleep 真等（asyncio.sleep(0) 仅让出调度）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
_s = str(_PLUGIN_DIR)
while _s in sys.path:
    sys.path.remove(_s)
sys.path.insert(0, _s)


def _load(filename: str, mod_name: str) -> Any:
    """按唯一模块名加载平铺模块（防裸名跨插件串扰，同目录既有惯例）。"""
    spec = importlib.util.spec_from_file_location(mod_name, _PLUGIN_DIR / filename)
    assert spec is not None and spec.loader is not None, f"cannot load {filename}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def kp() -> Any:
    module = _load("key_pool.py", "llm_gaps_key_pool_under_test")
    module.set_agent_priority(None)
    return module


@pytest.fixture
def rf() -> Any:
    module = _load("router_factory.py", "llm_gaps_router_factory_under_test")
    module.reset_router()
    yield module
    module.reset_router()


# ─────────────────── PrioritySemaphore._wake_next：死 waiter 跳过与回填 ───────────────────


async def test_wake_next_skips_dead_waiter_and_wakes_live_one(kp: Any) -> None:
    """死 future 在队首 → 丢弃后继续唤醒下一个活 waiter（许可不丢）。

    构造：容量 1 被占；waiter A 入队后 cancel（future 变 cancelled，仍在
    队列）；waiter B 入队。release() 时队首是 A 的死 future，_wake_next 必须
    跳过它并唤醒 B。
    """
    sem = kp.PrioritySemaphore(1)
    await sem.acquire()

    dead = asyncio.create_task(sem.acquire())
    await asyncio.sleep(0)
    dead.cancel()
    with pytest.raises(asyncio.CancelledError):
        await dead

    # 人为把死 future 放回队首（模拟「cancel 时未走移除路径」的残留态）
    loop = asyncio.get_running_loop()
    poisoned: asyncio.Future = loop.create_future()
    poisoned.cancel()
    sem._waiters.insert(0, (99, poisoned))

    got = asyncio.Event()

    async def live() -> None:
        await sem.acquire()
        got.set()

    task = asyncio.create_task(live())
    await asyncio.sleep(0)

    sem.release()

    await asyncio.wait_for(got.wait(), timeout=1)
    await task
    assert not sem._waiters  # 死 waiter 已清、活 waiter 已出队


async def test_wake_next_refills_value_when_no_live_waiter(kp: Any) -> None:
    """队列全为死 future（无活 waiter）→ 许可回填 _value（容量内）。"""
    sem = kp.PrioritySemaphore(2)
    loop = asyncio.get_running_loop()
    dead: asyncio.Future = loop.create_future()
    dead.cancel()
    sem._waiters.append((99, dead))
    sem._value = 0

    sem.release()

    assert sem._value == 1
    assert sem._waiters == []  # 死 waiter 被丢弃


def test_wake_next_does_not_refill_beyond_capacity(kp: Any) -> None:
    """``_value`` 已达容量 → 不回填（容量不变量，多 release 不放大许可）。"""
    sem = kp.PrioritySemaphore(1)
    loop = asyncio.new_event_loop()
    try:
        dead: asyncio.Future = loop.create_future()
        dead.cancel()
        sem._waiters.append((99, dead))
        sem._value = 1  # 已达容量

        sem._wake_next()

        assert sem._value == 1
        assert sem.capacity == 1
    finally:
        loop.close()


# ─────────────────── KeyPool.select：并发满放行 ───────────────────


def test_pool_select_returns_uncoooled_slot_when_rpm_still_available(kp: Any) -> None:
    """全部 slot exhausted 但未冷却且 rpm 有余（配额耗尽形态）→ 返回首个。

    契约（select 425-427 注释）：is_exhausted 成立的成因不止 rpm——token 配额
    耗尽而 rpm 未满是「并发/配额满」而非「限流满」，此时必须返回未冷却 slot，
    交 acquire_slot 在 record_request 后等信号量释放；返回 None 会让并发请求
    被误判为池耗尽。
    """
    slot = kp.KeySlot(
        key_id="quota-out", api_key="sk-x", rpm_limit=10, token_quota=100
    )
    slot.record_usage(60, 50)  # 110 > 100 → 配额耗尽
    assert slot.is_exhausted is True
    assert slot.is_cooling is False
    assert slot.rpm_remaining > 0

    pool = kp.KeyPool([slot], pool_id="p")

    assert pool.select() is slot


def test_pool_select_returns_none_when_all_cooling_or_rpm_starved(kp: Any) -> None:
    """区分度输入（负例）：冷却中 或 rpm 耗尽 → 不返回 slot（防 429 风暴）。

    与上一条共用同一 select 调用点，两输入仅「可用性」一面不同，
    锁定 427 行仅对「未冷却且 rpm 有余」生效。
    """
    cooling = kp.KeySlot(key_id="c", api_key="sk-c", rpm_limit=10)
    cooling._cooling_until = float("inf")
    starved = kp.KeySlot(key_id="s", api_key="sk-s", rpm_limit=1)
    starved.record_request()

    pool = kp.KeyPool([cooling, starved], pool_id="p")

    assert pool.get_unavailable_slots()  # 诊断面非空（全不可用）
    assert pool.select() is None


# ─────────────────── router_factory：懒加载幂等 ───────────────────


def test_ensure_provider_type_map_loaded_is_noop_when_already_loaded(
    rf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """映射非空 → 直接返回，不调用配置 loader（已加载态不被重建覆盖）。"""
    rf._provider_type_map["custom-provider"] = "openai"
    calls: list[str] = []

    def _boom() -> Any:
        calls.append("loader")
        raise AssertionError("映射非空时不应重建")

    monkeypatch.setattr("_config_models.get_model_config_loader", _boom, raising=False)

    rf._ensure_provider_type_map_loaded()

    assert calls == []
    assert rf._provider_type_map["custom-provider"] == "openai"


def test_ensure_provider_type_map_loaded_returns_after_populating_from_loader(
    rf: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """区分度输入（正例）：映射为空 → 从 loader 的 llm.yaml providers 重建。

    与上一条仅「映射是否为空」一面不同，共同锁定 97-98 行的早退语义。
    """
    rf._provider_type_map.clear()

    class _Loader:
        def _load_llm_data(self) -> dict[str, Any]:
            return {
                "providers": {
                    "apigo": {"type": "openai"},
                    "zai": {"type": "zai"},
                    "broken": "not-a-dict",       # 非 dict 噪声跳过
                    "no-type": {"api_base": "x"},  # 缺 type 跳过
                }
            }

    import _config_models as ccm

    monkeypatch.setattr(ccm, "get_model_config_loader", lambda: _Loader())

    rf._ensure_provider_type_map_loaded()

    assert rf._provider_type_map == {"apigo": "openai", "zai": "zai"}


# ─────────────────── router_factory：build_model_list 单 key api_base 回退 ───────────────────


class _Loader:
    """配置 loader 替身：只暴露 ``_load_llm_data``（router_factory 唯一消费面）。"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def _load_llm_data(self) -> dict[str, Any]:
        return self._data


def _slots_for(rf: Any, **kw: Any) -> Any:
    kw.setdefault("key_id", "k1")
    kw.setdefault("api_key", "sk-test")
    return rf.KeySlot(**kw)


def test_build_model_list_single_key_uses_slot_api_base_when_model_level_absent(
    rf: Any,
) -> None:
    """模型级 api_base 缺省 + slot 带 api_base → 回退用 slot 的（240 行）。

    契约（build_model_list 单 key 分支）：模型级 api_key 存在时走单 key 路径
    （不按 slot 展开 deployment）；api_base 优先级 = 模型级 > slot 级。
    """
    rf._provider_type_map["apigo"] = "openai"
    loader = _Loader(
        {
            "models": {
                "m1": {
                    "provider": "apigo",
                    "model_name": "MiniMax-M3",
                    "api_key": "sk-model-level",
                }
            },
            "providers": {"apigo": {"type": "openai"}},
        }
    )
    slots = {"apigo": [_slots_for(rf, api_base="https://slot.example.com")]}

    out = rf.build_model_list(loader, slots)

    assert len(out) == 1
    assert out[0]["litellm_params"]["api_base"] == "https://slot.example.com"
    assert out[0]["litellm_params"]["model"] == "openai/MiniMax-M3"
    assert out[0]["litellm_params"]["api_key"] == "sk-model-level"


def test_build_model_list_single_key_model_level_api_base_wins_over_slot(
    rf: Any,
) -> None:
    """区分度输入：模型级 api_base 存在 → 压过 slot 级（优先级不被反转）。"""
    rf._provider_type_map["apigo"] = "openai"
    loader = _Loader(
        {
            "models": {
                "m1": {
                    "provider": "apigo",
                    "model_name": "MiniMax-M3",
                    "api_key": "sk-model-level",
                    "api_base": "https://model.example.com",
                }
            },
            "providers": {"apigo": {"type": "openai"}},
        }
    )
    slots = {"apigo": [_slots_for(rf, api_base="https://slot.example.com")]}

    out = rf.build_model_list(loader, slots)

    assert out[0]["litellm_params"]["api_base"] == "https://model.example.com"


def test_build_model_list_multi_key_omits_api_base_when_both_absent(rf: Any) -> None:
    """区分度输入（负例）：无模型级 api_key（多 key 展开）+ slot 无 api_base
    → 键被删除（不落空串 api_base）。"""
    rf._provider_type_map["apigo"] = "openai"
    loader = _Loader(
        {
            "models": {"m1": {"provider": "apigo", "model_name": "MiniMax-M3"}},
            "providers": {"apigo": {"type": "openai"}},
        }
    )
    slots = {"apigo": [_slots_for(rf)]}  # api_base 缺省为 ""

    out = rf.build_model_list(loader, slots)

    assert "api_base" not in out[0]["litellm_params"]


# ─────────────────── _config_models：项目根与 .env 读取竞态 ───────────────────


def test_resolve_project_root_returns_none_without_config_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """祖先链无 config/kernel 目录 → 返回 None（纯环境变量展开行为）。

    以假 __file__ 锚定到无 config/kernel 的目录树（不依赖真实磁盘布局）。
    """
    ccm = _load("_config_models.py", "llm_gaps_config_models_under_test")
    fake_dir = Path(Path.cwd().anchor) / "llm-gaps-no-project-root" / "a" / "b"
    monkeypatch.setattr(ccm, "__file__", str(fake_dir / "_config_models.py"))

    assert ccm._resolve_project_root() is None


def test_resolve_project_root_finds_ancestor_with_config_kernel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """区分度输入（正例）：祖先含 config/kernel → 返回该祖先（探测真实生效）。"""
    ccm = _load("_config_models.py", "llm_gaps_config_models_root_hit")
    (tmp_path / "config" / "kernel").mkdir(parents=True)
    deep = tmp_path / "plugins" / "shared" / "system" / "llm"
    deep.mkdir(parents=True)
    monkeypatch.setattr(ccm, "__file__", str(deep / "_config_models.py"))

    assert ccm._resolve_project_root() == tmp_path


def test_env_file_vars_returns_empty_on_read_race_file_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """stat 成功后文件被删（读取期 FileNotFoundError）→ 静默空表、不告警。

    正常清理竞态语义：stat 通过（mtime 可读）但 read_text 时文件已不存在，
    等同「无 .env」而非错误，故静默；结果不入缓存（下次重读）。
    """
    import logging

    ccm = _load("_config_models.py", "llm_gaps_config_models_race")
    env_path = tmp_path / ".env"
    env_path.write_text("KEY=v\n", encoding="utf-8")
    monkeypatch.setattr(ccm, "_resolve_project_root", lambda: tmp_path)
    ccm._env_cache = None

    real_read_text = Path.read_text
    real_stat = Path.stat

    def _stat(self: Path, *a: Any, **kw: Any) -> Any:
        return real_stat(self, *a, **kw)

    def _read_text(self: Path, *a: Any, **kw: Any) -> str:
        if self == env_path:
            # 让 stat 已成功路径保留，但读取时文件已消失
            real_read_text(self, *a, **kw)
            raise FileNotFoundError("removed after stat")
        return real_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "stat", _stat)
    monkeypatch.setattr(Path, "read_text", _read_text)

    with caplog.at_level(logging.WARNING, logger=ccm.__name__):
        assert ccm._env_file_vars() == {}

    assert not [r for r in caplog.records if ".env 读取失败" in r.getMessage()]
    assert ccm._env_cache is None


def test_env_file_vars_parses_and_caches_on_normal_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """区分度输入（正例）：正常 .env 解析出键值并写缓存（与竞态路径互斥）。"""
    ccm = _load("_config_models.py", "llm_gaps_config_models_ok")
    env_path = tmp_path / ".env"
    env_path.write_text(
        '# comment\n\nA=1\nB="quoted"\nNOEQUALS\n', encoding="utf-8"
    )
    monkeypatch.setattr(ccm, "_resolve_project_root", lambda: tmp_path)
    ccm._env_cache = None

    assert ccm._env_file_vars() == {"A": "1", "B": "quoted"}
    assert ccm._env_cache is not None  # 成功结果入缓存
    assert ccm._env_file_vars() == {"A": "1", "B": "quoted"}  # 二次命中缓存
