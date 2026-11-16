# @feature: FP-0.2.一 Agent 运行时 | @ci: python-coverage
"""Agent 配置面「不设 run 墙钟预算」护栏。

用户裁定（2026-10-17）：任务可合法运行数小时，run 墙钟预算在该场景纯误伤——
预算耗尽时引擎 `force_timeout_signature` 会强制终态化（`ended=true` +
`router.stop_reason=timeout`），长任务在成稿落盘前即被收走。1016 三任务
（恰 30m00.07s / 40m00.3s）与 1017 code_writer 两任务（恰 40m00s）为实证。

护栏落在这里而非单点物料测试：口径是全 Agent 配置面的，一次只改一个文件必然
再漏（1017 事故即 config/agents/** 已摘而 plugins/shared/mode_*/agents/** 未摘）。
防挂死由单次调用上界（`plugin_call_timeout`，内核恒有界）承接，防失控由
`max_iterations` 承接，两者本护栏不涉及。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]

# Agent 配置的来源：主配置树、模式包（agent 配置随插件走）、模板。
# （config-e2e 镜像副本已随 P-16 删除——仓内配置唯一真源 = config/，护栏
# 扫描面随之收敛到真源，见 config/README.md 声明。）
_AGENT_CONFIG_GLOBS = (
    "config/agents/**/*.yaml",
    "plugins/shared/mode_*/agents/*.yaml",
    "config/templates/*.yaml",
)


def _discover() -> list[Path]:
    files: list[Path] = []
    for pattern in _AGENT_CONFIG_GLOBS:
        files.extend(ROOT.glob(pattern))
    return sorted({f for f in files if f.is_file()})


AGENT_CONFIGS = _discover()


def test_discovery_covers_every_source_tree() -> None:
    """防拟合：发现集非空，且三类来源各自命中——防 glob 写错后全量用例静默通过。"""
    missing = [
        pattern
        for pattern in _AGENT_CONFIG_GLOBS
        if not list(ROOT.glob(pattern))
    ]
    assert not missing, f"Agent 配置来源发现为空（glob 失效将让护栏静默放行）：{missing}"
    assert len(AGENT_CONFIGS) >= 20, (
        f"Agent 配置样本过少（{len(AGENT_CONFIGS)} 件），护栏覆盖面存疑"
    )


@pytest.mark.parametrize("path", AGENT_CONFIGS, ids=lambda p: str(p.relative_to(ROOT)))
def test_agent_config_declares_no_wall_clock_budget(path: Path) -> None:
    """Agent 配置顶层不得声明 timeout_seconds——键缺席即引擎看门狗不武装。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"Agent 配置必须解析为 dict：{path}"
    assert "timeout_seconds" not in data, (
        f"{path.relative_to(ROOT)} 声明了 run 墙钟预算 timeout_seconds="
        f"{data['timeout_seconds']}：预算耗尽时引擎强制终态化，长任务会被误杀。"
        f"去掉该键；防挂死靠 plugin_call_timeout 单次上界，防失控靠 max_iterations。"
    )
