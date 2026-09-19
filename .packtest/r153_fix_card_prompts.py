"""R153：三张角色卡 system_prompt 占位符内联真实字段值（BUG-46 修复）。

插值来源 = 同 yaml 顶层字段（description/personality/scenario），逐字面替换后回写。
"""
import io
import json
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

AGENTS = os.path.join(os.environ["APPDATA"], "agentos", "plugins", "modes", "mode_roleplay", "agents")

import yaml

for fn in sorted(os.listdir(AGENTS)):
    if not fn.endswith(".yaml"):
        continue
    fp = os.path.join(AGENTS, fn)
    raw = open(fp, encoding="utf-8").read()
    d = yaml.safe_load(raw)
    sp = d.get("system_prompt") or ""
    if not sp:
        print(fn, ": no system_prompt, skip")
        continue
    new = sp
    for field in ("description", "personality", "scenario"):
        val = str(d.get(field) or "").strip()
        if val:
            new = new.replace("{" + field + "}", val)
    leftover = re.findall(r"\{[a-z_]+\}", new)
    if new != sp:
        # yaml safe_dump 会重排结构；只在原文件上做文本级替换以保注释。
        # 用 block scalar 重建 system_prompt 段最稳：直接重写整个文件（注释会丢——
        # 卡文件注释是创作说明，保留它更稳妥 → 改用逐字段文本替换法）。
        # 文本级：把 system_prompt 块里的 {field} 占位符换为多行字面值（缩进对齐）。
        out = raw
        for field in ("description", "personality", "scenario"):
            val = str(d.get(field) or "").strip()
            if not val:
                continue
            pat = "{" + field + "}"
            if pat in out:
                # 多行值按原行缩进（system_prompt block 的缩进 = 2 空格）
                indented = val.replace("\n", "\n  ")
                out = out.replace(pat, indented)
        open(fp, "w", encoding="utf-8", newline="\n").write(out)
        print(fn, ": inlined", json.dumps(re.findall(r"\{[a-z_]+\}", sp)), "-> leftover", leftover)
    else:
        print(fn, ": no placeholders, leftover", leftover)
