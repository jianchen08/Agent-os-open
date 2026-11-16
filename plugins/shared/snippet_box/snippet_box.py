"""snippet_box 片段速查库工具实现。

本文件 schema 常量与 plugin.json capabilities.tools[0] 声明逐字对应
（G2 声明↔实现严格比对，漂移即剔除）。

数据面：插件目录 data/snippets.json（每次调用读写快照，无第三方依赖）。
统一信封：{ok, snippets, message}；参数条件必填（add→content、delete→snippet_id、
search→title 作查询词）在实现层校验，schema 层 required 只锁 action。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

TOOL_NAME = "snippet_box"

SNIPPET_BOX_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["action"],
    "properties": {
        "action": {
            "type": "string",
            "enum": ["add", "search", "list", "delete"],
            "description": "操作：add 新增 / search 检索 / list 全列 / delete 删除",
        },
        "title": {
            "type": "string",
            "description": "片段标题；search 时作为查询词（对 title/tags/content 做子串匹配）",
        },
        "tags": {
            "description": "标签：字符串数组或逗号分隔字符串（内部统一为数组）",
            "type": ["array", "string"],
            "items": {"type": "string"},
        },
        "content": {"type": "string", "description": "片段正文（add 时必填）"},
        "snippet_id": {"type": "string", "description": "片段 ID（delete 时必填，取 add 返回的 id）"},
    },
}

SNIPPET_BOX_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["ok", "snippets", "message"],
    "properties": {
        "ok": {"type": "boolean", "description": "本次操作是否成功"},
        "snippets": {
            "type": "array",
            "description": "受影响/匹配的片段列表",
            "items": {
                "type": "object",
                "required": ["id", "title", "tags", "content", "created_at"],
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "content": {"type": "string"},
                    "created_at": {"type": "number", "description": "unix 时间戳（秒）"},
                },
            },
        },
        "message": {"type": "string", "description": "结果说明（失败时含原因）"},
    },
}

SNIPPET_BOX_RENDER = {"card": "form", "title": "SnippetBox"}

SNIPPET_BOX_DESCRIPTION = "片段速查库：增删查代码/命令片段（add/search/list/delete），数据持久化在插件目录"

DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "snippets.json")


def _load_all(path: str) -> list[dict[str, Any]]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    if not isinstance(data, list):
        return []
    return data


def _save_all(snippets: list[dict[str, Any]], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snippets, fh, ensure_ascii=False, indent=2)


def _norm_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _fail(message: str) -> dict[str, Any]:
    return {"ok": False, "snippets": [], "message": message}


def snippet_box(
    action: str = "",
    title: str = "",
    tags: Any = None,
    content: str = "",
    snippet_id: str = "",
) -> dict[str, Any]:
    """片段速查库：按 action 增删查片段，读写 DATA_PATH 快照。"""
    if action == "add":
        if not str(content).strip():
            return _fail("add 需要 content（片段正文）")
        snippets = _load_all(DATA_PATH)
        snippet = {
            "id": uuid.uuid4().hex[:12],
            "title": str(title or ""),
            "tags": _norm_tags(tags),
            "content": str(content),
            "created_at": time.time(),
        }
        snippets.append(snippet)
        _save_all(snippets, DATA_PATH)
        return {"ok": True, "snippets": [snippet], "message": f"已添加片段 {snippet['id']}"}

    if action == "search":
        query = str(title or "").strip()
        if not query:
            return _fail("search 需要 title 作为查询词（对 title/tags/content 子串匹配）")
        needle = query.casefold()
        hits = [
            s
            for s in _load_all(DATA_PATH)
            if needle in str(s.get("title", "")).casefold()
            or needle in str(s.get("content", "")).casefold()
            or any(needle in str(t).casefold() for t in s.get("tags", []))
        ]
        return {"ok": True, "snippets": hits, "message": f"匹配 {len(hits)} 条"}

    if action == "list":
        snippets = _load_all(DATA_PATH)
        return {"ok": True, "snippets": snippets, "message": f"共 {len(snippets)} 条"}

    if action == "delete":
        if not str(snippet_id).strip():
            return _fail("delete 需要 snippet_id")
        snippets = _load_all(DATA_PATH)
        kept = [s for s in snippets if s.get("id") != snippet_id]
        if len(kept) == len(snippets):
            return _fail(f"片段不存在: {snippet_id}")
        _save_all(kept, DATA_PATH)
        return {"ok": True, "snippets": [], "message": f"已删除 {snippet_id}"}

    return _fail(f"未知 action: {action or '(空)'}（支持 add/search/list/delete）")
