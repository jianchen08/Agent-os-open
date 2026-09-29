#!/usr/bin/env python3
"""任务交接档案生成器：按五要素模板填充生成 handoff 档案。

设计：docs/working/打包发版循环问题与方案_20260929.md §5.3 / 范式 10
（治 E1：后台 agent 被平台限额窗杀死后任务无交接档案，靠主会话人肉盘点）。
约定先行，不改平台：长跑 agent 每完成一个里程碑回写一次
（--force 覆盖）；主会话盘点 = 读 handoff 目录 + health_probe.py。

五要素（模板 docs/working/templates/task_handoff.md，与方案文档同源）：
任务 / 进度 / 环境态（含密钥位置与端口进程占用两个强制记录面）/ 风险 / 下一步。

用法：
  python scripts/write_handoff.py --task-id T-123 --set title=打包批次P0 \\
      --set status=脚本3进行中 --set next=继续脚本4
  常用键：title source acceptance status done commits in_flight secrets
  processes workspace risks unverified next handover（未提供的键落「（待补）」；
  task_id / generated_at 自动填充）。
  默认输出 .agentos/handoff/<task_id>.md（--out 覆盖；已存在须 --force——
  覆盖是显式动作，防误抹他人档案）。
退出码：0 = 已生成；1 = 参数/模板错误。
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
TEMPLATE = _REPO / "docs" / "working" / "templates" / "task_handoff.md"
DEFAULT_OUT_DIR = _REPO / ".agentos" / "handoff"
#: 模板占位符全集（task_id/generated_at 由脚本自动填充）
_TOKENS = (
    "task_id", "generated_at", "title", "source", "acceptance",
    "status", "done", "commits", "in_flight",
    "secrets", "processes", "workspace",
    "risks", "unverified", "next", "handover",
)
_AUTO_TOKENS = ("task_id", "generated_at")
_TBD = "（待补）"


def parse_set(raw: str) -> tuple[str, str]:
    key, sep, value = raw.partition("=")
    if not sep or not key:
        raise ValueError(f"--set 形如 key=value，收到 {raw!r}")
    return key, value


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="生成任务交接档案（五要素模板填充）")
    parser.add_argument("--task-id", required=True, help="任务 id（决定默认文件名 <task_id>.md）")
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                        help=f"填充字段，可重复；可用键：{', '.join(t for t in _TOKENS if t not in _AUTO_TOKENS)}")
    parser.add_argument("--out", type=Path, default=None,
                        help="输出路径（默认 .agentos/handoff/<task_id>.md）")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的档案（里程碑回写用）")
    args = parser.parse_args()

    values: dict[str, str] = dict.fromkeys(_TOKENS, _TBD)
    values["task_id"] = args.task_id
    values["generated_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    for raw in args.set:
        try:
            key, value = parse_set(raw)
        except ValueError as exc:
            print(f"[handoff] {exc}")
            return 1
        if key in _AUTO_TOKENS or key not in _TOKENS:
            valid = ", ".join(t for t in _TOKENS if t not in _AUTO_TOKENS)
            print(f"[handoff] 未知或自动填充键 {key!r}（可用键：{valid}）")
            return 1
        values[key] = value

    if not TEMPLATE.is_file():
        print(f"[handoff] 模板缺失：{TEMPLATE}")
        return 1
    text = TEMPLATE.read_text(encoding="utf-8")
    # 模板与脚本占位符全集一致性（模板漂移即红，fail-closed）
    template_tokens = set(re.findall(r"\{\{(\w+)\}\}", text))
    if template_tokens != set(_TOKENS):
        print(f"[handoff] 模板占位符与脚本全集不一致："
              f"模板多出 {sorted(template_tokens - set(_TOKENS))}，"
              f"缺失 {sorted(set(_TOKENS) - template_tokens)}")
        return 1
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", value)

    out = args.out or (DEFAULT_OUT_DIR / f"{args.task_id}.md")
    if out.exists() and not args.force:
        print(f"[handoff] 已存在 {out}（里程碑回写请显式 --force，防误抹）")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    filled = sum(1 for t in _TOKENS if values[t] != _TBD) - len(_AUTO_TOKENS)
    print(f"[handoff] 已生成 {out}（显式填充 {filled} 项，待补 "
          f"{len(_TOKENS) - len(_AUTO_TOKENS) - filled} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
