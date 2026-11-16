# @feature: FP-0.2.〇 RP引擎(作者权威+/cheat) | @vision: V1 可进化 | @ci: python-coverage
"""agent_config_load 装载卡 engine 声明测试（RP 批 3，作者权威声明面）。

契约（照抄 context.regex_scripts / rp_state_* 先例）：agent yaml（模式卡）
extensions.engine → context.rp_engine，卡产物 verbatim 透传（装载零解释——
llm/rule_complexity/contract_level 一期不消费、收下透传；结构合法性由
card 质检服务 lint_engine 面（真实 card_lint 后端替身）+ 消费面 fail-soft 把关）；
无声明 = 不写键（兼容态零行为变化——出厂卡无 engine 声明即此形态）。

env 隔离：AGENTOS_USER_ROOT / AGENTOS_CONFIG_ROOT 钉到 tmp（与 2a 测试同契约）。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED = _DIR.parents[0]  # plugins/shared/

for _d in (_DIR, _SHARED):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

from _libs.pipeline_base.plugin import PluginContext  # noqa: E402
from _libs.testing.card_service_fake import install_card_service_fake  # noqa: E402


def _load_plugin_module() -> Any:
    mod_name = "agent_config_load_plugin_rp_engine_test"
    module_path = _DIR / "plugin.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(module_path))
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def factory_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """factory 配置根钉到 tmp（用户根留空 = 直接回落 factory）。"""
    factory = tmp_path / "factory"
    factory.mkdir()
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "user-root"))
    monkeypatch.delenv("AGENTOS_USER_CONFIG_DIR", raising=False)
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(factory))
    return factory


def _write_card(factory_root: Path, extensions: dict[str, Any] | None) -> str:
    """写一张最小模式卡（roleplay/luna），返回 agent.id 键。"""
    card_dir = factory_root / "agents" / "roleplay"
    card_dir.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {"display_name": "月影", "system_prompt": "扮演。"}
    if extensions is not None:
        data["extensions"] = extensions
    import yaml

    (card_dir / "luna.yaml").write_text(
        yaml.safe_dump(data, allow_unicode=True), encoding="utf-8"
    )
    return "roleplay/luna"


def _run(extensions: dict[str, Any] | None, factory_root: Path) -> dict[str, Any]:
    mod = _load_plugin_module()
    agent_id = _write_card(factory_root, extensions)
    plugin = mod.AgentConfigLoadPlugin(config={})
    install_card_service_fake(plugin)  # lint 面走真实 card_lint 后端替身（T6c）
    ctx = PluginContext(state={"agent.id": agent_id}, config={})
    return asyncio.run(plugin.execute(ctx)).state_updates


def test_engine_declared_loaded_verbatim(factory_root: Path) -> None:
    """engine 声明 verbatim 透传：五个字段（含一期不消费的三个）原样进通道。"""
    engine = {
        "llm": "with_llm",
        "rule_complexity": "core",
        "allow_cheat": True,
        "contract_level": 1,
        "cheat_overrides": {"keep_state_machine": ["npc.*.好感度"]},
    }

    updates = _run({"engine": engine}, factory_root)

    assert updates["context.rp_engine"] == engine, "卡产物零解释透传（声明面先行）"


def test_engine_absent_writes_no_key(factory_root: Path) -> None:
    """无 engine 声明 = 不写键（兼容态零行为变化）。"""
    updates = _run(None, factory_root)

    assert "context.rp_engine" not in updates


@pytest.mark.parametrize(
    "engine",
    [
        {},  # 空映射 = 未声明语义（不写键）
        "不是映射",  # 形态非法 = warning 直通不写键
        ["也不是映射"],
    ],
)
def test_engine_non_dict_or_empty_writes_no_key(
    factory_root: Path, engine: Any
) -> None:
    updates = _run({"engine": engine}, factory_root)

    assert "context.rp_engine" not in updates, f"形态 {engine!r} 不入通道（fail-soft）"


def test_engine_with_partial_fields_loaded_verbatim(factory_root: Path) -> None:
    """作者只声明 allow_cheat 单字段 = 合法形态（字段独立可选）。"""
    updates = _run({"engine": {"allow_cheat": False}}, factory_root)

    assert updates["context.rp_engine"] == {"allow_cheat": False}


def test_engine_with_lint_issues_still_loaded_verbatim(
    factory_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """声明带结构问题（lint 警告面）：装载仍 verbatim（值域执法归消费面）。"""
    import logging

    updates = _run({"engine": {"allow_cheat": "yes", "未知键": 1}}, factory_root)

    assert updates["context.rp_engine"] == {"allow_cheat": "yes", "未知键": 1}
    warnings_text = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("allow_cheat" in w for w in warnings_text), "lint 警告面可见（作者笔形）"
    assert any("未知声明键" in w for w in warnings_text)
