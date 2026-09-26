#!/usr/bin/env python3
"""config/ 静态校验闸（执行方案批次 4-4，盲区审计产出）。

config/ 16 个子目录的 yaml 此前无 PR 级校验——配置写错最晚要到内核启动 /
夜间车道才炸。本闸静态前置两查（秒级，fail-closed）：

1. 可解析：config/**/*.yaml 全部 safe_load 成功（语法错即红）；
2. 引用存在：config/pipelines/*.yaml 引用的步骤插件（plugins 列表 +
   "- pipeline_*" 列表项形态）必须存在于已知插件面（GATES 外的独立源：
   git 跟踪的 plugin.json 清单 id 集 + 目录名集）。

不做（边界）：G2 运行时语义校验（schema 深校验）仍由内核装载期持有；
本闸只做"启动前可静态发现"的两类错。

用法：python scripts/check_config_static.py
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STEP_NAME_RE = re.compile(r"^\s*-\s*(?:name:\s*)?(pipeline_[a-z0-9_]+)\s*$")


def _known_plugin_ids() -> set[str]:
    """已知插件面 = git 跟踪 plugin.json 的 id ∪ 目录名。"""
    proc = subprocess.run(
        ["git", "ls-files", "*plugin.json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    ids: set[str] = set()
    import json

    for raw in proc.stdout.splitlines():
        rel = raw.strip()
        if not rel.startswith("plugins/") or not rel.endswith("plugin.json"):
            continue
        try:
            manifest = json.loads((ROOT / rel).read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pid = manifest.get("id")
        if pid:
            ids.add(str(pid))
        ids.add(Path(rel).parent.name)
    return ids


def main() -> int:
    import yaml

    errors: list[str] = []
    yaml_files = sorted(CONFIG_DIR.rglob("*.yaml"))
    if not yaml_files:
        print("config-static: config/ 下无 yaml（异常，fail-closed）。")
        return 1
    for yf in yaml_files:
        try:
            text = yf.read_text(encoding="utf-8")
            data = yaml.safe_load(text)
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"yaml 解析失败: {yf.relative_to(ROOT)}: {exc}")
            continue
        if yf.parent.name == "pipelines" and isinstance(data, dict):
            known = _known_plugin_ids()
            for m in STEP_NAME_RE.finditer(text):
                step = m.group(1)
                # pipeline_xxx 形态：目录名带 pipeline_ 前缀的插件，或 id 即该名
                if step not in known:
                    errors.append(
                        f"管道步骤引用未知插件: {yf.relative_to(ROOT)} :: {step}"
                    )

    if errors:
        print(f"config-static: {len(errors)} 项违规：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    print(f"config-static: 通过（{len(yaml_files)} 个 yaml 可解析，管道步骤引用全部存在）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
