# -*- coding: utf-8 -*-
"""提示音目录存储：扫描 sounds/*.json 合成预设，校验、排序与默认项检测。

预设文件即内容契约（id/label/params 必填，params 至少含 partials+notes；
可选 default 标记默认音效）：用户或 agent 落一个 JSON 文件即上目录，
单个坏文件跳过不拖垮整个目录面。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REQUIRED_TOP = {"id", "label", "params"}
_REQUIRED_PARAMS = {"partials", "notes"}
_REQUIRED_NOTE = {"freq", "start", "peak", "decay"}
_NUMERIC_NOTE_OPTIONAL = {"glideTo", "glideTime", "vibratoHz", "vibratoDepth"}


def _valid_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_preset(doc: Any) -> bool:
    """结构校验：必填字段齐全、数值字段均为数字。坏预设由调用方跳过。"""
    if not isinstance(doc, dict) or not _REQUIRED_TOP.issubset(doc):
        return False
    if not isinstance(doc["id"], str) or not doc["id"]:
        return False
    if not isinstance(doc["label"], str) or not doc["label"]:
        return False
    params = doc["params"]
    if not isinstance(params, dict) or not _REQUIRED_PARAMS.issubset(params):
        return False
    partials = params["partials"]
    if not isinstance(partials, list) or not partials:
        return False
    for p in partials:
        if not isinstance(p, dict) or not {"ratio", "gain", "decayRatio"}.issubset(p):
            return False
        if not all(_valid_number(p[k]) for k in ("ratio", "gain", "decayRatio")):
            return False
    notes = params["notes"]
    if not isinstance(notes, list) or not notes:
        return False
    for n in notes:
        if not isinstance(n, dict) or not _REQUIRED_NOTE.issubset(n):
            return False
        if not all(_valid_number(n[k]) for k in _REQUIRED_NOTE):
            return False
        if not all(
            n.get(k) is None or _valid_number(n[k]) for k in _NUMERIC_NOTE_OPTIONAL
        ):
            return False
    for k in ("attack", "wet"):
        if params.get(k) is not None and not _valid_number(params[k]):
            return False
    taps = params.get("taps")
    if taps is not None:
        if not isinstance(taps, list):
            return False
        for t in taps:
            if not isinstance(t, dict) or not {"delay", "feedback", "lowpass"}.issubset(t):
                return False
            if not all(_valid_number(t[k]) for k in ("delay", "feedback", "lowpass")):
                return False
    return True


def load_sounds(sounds_dir: Path) -> tuple[list[dict], str]:
    """扫描目录加载预设：返回 (预设列表按 id 升序, 默认预设 id)。

    default:true 的首个生效；无标记时取 id 升序首个；空目录返回 ([], "")。
    """
    presets: list[dict] = []
    if sounds_dir.is_dir():
        for path in sorted(sounds_dir.glob("*.json")):
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                logger.warning("[notification_sounds] 预设 %s 解析失败，跳过: %s", path.name, e)
                continue
            if _valid_preset(doc):
                presets.append(doc)
            else:
                logger.warning("[notification_sounds] 预设 %s 结构不合法，跳过", path.name)
    presets.sort(key=lambda d: str(d["id"]))
    default_id = ""
    for doc in presets:
        if doc.get("default") is True:
            default_id = str(doc["id"])
            break
    if not default_id and presets:
        default_id = str(presets[0]["id"])
    return presets, default_id
