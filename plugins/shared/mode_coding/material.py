"""编码模式物料组装器（模式包 material.py 约定出口的本包实现）。

约定契约（通用模式物料步骤消费，见 plugins/shared/mode_material_inject/
plugin.py）：暴露 ``build_injection(state, pkg_dir)`` 返回本模式追加注入文本
（无则空串）。种子自包含：不 import 共享根模块，依赖仅标准库。

职责（编码模式派发口径注入）：包内 rules/dispatch.md（实现/修复类任务派发纪律：
验收指标必带、完成判据指向交付物内容、接续用 continue、审查验证外包、签发测试
只读、探索显式声明）整文追加到 system prompt 尾部；编码模式激活
（execution_context.mode=coding 或 agent 键归属派生）即生效，派发方由此获得
同一套派发纪律约束。物料与 state 无关，恒整文注入。

降级契约：rules/dispatch.md 是本组装器唯一职责物料，缺失/损坏 = warning 留痕
+ 空串零注入（与通用步骤「声明了但文件缺失 → warning」同口径，声明与实物不符
须可归因），绝不阻断管道。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 派发口径规则文件（相对包根）：注入内容的单一来源。
_DISPATCH_RULES_RELPATH = ("rules", "dispatch.md")


def build_injection(state: dict[str, Any], pkg_dir) -> str:  # noqa: ARG001 — 约定出口签名（通用步骤按契约调用）
    """模式包组装器约定出口：返回编码模式派发口径注入文本（无则空串）。

    Args:
        state: 管道 state（编码模式物料与 state 无关，恒整文注入）。
        pkg_dir: 本模式包目录（通用步骤经 find_package_dir 解析后传入）。
    """
    path = Path(pkg_dir).joinpath(*_DISPATCH_RULES_RELPATH)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning(
            "[mode_coding.material] 派发口径文件不可读，零注入 | path=%s | err=%s",
            path,
            exc,
        )
        return ""
    return text
