# @feature: FP-GATE LLM 流式退化循环守卫——via=plugin provider 共享翻译器契约 | @ci: python-coverage
"""via=plugin provider 必须经共享翻译器产出流事件的契约门禁。

设计真值：ADR 2026-11-07-llm-stream-repetition-guard。退化循环守卫的落点是
共享根 ``_llm_streaming.StreamTranslator``（网关与 provider 插件同源消费），
不是网关的 chunk 回调——``providers.<name>.via=plugin`` 的模型由 provider 插件
**跨进程**跑自己的传输与流循环，网关根本看不到 chunk：

- 网关路径（配置面直连）：``_StreamOutbound.on_chunk`` 里跑同一个翻译器，
  判定后返回 ``"stop"`` 交给 adapter 截断；
- provider 插件路径：插件必须用同一个翻译器出 DSH 事件，判定后读
  ``translator.stopped`` 自行收流。

绕开翻译器自造事件的插件会让**退化守卫与 DSH 8 事件协议同时失效**（且不会有
任何运行时报错——正是 2026-11-07 事故的失效形态）。故按 llm.yaml 反查：
声明的 provider 类型必须有对应插件目录，且源码引用共享翻译器。新增 provider
漏了就是 CI 红，而不是运行时静默失守。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parent.parent
_LLM_YAML = _REPO / "config" / "plugins" / "llm" / "llm.yaml"
_PLUGINS_SHARED = _REPO / "plugins" / "shared"

#: provider 侧必须引用的共享翻译器符号（任一命中即视为经共享面出事件）
_TRANSLATOR_MARKERS = ("StreamTranslator", "_llm_streaming")


def _plugin_providers(config: dict[str, Any]) -> list[tuple[str, str]]:
    """从 llm.yaml 提取 ``via=plugin`` 的 provider 声明 [(provider 名, provider_type)]。

    与 router_factory 同口径：``provider_type`` 缺省回落 provider 名，
    命名空间 = ``llm_provider.<provider_type>``。
    """
    out: list[tuple[str, str]] = []
    providers = config.get("providers") or {}
    if not isinstance(providers, dict):
        return out
    for name, conf in providers.items():
        if isinstance(conf, dict) and conf.get("via") == "plugin":
            out.append((str(name), str(conf.get("provider_type") or name)))
    return out


def _contract_violations(config: dict[str, Any], plugins_dir: Path) -> list[str]:
    """返回违规清单（空 = 全绿）。

    违规判据：声明了 via=plugin 但 (a) 对应插件目录不存在，或
    (b) 插件源码里找不到共享翻译器引用。
    """
    violations: list[str] = []
    for provider_name, provider_type in _plugin_providers(config):
        plugin_dir = plugins_dir / f"llm_provider_{provider_type}"
        if not plugin_dir.is_dir():
            violations.append(
                f"providers.{provider_name}: via=plugin 但插件目录缺失 "
                f"{plugin_dir.name}/（命名空间 llm_provider.{provider_type}）"
            )
            continue
        sources = list(plugin_dir.rglob("*.py"))
        if not sources:
            violations.append(
                f"providers.{provider_name}: {plugin_dir.name}/ 无 Python 实现"
            )
            continue
        if not any(
            marker in p.read_text(encoding="utf-8", errors="replace")
            for p in sources
            for marker in _TRANSLATOR_MARKERS
        ):
            violations.append(
                f"providers.{provider_name}: {plugin_dir.name}/ 未引用共享翻译器"
                f"（{' / '.join(_TRANSLATOR_MARKERS)}）——绕开它则退化循环守卫"
                f"与 DSH 8 事件协议对该 provider 失效"
            )
    return violations


def test_real_config_providers_are_conformant() -> None:
    """真实配置面：所有 via=plugin provider 都经共享翻译器出事件。"""
    assert _LLM_YAML.is_file(), f"llm 配置缺失: {_LLM_YAML}"
    config = yaml.safe_load(_LLM_YAML.read_text(encoding="utf-8")) or {}

    assert _contract_violations(config, _PLUGINS_SHARED) == []


def test_gate_flags_missing_plugin_dir(tmp_path: Path) -> None:
    """负控制：声明了 via=plugin 但插件目录不存在 → 逐条可辨。"""
    config = {"providers": {"dsh": {"via": "plugin", "type": "openai"}}}

    violations = _contract_violations(config, tmp_path)

    assert len(violations) == 1
    assert "dsh" in violations[0]
    assert "插件目录缺失" in violations[0]


def test_gate_flags_provider_without_translator(tmp_path: Path) -> None:
    """负控制：插件在但自造事件（不引共享翻译器）→ 红。"""
    plugin_dir = tmp_path / "llm_provider_dsh"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(
        "def complete_stream(params):\n    return {'text': 'ok'}\n", encoding="utf-8"
    )
    config = {"providers": {"dsh": {"via": "plugin"}}}

    violations = _contract_violations(config, tmp_path)

    assert len(violations) == 1
    assert "未引用共享翻译器" in violations[0]


def test_gate_accepts_translator_based_provider(tmp_path: Path) -> None:
    """正控制：引用共享翻译器的 provider 插件 → 绿（不误伤合规实现）。"""
    plugin_dir = tmp_path / "llm_provider_dsh"
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(
        "from _llm_streaming import StreamTranslator\n", encoding="utf-8"
    )
    config = {"providers": {"dsh": {"via": "plugin"}}}

    assert _contract_violations(config, tmp_path) == []


def test_gate_ignores_config_face_providers(tmp_path: Path) -> None:
    """未声明 via=plugin 的配置面 provider 不进门禁（37 家零迁移面）。"""
    config = {"providers": {"cpa_gateway": {"type": "openai", "api_base": "http://x"}}}

    assert _contract_violations(config, tmp_path) == []


if __name__ == "__main__":  # pragma: no cover - 手工排查入口
    _cfg = yaml.safe_load(_LLM_YAML.read_text(encoding="utf-8")) or {}
    _bad = _contract_violations(_cfg, _PLUGINS_SHARED)
    print(f"via=plugin providers: {_plugin_providers(_cfg)}")
    print("violations:", _bad or "none")
    pytest.main([__file__, "-q"])
