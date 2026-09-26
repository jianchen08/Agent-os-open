"""onboarding_service 内容装载校验（fail-closed）。

内容即数据契约（docs/decisions/2026-09-24-onboarding-plugin.md）：walkthrough /
step / 完成条件 / CTA 全声明式 JSON；本模块是唯一校验实现——schema 校验不过
即拒载（错误上浮给调用方，不静默跳过）。前端求值器（services/onboarding/evaluator.ts）
与本模块的条件类型枚举保持同词表。

完成条件词表：api_check / panel_visited / mode_selected / cta_clicked / manual
+ 复合 all / any（嵌套深度 ≤ 3）；api_check 仅 GET 且端点必须命中白名单。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# api_check 端点白名单（仅 GET）：新增检测源 = 本表加条目 + 前端可达性核对。
VALID_API_CHECK_ENDPOINTS: frozenset[str] = frozenset({
    "/ext/llm_service/config/llm",  # providers / defaults.chat 双检（配模型完成判定）
    "/api/v1/n",  # 会话列表（threads 非空 = 已发出首条消息）
})

# 步骤内嵌向导注册表（P0 仅 llm_setup；未注册键装载即拒）。
WIZARD_REGISTRY: frozenset[str] = frozenset({"llm_setup"})

_COMPLETION_LEAF_TYPES = frozenset({
    "api_check", "panel_visited", "mode_selected", "cta_clicked", "manual",
})
_COMPLETION_COMPOSITE_TYPES = frozenset({"all", "any"})
_API_CHECK_OPS = frozenset({"non_empty"})
_CTA_ACTION_TYPES = frozenset({"open_panel", "switch_mode", "external_url", "focus_composer"})
_MAX_COMPOSITE_DEPTH = 3


class ContentValidationError(Exception):
    """内容校验失败（携带全部错误行）。"""


def _require_str(data: dict[str, Any], key: str, errors: list[str], where: str) -> None:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{where}: 字段 {key} 必须是非空字符串")


def _validate_completion(cond: Any, errors: list[str], where: str, depth: int = 0) -> None:
    if not isinstance(cond, dict):
        errors.append(f"{where}: completion 必须是对象")
        return
    ctype = cond.get("type")
    if ctype in _COMPLETION_COMPOSITE_TYPES:
        if depth >= _MAX_COMPOSITE_DEPTH:
            errors.append(f"{where}: 复合条件嵌套超过 {_MAX_COMPOSITE_DEPTH} 层")
            return
        conditions = cond.get("conditions")
        if not isinstance(conditions, list) or not conditions:
            errors.append(f"{where}: 复合条件 {ctype} 的 conditions 必须是非空数组")
            return
        for i, sub in enumerate(conditions):
            _validate_completion(sub, errors, f"{where}.conditions[{i}]", depth + 1)
        return
    if ctype not in _COMPLETION_LEAF_TYPES:
        errors.append(f"{where}: 未知完成条件类型 {ctype!r}")
        return
    if ctype == "api_check":
        endpoint = cond.get("endpoint")
        if endpoint not in VALID_API_CHECK_ENDPOINTS:
            errors.append(
                f"{where}: api_check.endpoint {endpoint!r} 不在白名单 "
                f"{sorted(VALID_API_CHECK_ENDPOINTS)}"
            )
        if "method" in cond and cond.get("method") != "GET":
            errors.append(f"{where}: api_check 仅允许 GET")
        op = cond.get("op", "non_empty")
        if op not in _API_CHECK_OPS:
            errors.append(f"{where}: api_check.op {op!r} 不受支持")
        if "json_path" in cond and (not isinstance(cond["json_path"], str) or not cond["json_path"]):
            errors.append(f"{where}: api_check.json_path 必须是非空点路径")
    elif ctype == "panel_visited":
        panel = cond.get("panel")
        if not isinstance(panel, str) or not panel.startswith("/"):
            errors.append(f"{where}: panel_visited.panel 必须是 / 开头的路径")


def _validate_cta(cta: Any, errors: list[str], where: str) -> None:
    if not isinstance(cta, dict) or not isinstance(cta.get("label"), str) or not cta.get("label"):
        errors.append(f"{where}: cta.label 必须是非空字符串")
        return
    action = cta.get("action")
    if not isinstance(action, dict) or action.get("type") not in _CTA_ACTION_TYPES:
        errors.append(f"{where}: cta.action.type 必须是 {sorted(_CTA_ACTION_TYPES)} 之一")
        return
    atype = action["type"]
    target = action.get("target")
    if atype == "open_panel" and (not isinstance(target, str) or not target.startswith("/")):
        errors.append(f"{where}: open_panel.target 必须是 / 开头的路径")
    if atype == "external_url" and (
        not isinstance(target, str) or not target.startswith("https://")
    ):
        errors.append(f"{where}: external_url.target 必须是 https:// 外链")
    if atype == "switch_mode" and (not isinstance(target, str) or not target):
        errors.append(f"{where}: switch_mode.target 必须是非空模式键")


def validate_walkthrough(data: Any) -> list[str]:
    """校验单个 walkthrough dict，返回全部错误行（空列表 = 合法）。"""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["walkthrough 顶层必须是对象"]
    _require_str(data, "id", errors, "walkthrough")
    _require_str(data, "title", errors, "walkthrough")
    if not isinstance(data.get("order"), int):
        errors.append("walkthrough: order 必须是整数")
    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("walkthrough: steps 必须是非空数组")
        return errors
    seen_ids: set[str] = set()
    for i, step in enumerate(steps):
        where = f"steps[{i}]"
        if not isinstance(step, dict):
            errors.append(f"{where}: step 必须是对象")
            continue
        step_id = step.get("id")
        if not isinstance(step_id, str) or not step_id:
            errors.append(f"{where}: id 必须是非空字符串")
        elif step_id in seen_ids:
            errors.append(f"{where}: 步骤 id {step_id!r} 重复")
        seen_ids.add(str(step_id))
        _require_str(step, "title", errors, where)
        _require_str(step, "body", errors, where)
        wizard = step.get("wizard")
        if wizard is not None and wizard not in WIZARD_REGISTRY:
            errors.append(f"{where}: 未知 wizard 键 {wizard!r}（注册表 {sorted(WIZARD_REGISTRY)}）")
        if "cta" in step:
            _validate_cta(step.get("cta"), errors, where)
        if "completion" in step:
            _validate_completion(step["completion"], errors, where)
    return errors


def load_walkthroughs(
    content_dir: Path,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """装载并校验目录下全部 *.json。

    Returns:
        (walkthroughs, errors)：walkthroughs 按 order 升序；errors 非空时
        walkthroughs 为空（fail-closed：任一文件非法不部分放行）。
        default_open 全局唯一约束在此执行（跨文件）。
    """
    walkthroughs: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for path in sorted(content_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"file": path.name, "error": f"解析失败: {exc}"})
            continue
        file_errors = validate_walkthrough(data)
        errors.extend({"file": path.name, "error": e} for e in file_errors)
        if not file_errors:
            walkthroughs.append(data)
    default_open_ids = [w["id"] for w in walkthroughs if w.get("default_open")]
    if len(default_open_ids) > 1:
        errors.append({
            "file": "*",
            "error": f"default_open 只允许一个 walkthrough，当前 {default_open_ids}",
        })
    if errors:
        return [], errors
    walkthroughs.sort(key=lambda w: int(w.get("order", 0)))
    return walkthroughs, []
