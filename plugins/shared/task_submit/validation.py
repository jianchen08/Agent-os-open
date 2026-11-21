"""workspace 与验收材料校验域（workspace 路径 / allowed_paths / signed acceptance tests）（自 tool.py 按职责拆分）。"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from _libs.dangerous_targets import check_root_and_system_dirs

from agentos_plugin_sdk import ToolExecutionResult, create_failure_result

logger = logging.getLogger(__name__)

def _validate_workspace_path(workspace: str) -> str | None:
    """验证目标空间路径的安全性。

    磁盘根目录/系统危险目录检查单源 ``_libs.dangerous_targets``（盘符根判定
    =路径形态而非运行平台——Linux 侧车上对 "C:\\" 形态同样必须拒）。
    """
    if not workspace:
        return None

    # 规范化路径用于比较
    try:
        normalized = os.path.normpath(workspace)
    except (ValueError, TypeError):
        return f"目标空间路径无效: {workspace}"

    Path(normalized)

    # ── 1+2. 磁盘根目录 / 系统危险目录检查（单源：_libs.dangerous_targets）──
    root_or_system_err = check_root_and_system_dirs(normalized, workspace)
    if root_or_system_err:
        return root_or_system_err

    # ── 3. 配置文件工作空间根目录检查 ──
    normalized_lower = normalized.lower()
    try:
        from isolation.workspace import get_workspace_config_root  # noqa: PLC0415

        ws_root = get_workspace_config_root()
        ws_root_normalized = os.path.normpath(ws_root)
        if normalized_lower == ws_root_normalized.lower():
            return (
                f"目标空间不能设置为当前配置的工作空间根目录: {workspace}。"
                f"该目录是系统管理工作空间的根目录，不允许作为任务目标操作。"
            )
    except Exception as e:
        logger.warning("[TaskSubmit] 读取工作空间配置根目录失败，跳过该检查 | error=%s", e)

    return None


def _validate_allowed_paths(value: Any) -> ToolExecutionResult | None:
    """任务书改动授权路径清单轻校验（E0-45/D3 字段面，消费方=scope_consistency）。

    只做契约面形态校验：数组（或缺省/None）且元素均为非空字符串（path 形态）；
    语义（越界检测）留给消费方。字段缺省/空数组=合法（不启用改动范围约束）。
    非法形态拒绝且错误信息指明具体字段与元素索引。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return create_failure_result(
            error=f"allowed_paths 形态非法：须为数组（元素为非空字符串路径），收到 {type(value).__name__}。",
            error_code="INVALID_ALLOWED_PATHS",
        )
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            return create_failure_result(
                error=(
                    f"allowed_paths 形态非法：第 {i} 个元素须为非空字符串"
                    f"（改动授权路径，相对工作区），收到 {type(item).__name__}。"
                ),
                error_code="INVALID_ALLOWED_PATHS",
            )
    return None


def _validate_signed_acceptance_tests(value: Any) -> ToolExecutionResult | None:
    """签发验收测试清单轻校验（E0-24/25 字段面，签发分离）。

    只做契约面结构完整性校验：数组（或缺省/None）且每个元素为对象、含
    path/frozen_version/issuer 三键、issuer 为非空字符串（签发方可追溯）；
    语义（产出验收测试未在签发后修改）留给消费方。字段缺省/空数组=合法。
    非法形态拒绝且错误信息指明具体缺失/非法的字段名。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        return create_failure_result(
            error=(
                "signed_acceptance_tests 形态非法：须为数组（元素为"
                "{path, frozen_version, issuer} 对象），"
                f"收到 {type(value).__name__}。"
            ),
            error_code="INVALID_SIGNED_ACCEPTANCE_TESTS",
        )
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            return create_failure_result(
                error=(
                    f"signed_acceptance_tests 形态非法：第 {i} 个元素须为对象"
                    "（含 path/frozen_version/issuer 三键），"
                    f"收到 {type(item).__name__}。"
                ),
                error_code="INVALID_SIGNED_ACCEPTANCE_TESTS",
            )
        missing = [key for key in ("path", "frozen_version", "issuer") if key not in item]
        if missing:
            return create_failure_result(
                error=(
                    f"signed_acceptance_tests 第 {i} 个元素缺少字段：{missing}"
                    "（须含 path/frozen_version/issuer 三键）。"
                ),
                error_code="INVALID_SIGNED_ACCEPTANCE_TESTS",
            )
        issuer = item.get("issuer")
        if not isinstance(issuer, str) or not issuer.strip():
            return create_failure_result(
                error=(
                    f"signed_acceptance_tests 第 {i} 个元素的 issuer 形态非法："
                    f"须为非空字符串（签发方标识），收到 {issuer!r}。"
                ),
                error_code="INVALID_SIGNED_ACCEPTANCE_TESTS",
            )
    return None
