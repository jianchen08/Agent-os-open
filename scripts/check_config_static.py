#!/usr/bin/env python3
"""config/ 静态校验闸（执行方案批次 4-4，盲区审计产出）。

config/ 16 个子目录的 yaml 此前无 PR 级校验——配置写错最晚要到内核启动 /
夜间车道才炸。本闸静态前置三查（秒级，fail-closed）：

1. 可解析：config/**/*.yaml 全部 safe_load 成功（语法错即红）；
2. 引用存在：config/pipelines/*.yaml 引用的步骤插件（plugins 列表 +
   "- pipeline_*" 列表项形态）必须存在于已知插件面（GATES 外的独立源：
   git 跟踪的 plugin.json 清单 id 集 + 目录名集）；
3. 技能引用存在（2026-09-29，R300 装机悬空引用防回潮）：config/** 文本
   （yaml + md）里 ``skills/<名>/SKILL.md`` 形态的技能索引引用必须命中
   权威源并集（git 跟踪的仓根 skills/*/SKILL.md ∪ 出厂模式包
   plugins/shared/modes/mode_*/skills/*/SKILL.md）。提示词把技能路径报给
   模型而不保证实体存在 = R300 事故链源头（模型按 {{user_root}} 拼绝对
   路径 File not found），悬空引用一律 PR 阶段消灭。整行注释与通配形态
   （skills/x-*/SKILL.md）不入扫。

不做（边界）：G2 运行时语义校验（schema 深校验）仍由内核装载期持有；
本闸只做"启动前可静态发现"的三类错。

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
#: 技能索引引用形态（具体名，不含通配；路径前缀任意——工作空间快照相对
#: 引用 skills/<名>/SKILL.md 与 dev 全路径 …/skills/<名>/SKILL.md 同判）。
SKILL_REF_RE = re.compile(r"skills/([A-Za-z0-9._-]+)/SKILL\.md")
#: 技能引用扫描的文本面（yaml 配置 + md 说明文档都会把路径报给模型）。
SKILL_REF_EXTS = {".yaml", ".yml", ".md"}


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


def _known_skill_names() -> set[str]:
    """技能权威源并集 = git 跟踪的仓根 skills/*/SKILL.md ∪ 出厂模式包
    plugins/shared/modes/mode_*/skills/*/SKILL.md（与 workspace_lifecycle
    _skill_sources 的 dev 侧源同口径；用户根 skills/ 是装机补种通道，不是
    PR 阶段可引用的新增源——包内没有的引用装到真用户机上必然悬空）。"""
    proc = subprocess.run(
        ["git", "ls-files", "skills/*/SKILL.md", "plugins/shared/modes/*/skills/*/SKILL.md"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    return {Path(rel.strip()).parent.name for rel in proc.stdout.splitlines() if rel.strip()}


def _skill_ref_violations(text: str, rel: Path, known: set[str]) -> list[str]:
    """单文件技能引用悬空清单（整行注释与通配形态不入扫）。"""
    found: list[str] = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        for m in SKILL_REF_RE.finditer(line):
            name = m.group(1)
            if name not in known:
                found.append(f"技能索引引用悬空（权威源无实体）: {rel}:{i} :: {m.group(0)}")
    return found


def main() -> int:
    import yaml

    errors: list[str] = []
    yaml_files = sorted(CONFIG_DIR.rglob("*.yaml"))
    if not yaml_files:
        print("config-static: config/ 下无 yaml（异常，fail-closed）。")
        return 1
    known_skills: set[str] | None = None
    text_files = sorted(p for ext in SKILL_REF_EXTS for p in CONFIG_DIR.rglob(f"*{ext}"))
    for tf in text_files:
        try:
            text = tf.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"文本读取失败: {tf.relative_to(ROOT)}: {exc}")
            continue
        if known_skills is None:
            known_skills = _known_skill_names()
        errors.extend(_skill_ref_violations(text, tf.relative_to(ROOT), known_skills))
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
    print(
        f"config-static: 通过（{len(yaml_files)} 个 yaml 可解析，管道步骤引用与"
        f"技能索引引用（{len(text_files)} 文件）全部存在）。"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
