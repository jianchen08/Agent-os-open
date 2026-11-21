# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: python-coverage
"""task_reminder 配置声明面契约测试——agent yaml 声明键与插件消费键对齐。

插件真实消费的 ``plugins.enabled.task_reminder`` 配置键（plugin.py
``__init__`` / ``_max_reminders_for`` / ``_max_reminders_total_for``）：

- ``max_reminders``：per-run 提醒预算上限；
- ``max_reminders_total``：跨 run 任务级总预算上限；
- ``priority``：输出插件优先级。

评估者模式不再走配置键：路由键 = 管道 state 的 ``evaluation.mode`` /
``evaluation.metric_id``（task_evaluate 派发评估子管道时写入）。

本测试扫描声明面（config/agents/** 与 plugins/shared/**/agents/*.yaml），
断言两件事：

1. 任何 task_reminder 配置块声明的键都落在上述消费键集合内——声明了
   插件不消费的键 = 配置写了不生效的契约漂移（X12 巡检 P-132）；
2. 声明面上 ``cooldown_seconds`` 零残留——该键曾被 14 处 yaml 声明而
   插件零消费（X25-F1 删除路线清理：提醒发射为轮次驱动、无时间维度，
   接线需新增时钟锚设计与被抑制轮路由语义，触碰提醒闸门节奏）。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]

# 插件消费键白名单（与 plugin.py 消费面一一对应；改动消费面时同步本表）
_CONSUMED_KEYS = {"max_reminders", "max_reminders_total", "priority"}

# 声明面：agent 配置 yaml（含 mode 插件内置 agent 与模板规格）
_DECLARATION_ROOTS = ["config/agents", "config/templates", "plugins/shared"]

_SKIP_DIRS = {"node_modules", ".venv", "__pycache__", "runtime", "extra-tools"}

_TASK_REMINDER_RE = re.compile(r"^(\s*)task_reminder:\s*$")
_YAML_SUFFIXES = (".yaml", ".yml")


def _yaml_files_under(roots: list[str]) -> list[Path]:
    files: list[Path] = []
    for rel in roots:
        base = _REPO_ROOT / rel
        if not base.is_dir():
            continue
        # os.walk 而非 Path.walk：后者是 3.12+ API，CI/精简环境 3.11 无此方法
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for fn in filenames:
                if fn.endswith(_YAML_SUFFIXES):
                    files.append(Path(dirpath) / fn)
    return sorted(files)


def _task_reminder_block_keys(text: str) -> set[str]:
    """提取 yaml 文本中全部 task_reminder 块的直接子键（跳过注释行）。

    块内嵌套更深层级（嵌套映射/块标量）不属直接子键，按缩进过滤。
    """
    lines = text.splitlines()
    keys: set[str] = set()
    for i, ln in enumerate(lines):
        m = _TASK_REMINDER_RE.match(ln)
        if m is None:
            continue
        block_indent = len(m.group(1))
        child_indent: int | None = None
        for nxt in lines[i + 1 :]:
            stripped = nxt.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(nxt) - len(nxt.lstrip())
            if indent <= block_indent:
                break
            if child_indent is None:
                child_indent = indent
            if indent == child_indent:
                keys.add(stripped.split(":")[0].strip().strip("'\""))
    return keys


class TestTaskReminderConfigContract:
    def test_blocks_declare_only_consumed_keys(self) -> None:
        """声明面 task_reminder 块的每个键都必须被插件消费（fail on drift）。"""
        drifted: dict[str, set[str]] = {}
        for path in _yaml_files_under(["config/agents", "plugins/shared"]):
            keys = _task_reminder_block_keys(path.read_text(encoding="utf-8"))
            unknown = keys - _CONSUMED_KEYS
            if unknown:
                drifted[str(path.relative_to(_REPO_ROOT))] = unknown
        assert not drifted, (
            "task_reminder 配置块声明了插件不消费的键（契约漂移，"
            f"消费键白名单={sorted(_CONSUMED_KEYS)}）：{drifted}"
        )

    def test_cooldown_seconds_zero_residue(self) -> None:
        """声明面（含注释行）cooldown_seconds 零残留。"""
        residue: list[str] = []
        for path in _yaml_files_under(_DECLARATION_ROOTS):
            for lineno, ln in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "cooldown_seconds" in ln:
                    residue.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}")
        assert not residue, f"声明面仍有 cooldown_seconds 残留：{residue}"
