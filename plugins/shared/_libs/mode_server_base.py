"""模式插件 server.py 共享基面：profile 服务 + 面板页响应（审查总册 AR-M2）。

六件模式插件（mode_coding / mode_evolution / mode_godot / mode_research /
mode_roleplay / mode_writing）的 profile 服务面（mode.yaml 读取 +
mode.describe / mode.get_profile 服务注册）与 webview 面板页的
HttpHandleResponse 封装是同构样板——唯一实现收敛在本模块，六件 server.py
经 bootstrap_plugin 引导后导入，各自保留与契约测试耦合的模块级名
（load_profile / mode_describe / mode_get_profile / _PANEL_HTML_PATH /
_panel_html_response）。

可达性契约：bootstrap_plugin 的 shared_root 判定（含 SDK 位置锚回退，
SDK bootstrap 模块）使本模块对仓内布局与用户空间播种副本均可达；profile
与服务声明仍随各包自包含（mode.yaml / plugin.json 在包内），本模块只承载
无状态实现面。
"""
from __future__ import annotations

import base64
from typing import Any

import yaml

__all__ = [
    "DESCRIBE_FIELDS",
    "load_mode_profile",
    "register_profile_services",
    "html_file_response",
    "html_text_response",
]

# mode.describe 透出的元数据字段（profile 键白名单，六模式同构）
DESCRIBE_FIELDS: tuple[str, ...] = (
    "mode",
    "name",
    "chain",
    "weights",
    "budget",
    "panel_page_id",
)

_DESCRIBE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "mode",
        "name",
        "chain",
        "weights",
        "budget",
        "profile_path",
        "panel_page_id",
    ],
    "properties": {
        "mode": {"type": "string"},
        "name": {"type": "string"},
        "chain": {"type": "object"},
        "weights": {"type": "object"},
        "budget": {"type": "object"},
        "profile_path": {"type": "string"},
        "panel_page_id": {"type": "string"},
    },
}

_GET_PROFILE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["mode"],
    "properties": {"mode": {"type": "string"}},
}


def load_mode_profile(path: str) -> dict[str, Any]:
    """读取包内 mode.yaml；空文件或缺 mode 键视为种子损坏（fail-closed）。"""
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or not data.get("mode"):
        raise ValueError(f"profile 种子损坏（缺 mode 键）: {path}")
    return data


def register_profile_services(
    plugin: Any,
    profile_path: str,
    describe_description: str,
    get_profile_description: str,
) -> tuple[Any, Any]:
    """注册 mode.describe / mode.get_profile 两服务（manifest
    capabilities.services 声明面的服务端实现，wire = MCP tools/call）。

    返回两 handler 供 server 模块级别名（契约测试直调缝，与注册同一实现）。
    """
    async def mode_describe() -> dict[str, Any]:
        profile = load_mode_profile(profile_path)
        describe = {field: profile[field] for field in DESCRIBE_FIELDS}
        describe["profile_path"] = profile_path
        return describe

    async def mode_get_profile() -> dict[str, Any]:
        return load_mode_profile(profile_path)

    plugin.tool(
        name="mode.describe",
        schema={"type": "object", "properties": {}},
        description=describe_description,
        output_schema=_DESCRIBE_OUTPUT_SCHEMA,
    )(mode_describe)
    plugin.tool(
        name="mode.get_profile",
        schema={"type": "object", "properties": {}},
        description=get_profile_description,
        output_schema=_GET_PROFILE_OUTPUT_SCHEMA,
    )(mode_get_profile)
    return mode_describe, mode_get_profile


def html_text_response(html: str) -> dict[str, Any]:
    """HTML 文本 → HttpHandleResponse（text/html，body base64，内核约定）。"""
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": base64.b64encode(html.encode("utf-8")).decode("ascii"),
        "body_encoding": "base64",
    }


def html_file_response(path: str) -> dict[str, Any]:
    """包内 HTML 文件 → HttpHandleResponse（text/html，body base64）。

    path 由调用方每次传入（server 模块级 `_PANEL_HTML_PATH` 在调用时读取，
    测试经 monkeypatch 换路径断言 500 边界）。
    """
    with open(path, encoding="utf-8") as fh:
        return html_text_response(fh.read())
