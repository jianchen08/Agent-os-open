# @feature: FP-0.2.一 第三方插件协议 | @vision: V3 可嵌入 | @ci: python-coverage
"""工具结果配对消息仓扫守卫（ADR 2026-09-28 固定函数路径）。

SDK 之外任何插件代码不得自建 ``{"role": "tool", "tool_call_id": ...}`` 配对
消息——构造一律经 ``agentos_plugin_sdk.tool_result_protocol``（契约夹具与
tool_core Rust 双车道锚定）。判定规则：非测试 .py 文件同时命中
role=tool 字面量与 tool_call_id 键构造即违规（result_format 的
role=tool 无 tool_call_id，属结果格式化面，自动豁免）。

允许清单自带活性断言：条目必须仍在触发——迁移批次完成后条目不摘除即红
（清单自清洁，不许死条目长存）。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("plugins/shared", "plugins/modes")

# 剪枝：依赖/产物目录不扫（venv、node_modules、构建产物——既非本仓插件
# 源码，体量又会让逐文件扫描分钟级卡死）。
PRUNE_DIRS = {"__pycache__", "node_modules", "target", "runtime", "extra-tools", ".git"}

ROLE_TOOL_RE = re.compile(r"""["']role["']\s*:\s*["']tool["']""")
TOOL_CALL_ID_RE = re.compile(r"""["']tool_call_id["']\s*:""")

# 允许清单（相对仓根 posix 路径；每项须带理由，迁移批次完成后摘除）：
# - llm_core：生成中断占位（非工具结果：无 envelope、带 status=interrupted，
#   语义是"工具未执行"告知，不是工具结果协议成员）。
ALLOWLIST: dict[str, str] = {
    "plugins/shared/pipeline/core/llm_core/plugin.py": "中断占位非工具结果",
}


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_") or path.name == "conftest.py"


def _iter_plugin_py(scan_dir: str):
    for dirpath, dirnames, filenames in os.walk(ROOT / scan_dir):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS and not d.startswith(".")]
        for filename in filenames:
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def _scan_violations() -> list[str]:
    violations: list[str] = []
    for scan_dir in SCAN_DIRS:
        for path in _iter_plugin_py(scan_dir):
            if _is_test_file(path):
                continue
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWLIST:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if ROLE_TOOL_RE.search(text) and TOOL_CALL_ID_RE.search(text):
                violations.append(rel)
    return violations


def test_no_paired_tool_message_literals_outside_protocol() -> None:
    """违规扫描：SDK 外零自建配对消息（发现即红，带文件清单）。"""
    violations = _scan_violations()
    assert not violations, (
        "以下文件自建 role=tool 配对消息，违反固定函数路径"
        f"（ADR 2026-09-28，应经 agentos_plugin_sdk.tool_result_protocol）: {violations}"
    )


def test_allowlist_entries_are_live() -> None:
    """清单活性：每个允许条目必须仍存在且仍触发——迁移完成后不摘即红。"""
    for rel, reason in ALLOWLIST.items():
        path = ROOT / rel
        assert path.exists(), f"允许清单死条目（文件已不存在）: {rel}"
        text = path.read_text(encoding="utf-8", errors="replace")
        assert ROLE_TOOL_RE.search(text), f"允许清单条目已不再触发 role=tool（{reason}——迁移已完成，应摘除该条目）: {rel}"
        assert TOOL_CALL_ID_RE.search(text), f"允许清单条目已不再触发 tool_call_id（{reason}——迁移已完成，应摘除该条目）: {rel}"


def test_detector_flags_paired_construction() -> None:
    """探测器自证（防拟合）：内联样本必须被双模式命中，缺一即探测器失效。"""
    sample = '{"role": "tool", "tool_call_id": cid, "content": "x"}'
    assert ROLE_TOOL_RE.search(sample)
    assert TOOL_CALL_ID_RE.search(sample)
    # 负对照：仅 role=tool 无 tool_call_id（result_format 面）不触发。
    only_role = "{'role': 'tool', 'name': name, 'content': content}"
    assert ROLE_TOOL_RE.search(only_role)
    assert not TOOL_CALL_ID_RE.search(only_role)
