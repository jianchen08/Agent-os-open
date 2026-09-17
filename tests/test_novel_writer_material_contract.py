# @feature: FP-0.2.二 写作模式执行链 | @ci: python-coverage
"""novel_writer_agent 物料契约测试（BUG-32）。

写作执行者是写作模式链的落盘出口：物料必须自持「成稿经 file_write 落盘再
收尾」的指令闭环 + 落盘/评估工具声明。物料半成品（缺 tool_ids 或缺落盘
指令）在派发打通后会以"纯聊输出零工具调用"形态暴露（BUG-32 现场参考，
根因为 context_build 层级键断链，物料本身须保持契约完整防回退）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
AGENT_YAML = ROOT / "config" / "agents" / "executor" / "generation" / "novel_writer_agent.yaml"

# 落盘出口的最小工具面：写文件 + 收尾评估（缺一任务就无法既留产物又过闸门）。
_REQUIRED_TOOL_IDS = ("file_write", "task_evaluate")


@pytest.fixture(scope="module")
def agent_cfg() -> dict:
    import yaml as _yaml

    with open(AGENT_YAML, encoding="utf-8") as f:
        data = _yaml.safe_load(f)
    assert isinstance(data, dict), f"agent yaml 必须解析为 dict：{AGENT_YAML}"
    return data


def test_yaml_lives_at_hierarchical_key_path() -> None:
    """物料必须存在于层级键同构路径（agent.id=executor/generation/novel_writer_agent
    经注册表相对路径直解析命中）。"""
    assert AGENT_YAML.is_file(), f"层级键物料缺失：{AGENT_YAML}"


def test_tool_ids_declare_persistence_surface(agent_cfg: dict) -> None:
    """tool_ids 必须声明落盘与评估工具（缺声明 = 工具面即使接通也无出口）。"""
    tool_ids = agent_cfg.get("tool_ids")
    assert isinstance(tool_ids, list) and tool_ids, "tool_ids 必须为非空列表"
    missing = [t for t in _REQUIRED_TOOL_IDS if t not in tool_ids]
    assert not missing, f"tool_ids 缺少落盘链必需工具: {missing}"


@pytest.mark.parametrize(
    "keyword",
    ["写入", "输出文件", "output_path"],
    ids=["write_directive", "output_file_step", "output_path_anchor"],
)
def test_system_prompt_carries_persistence_directive(agent_cfg: dict, keyword: str) -> None:
    """system_prompt 必须含「成稿落盘」指令锚点（输出文件步骤 + 输出路径消费）。"""
    prompt = agent_cfg.get("system_prompt", "")
    assert keyword in prompt, (
        f"system_prompt 缺少落盘指令锚点 {keyword!r}——纯聊输出形态的物料回退征兆"
    )


@pytest.mark.parametrize(
    "anchor",
    ["file_write", "禁止", "task_evaluate"],
    ids=["write_tool_named", "chat_delivery_forbidden", "evaluate_gated_on_write"],
)
def test_system_prompt_carries_write_before_evaluate_gate(agent_cfg: dict, anchor: str) -> None:
    """BUG-33 契约：落盘指令必须是无条件的工具级指令——成稿只经 file_write 交付
    （禁止聊天正文交付），且 file_write 成功是调用 task_evaluate 的前置条件。
    路径条件式措辞（"写入指定路径（output_path）"）在 task_submit 派发不填
    input_schema 时会被模型视为不适用，成稿以聊天正文交付（BUG-33 现场）。"""
    prompt = agent_cfg.get("system_prompt", "")
    assert anchor in prompt, f"system_prompt 缺少落盘闸门锚点 {anchor!r}"


def test_timeout_budget_declared(agent_cfg: dict) -> None:
    """长创作任务必须显式声明超时预算（断链时 stop_check 落 600s 默认值先杀任务）。"""
    timeout = agent_cfg.get("timeout_seconds")
    assert isinstance(timeout, int) and timeout >= 600, (
        "timeout_seconds 必须为 ≥600 的整数（创作任务耗时超过默认 600s）"
    )
