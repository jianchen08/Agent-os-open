#!/usr/bin/env python3
# @feature: FP-0.2.〇 项目类型 CI 包
"""素材文件规范引擎（base/block）：读项目 local/asset_rules.json 执法（J4：
工厂出引擎不出规则——无规则文件=skip，规则内容归项目自治）。

规则 schema（local/asset_rules.json）：
  {"rules": [{"match": ["*.wav", "*.psd"],   # 文件名/相对路径 glob（命中任一）
              "allow_dirs": ["assets/audio"],  # 允许存放的根相对目录（命中任一即合规）
              "note": "音频只准入 assets/audio/"}]}
规则文件不可解析或缺 rules 键 = 红（fail-closed，规则面写坏不是"无规则"）。
"""

from __future__ import annotations

import fnmatch
import json
import sys

from _common import CI_DIR, input_files, is_under, to_posix

RULES_PATH = CI_DIR / "local" / "asset_rules.json"


def load_rules() -> tuple[list[dict] | None, str]:
    """返回（规则列表, skip/错误原因）。规则列表 None 时按原因决定 exit 1 或 2。"""
    if not RULES_PATH.is_file():
        return None, "skip: 无 local/asset_rules.json（素材规范未声明，引擎不执法）"
    try:
        data = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return None, f"规则文件不可解析: {exc}"
    rules = data.get("rules") if isinstance(data, dict) else None
    if not isinstance(rules, list):
        return None, '规则文件须为 {"rules": [...]} 形态'
    return rules, ""


def main() -> int:
    rules, reason = load_rules()
    if rules is None:
        print(reason)
        return 2 if reason.startswith("skip") else 1
    findings: list[str] = []
    for rel in input_files():
        posix = to_posix(rel)
        name = posix.rsplit("/", 1)[-1]
        for rule in rules:
            if not isinstance(rule, dict):
                print("规则条目须为对象")
                return 1
            globs = [g for g in rule.get("match") or [] if isinstance(g, str)]
            allow = [d for d in rule.get("allow_dirs") or [] if isinstance(d, str)]
            if not globs or not allow:
                print("规则条目缺 match/allow_dirs")
                return 1
            matched = any(fnmatch.fnmatch(name, g) or fnmatch.fnmatch(posix, g) for g in globs)
            if matched and not is_under(posix, allow):
                note = str(rule.get("note") or "").strip()
                findings.append(f"{posix}: 违反素材规范（仅允许 {'|'.join(allow)}）" + (f"——{note}" if note else ""))
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
