"""通用多模态结果归一通道（SDK 级，所有产图工具唯一入口）。

MCP ``tools/call`` 结果 → AgentOS ``ToolExecutionResult`` 的归一转换：

- ``isError`` → 失败结果（UPSTREAM_TOOL_ERROR，content 错误文本透出）
- text content → ``data.snapshot_text``（多段 ``\\n`` 连接）
- image content（MCP 多模态字段 ``{"type":"image","data":base64,"mimeType"}``）
  → 落盘 workspace（``data.file_path``/``image_mime``，前端 render:image 卡）
  + ``metadata.multimodal_content``（tool_core ``inject_multimodal`` 回传模型
  的通用多模态通道；SDK slim 序列化会从 LLM 文本上下文剔除该键，防 base64
  污染）

浏览器截屏 / 桌面截屏 / 图片生成等一切产图工具一律经本模块回图，不在各自
插件手搓转换——MCP 标准多模态字段进来，系统内多模态通道出去，转换只写一次。
"""

from __future__ import annotations

import base64
import os
import time
from typing import Any

from .results import ToolExecutionResult
from .tool_types import create_failure_result, create_success_result

# 截图/图片大小上限（base64 解码后）：超限拒收（防巨图撑爆上下文）
MAX_IMAGE_BYTES = 8 * 1024 * 1024

_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def mcp_content_parts(mcp_result: dict[str, Any]) -> tuple[list[str], str, str]:
    """MCP content 数组 → ``(texts, image_b64, image_mime)``。

    非 dict 项跳过（上游实现瑕疵兜底）；resource content 取其 text 并入 texts。
    """
    texts: list[str] = []
    image_b64 = ""
    image_mime = ""
    for item in mcp_result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image":
            image_b64 = str(item.get("data") or "")
            image_mime = str(item.get("mimeType") or "image/png")
        elif item.get("type") == "text":
            texts.append(str(item.get("text") or ""))
        elif item.get("type") == "resource":
            texts.append(str((item.get("resource") or {}).get("text") or ""))
    return texts, image_b64, image_mime


def error_text(mcp_result: dict[str, Any]) -> str:
    """isError 结果的 content 错误文本（空回退占位）。"""
    texts, _, _ = mcp_content_parts(mcp_result)
    return "\n".join(texts).strip() or "上游工具执行失败"


def save_image_with_multimodal(
    raw: bytes, mime: str, workspace: str, stem: str = "image"
) -> tuple[str, list[dict[str, Any]]]:
    """图片字节 → ``(落盘路径, multimodal_content 块)``。

    落盘任务 workspace（缺省回退 cwd），文件名 ``{stem}-{毫秒时间戳}.{ext}``。
    """
    root = workspace or os.getcwd()
    ext = "png"
    if "jpeg" in mime or "jpg" in mime:
        ext = "jpg"
    path = os.path.join(root, f"{stem}-{int(time.time() * 1000)}.{ext}")
    parent = os.path.dirname(path) or root
    os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(raw)
    blocks = [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}}
    ]
    return path, blocks


def multimodal_content_from_file(file_path: str) -> list[dict[str, Any]] | None:
    """本地图片文件 → multimodal_content 块；文件缺失/读取失败返回 None。

    媒体生成类工具（文件已在 Provider 侧产出）用这个入口进同一条通道。
    """
    if not file_path or not os.path.isfile(file_path):
        return None
    try:
        with open(file_path, "rb") as f:
            raw = f.read()
    except OSError:
        return None
    mime = _MIME_BY_EXT.get(os.path.splitext(file_path)[1].lower(), "image/png")
    return [{"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}}]


def normalize_mcp_result(
    mcp_result: dict[str, Any],
    *,
    tool_name: str,
    workspace: str = "",
    stem: str = "image",
    extra_data: dict[str, Any] | None = None,
) -> ToolExecutionResult:
    """MCP ``tools/call`` 结果 → AgentOS 工具出参（通用归一，见模块 docstring）。"""
    if mcp_result.get("isError"):
        return create_failure_result(error=error_text(mcp_result), error_code="UPSTREAM_TOOL_ERROR")

    texts, image_b64, image_mime = mcp_content_parts(mcp_result)
    out: dict[str, Any] = {"status": 200, "tool": tool_name}
    if texts:
        out["snapshot_text"] = "\n".join(texts)
    if extra_data:
        out.update(extra_data)

    metadata: dict[str, Any] = {"action": tool_name}
    if image_b64:
        try:
            raw = base64.b64decode(image_b64, validate=False)
        except (ValueError, TypeError) as e:
            return create_failure_result(error=f"截图 base64 解码失败: {e}", error_code="IMAGE_DECODE_FAILED")
        if len(raw) > MAX_IMAGE_BYTES:
            return create_failure_result(
                error=f"截图超限: {len(raw)} > {MAX_IMAGE_BYTES} bytes", error_code="IMAGE_TOO_LARGE"
            )
        path, blocks = save_image_with_multimodal(raw, image_mime, workspace, stem=stem)
        out["file_path"] = path
        out["image_mime"] = image_mime
        metadata["multimodal_content"] = blocks

    return create_success_result(data=out, metadata=metadata)
