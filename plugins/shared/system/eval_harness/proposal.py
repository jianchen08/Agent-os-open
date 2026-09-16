# -*- coding: utf-8 -*-
"""提案七条静态校验（纯函数，机械执行）。

提案语义（MVP）：整文件替换——content 为提案后的完整文件内容，target 为主仓
相对路径。七条全部通过才允许落 staging；任一失败返回违规清单。
"""
from __future__ import annotations

import hashlib
import os
from typing import Any

# ② 冻结面（路径前缀一票拒绝）——B 部类 + 内核 + 受控区 + 安全红线文件
# + 裁决权载体（ADR 2026-09-16：评估/复盘面整体冻结——阈值参数直接改变
# 判定松紧，被裁决者不可经提案改裁判，P3 一票否决的结构性防线）
FROZEN_PREFIXES = (
    "config/self_evolve/",
    "plugins/shared/system/eval_harness/",
    "plugins/shared/modes/",
    "plugins/shared/system/evaluation/",
    "plugins/shared/system/review/",
    "config/plugins/evaluation/",
    "config/plugins/review/",
    "kernel/",
    ".env",
    "config/kernel/",
    "config/plugins/isolation/",
    "config/rules/information_integrity_rules.md",
    "config/rules/document_context_rules.md",
    "tests/eval_bench/",
)

# ③ 路径白名单（A 部类可改面，MVP 用前缀表近似登记表）
EVOLVABLE_PREFIXES = (
    "config/agents/",
    "config/pipelines/",
    "config/rules/",
    "plugins/shared/tools/auto_gen_",
    "plugins/shared/pipeline/auto_gen_",
)

REQUIRED_FIELDS = ("layer", "target", "change_type", "content")
CHANGE_TYPES = {"param_edit", "content_edit", "list_add", "list_remove", "new_plugin"}


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(path: str) -> str:
    p = str(path).replace("\\", "/")
    # 仅剥前导 "./" 序列（不能按字符集剥：".env" 与 "../x" 不可损坏）
    while p.startswith("./"):
        p = p[2:]
    return p


def validate(proposal: dict[str, Any], project_root: str) -> list[str]:
    """返回违规清单；空列表 = 通过。"""
    violations: list[str] = []

    # ① schema 完整性
    for field in REQUIRED_FIELDS:
        if not str(proposal.get(field) or "").strip():
            violations.append(f"schema: 缺必填字段 {field}")
    if proposal.get("change_type") not in CHANGE_TYPES:
        violations.append(f"schema: change_type 非法 {proposal.get('change_type')!r}")
    if violations:
        return violations

    target = _norm(str(proposal["target"]))
    root_abs = os.path.abspath(project_root)
    abs_target = os.path.abspath(os.path.join(root_abs, target))

    # 逃逸检查：target 解析后必须仍在项目根内
    if os.path.commonpath([root_abs, abs_target]) != root_abs:
        violations.append(f"whitelist: 目标路径逃逸项目根 {target}")
        return violations

    # ② 冻结面（前缀一票拒绝）
    for prefix in FROZEN_PREFIXES:
        if target.startswith(prefix):
            violations.append(f"frozen: 触碰冻结面 {prefix}*")
            return violations

    # ③ 路径白名单
    if not any(target.startswith(p) for p in EVOLVABLE_PREFIXES):
        violations.append(f"whitelist: 目标不在可改面内 {target}")

    # ④ 原子性（MVP：单文件提案）
    if "\n---PROPOSAL-FILE---\n" in str(proposal.get("content", "")):
        violations.append("atomicity: 多文件提案（MVP 仅支持单文件）")

    # ⑤ base 一致性（目标文件当前内容哈希与提案声明一致；新文件豁免）
    base_hash = str(proposal.get("base_hash") or "").strip()
    if os.path.isfile(abs_target):
        current = file_sha256(abs_target)
        if base_hash and base_hash != current:
            violations.append("base: 目标文件已前移（base_hash 不符），请基于最新内容重新生成")
    else:
        if proposal["change_type"] != "new_plugin" and proposal.get("layer") != "L3":
            violations.append("base: 目标文件不存在且提案非新增类")

    # ⑥ 写面唯一：由 server 层落盘保证（staging 目录），此处校验提案未内嵌绝对路径
    if re_search_abs_path(str(proposal.get("content", ""))):
        violations.append("write_scope: content 内嵌绝对路径（应使用相对路径与占位符）")

    # ⑦ 题面相似度：MVP 跳过（嵌入检索二期），记录 skipped 不计违规
    return violations


def re_search_abs_path(content: str) -> bool:
    import re
    return bool(re.search(r"[A-Za-z]:\\\\|(?<![\"'\w])/(?:Users|home)/", content))
