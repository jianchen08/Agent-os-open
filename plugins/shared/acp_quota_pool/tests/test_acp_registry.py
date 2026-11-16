# @feature: FP-0.2.ACP额度池 M0 | @ci: python-coverage
"""acp_registry 行为测试——注册表解析面（fail-closed）。

注册表是外部输入：缺失/损坏/条目残缺一律 ValueError 拒载（配置损坏 ≠
空注册表），合法输入按字段解析；文件装载走 providers.toml 真语料。
≥2 组有区分度输入（单家/多家、缺省/全字段）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from acp_registry import load_registry, load_registry_file

pytestmark = pytest.mark.unit


def _valid_single() -> dict[str, Any]:
    return {
        "providers": {
            "opencode": {
                "name": "OpenCode",
                "command": "opencode",
                "args": ["acp"],
                "quota_model": "Zen 免费模型档",
                "cost_class": "free",
                "strength": "unverified",
            }
        }
    }


def test_valid_single_provider_parses_fields() -> None:
    registry = load_registry(_valid_single())

    assert set(registry.keys()) == {"opencode"}
    spec = registry["opencode"]
    assert spec.id == "opencode"
    assert spec.command == "opencode"
    assert spec.args == ("acp",)
    assert spec.name == "OpenCode"
    assert spec.quota_model == "Zen 免费模型档"
    assert spec.cost_class == "free"
    assert spec.strength == "unverified"
    assert spec.timeout_seconds > 0  # 缺省超时取正常数，不落 0/负


def test_multiple_providers_keep_distinct_entries_and_field_defaults() -> None:
    config = {
        "providers": {
            "alpha": {"command": "alpha-cli"},
            "beta": {"command": "beta-cli", "args": ["acp", "--quiet"], "timeout_seconds": 7.5},
        }
    }

    registry = load_registry(config)

    assert registry["alpha"].args == ()
    assert registry["beta"].args == ("acp", "--quiet")
    assert registry["beta"].timeout_seconds == 7.5
    assert registry["alpha"].name == "alpha"  # name 缺省回落 provider id
    assert registry["alpha"].strength == ""  # 路由元数据可缺省（按 unverified 口径处理）


@pytest.mark.parametrize(
    "config",
    [
        None,
        {},
        {"providers": {}},
        {"other_section": {"command": "x"}},
        {"providers": {"opencode": {"args": ["acp"]}}},  # 缺 command
        {"providers": {"opencode": {"command": ""}}},
        {"providers": {"opencode": {"command": "opencode", "timeout_seconds": 0}}},
        {"providers": {"opencode": {"command": "opencode", "timeout_seconds": -3}}},
        {"providers": {"opencode": {"command": "opencode", "args": "not-a-list"}}},
        {"providers": {"opencode": {"command": "opencode", "backend": "rpc"}}},  # 未知后端
        # oneshot 缺 {goal} 占位符（目标全文无处注入）
        {"providers": {"zcode": {"command": "node", "args": ["-p"], "backend": "oneshot"}}},
    ],
)
def test_malformed_configs_fail_closed(config: Any) -> None:
    with pytest.raises(ValueError, match="必须|注册表|backend|占位符"):
        load_registry(config)


def test_load_registry_file_reads_providers_toml(tmp_path: Path) -> None:
    toml_path = tmp_path / "providers.toml"
    toml_path.write_text(
        "\n".join(
            [
                "version = 1",
                "[providers.opencode]",
                'name = "OpenCode"',
                'command = "opencode"',
                "args = ['acp']",
                "timeout_seconds = 120",
                'cost_class = "free"',
                'strength = "unverified"',
            ]
        ),
        encoding="utf-8",
    )

    registry = load_registry_file(toml_path)

    spec = registry["opencode"]
    assert spec.command == "opencode"
    assert spec.timeout_seconds == 120.0
    assert spec.cost_class == "free"


def test_load_registry_file_missing_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_registry_file(tmp_path / "absent.toml")


def test_backend_defaults_to_acp_and_oneshot_parses() -> None:
    """backend 缺省 = acp（存量条目零改动兼容）；oneshot 显式解析并要求占位符。"""
    config = {
        "providers": {
            "acp-family": {"command": "some-cli", "args": ["acp"]},
            "oneshot-family": {
                "command": "node",
                "args": ["agent.cjs", "-p", "{goal}", "--json"],
                "backend": "oneshot",
            },
        }
    }

    registry = load_registry(config)

    assert registry["acp-family"].backend == "acp"
    assert registry["oneshot-family"].backend == "oneshot"
    assert registry["oneshot-family"].args == ("agent.cjs", "-p", "{goal}", "--json")


def test_shipped_providers_toml_parses_and_has_m0_pool() -> None:
    """随包注册表真语料守卫：可解析、M0 现役 opencode 在册且 spawn 面与实证一致。"""
    shipped = Path(__file__).resolve().parents[1] / "scripts" / "providers.toml"

    registry = load_registry_file(shipped)

    spec = registry["opencode"]
    assert spec.command == "opencode"
    assert spec.args == ("acp",)  # ACP spawn 入口（2026-10-15 opencode 1.18.18 实证）
    assert spec.timeout_seconds > 0
    assert spec.cost_class == "free"
    assert spec.backend == "acp"


def test_shipped_providers_toml_has_m1_zcode_oneshot() -> None:
    """M1 真语料守卫：zcode 在册，spawn 面与 -p 无头实证一致（node + cjs + {goal}）。"""
    shipped = Path(__file__).resolve().parents[1] / "scripts" / "providers.toml"

    registry = load_registry_file(shipped)

    spec = registry["zcode"]
    assert spec.backend == "oneshot"
    assert spec.command == "node"
    assert spec.args[0].endswith("zcode.cjs")  # 桌面壳自带 CLI（不在 PATH）
    assert "-p" in spec.args
    assert "{goal}" in spec.args  # 派发目标全文注入点
    assert "--json" in spec.args
    assert "--mode" in spec.args
    assert "yolo" in spec.args  # 免审批注册档
    assert spec.timeout_seconds >= 180  # 无头编码任务给足（任务书口径 3-5 分钟）
    assert spec.cost_class == "paid"
    assert spec.strength == "unverified"  # 基准题定档前 S2 禁接


def test_pick_provider_multi_provider_pool_requires_explicit_choice() -> None:
    """多家注册后省略 provider = 明确报错（M0 单家自动选家语义随多家属显式指定制）。"""
    from acp_pool import _pick_provider

    registry = load_registry(
        {
            "providers": {
                "opencode": {"command": "opencode", "args": ["acp"]},
                "zcode": {"command": "node", "args": ["-p", "{goal}"], "backend": "oneshot"},
            }
        }
    )

    assert _pick_provider(registry, "zcode").id == "zcode"
    with pytest.raises(ValueError, match="显式指定"):
        _pick_provider(registry, "")
