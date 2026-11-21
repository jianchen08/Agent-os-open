"""MiniMax/思考标签解析家族——自 adapter.py 拆出（2500 行闸，2026-11-08）。

复制锚点：源文件 ``plugins/shared/llm/adapter_minimax.py`` @
c0fe6922fc8cf68cc0c46b6ff1e11c0db021056a（批 2 收口基线）；复制面 =
``_extract_thinking_from_content`` / ``_parse_bare_json_tool_call`` /
``parse_minimax_native_tool_calls`` 及三个标签正则，逐行同源。改造：模块名
``llm_minimax``（同进程裸名隔离）；``uuid`` 生成的调用 id 契约不变。

拆分边界：`<think/>` 标签提取、容器内裸 JSON 工具调用解析、MiniMax 原生
工具调用文本恢复。纯函数无状态，源侧 adapter.py 重导出保兼容（历史导入面
`from adapter import parse_minimax_native_tool_calls` 不变）。
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any

_THINK_PATTERN = re.compile(
    r"<think[^>]*>(.*?)</think[^>]*>",
    re.DOTALL,
)
_THINK_PATTERN_NO_GT = re.compile(
    r"<think\s(.*?)</think[^>]*>",
    re.DOTALL,
)


def _extract_thinking_from_content(content: str | None) -> tuple[str | None, str | None]:
    """从 content 中提取 <think/> 标签内容，返回 (thinking_text, cleaned_content)。

    MiniMax-M2.7 等推理模型把思考内容包裹在 <think/> 标签中混在 content 字段返回，
    上游把思考内容混在 content 返回时不映射 reasoning_content，因此这里手动解析 <think/> 标签，
    将思考内容与正文分离。

    支持两种标签格式：
    1. 标准 XML: <think\\n...\\n</think/> 或 <think type="x">...</think...>
    2. MiniMax: <think\\n...\\n</think/> (开始标签无 >)

    Args:
        content: LLM 返回的原始 content 文本

    Returns:
        (thinking_text, cleaned_content) 元组
    """
    if not content:
        return None, content

    pattern, matches = _THINK_PATTERN, _THINK_PATTERN.findall(content)
    if not matches:
        pattern, matches = _THINK_PATTERN_NO_GT, _THINK_PATTERN_NO_GT.findall(content)
    if not matches:
        return None, content

    thinking = "\n".join(m.strip() for m in matches if m.strip())
    cleaned = pattern.sub("", content).strip()
    return thinking if thinking else None, cleaned if cleaned else None


# ── MiniMax 原生工具调用正文兜底解析（BUG-17）────────────────────────────
# MiniMax-M2/M3 系以特殊标记承载工具调用（<minimax:tool_call> 容器 + invoke/
# parameter 子标签）。上游未激活 tool-call 解析时（请求未携带 tools 声明等），
# 调用意图以正文到达，标记还会呈转义倾倒形态 ]<]minimax[>[<invoke ...>
# （生产 2026-09-15 实测字节）。此处把正文中的调用块解析回结构化 tool_calls
# 并从正文剥离——否则调用意图泄漏为 UI 正文且工具永不执行。
# 标签前缀三种到达形态：纯净 <tag>、命名空间 <minimax:tag>、转义倾倒。
_TAG_OPEN = r"(?:<(?:minimax:)?|\]<\]minimax\[>\[<)"
_TAG_CLOSE = r"(?:</(?:minimax:)?|\]<\]minimax\[>\[</)"
# 调用块：容器非贪婪捕获到首个闭合（截断流无闭合时容忍到文本末尾）。
_MINIMAX_BLOCK_RE = re.compile(
    _TAG_OPEN + r"tool_call\s*>(.*?)(?:" + _TAG_CLOSE + r"tool_call\s*>|\Z)",
    re.DOTALL,
)
# invoke：闭合以 lookahead 终结（容忍缺闭合，截到文本末尾）。
_MINIMAX_INVOKE_RE = re.compile(
    _TAG_OPEN + r'invoke\s+name="([^"]+)"\s*>(.*?)(?=' + _TAG_CLOSE + r"invoke\s*>|\Z)",
    re.DOTALL,
)
# parameter：终结点 = 闭合标签 / 下一个 parameter 开标签 / invoke 闭合——
# 模型漏写 parameter 闭合时（实测有），截到下一边界保参不丢（值可能带模型
# 自己写错的残余闭标签，原样交付不做猜测性清洗）。
_MINIMAX_PARAM_RE = re.compile(
    _TAG_OPEN + r'parameter\s+name="([^"]+)"\s*>(.*?)'
    r"(?=" + _TAG_CLOSE + r"parameter\s*>|" + _TAG_OPEN + r"parameter\s+name=|"
    + _TAG_CLOSE + r"invoke\s*>|\Z)",
    re.DOTALL,
)


def _parse_bare_json_tool_call(body: str) -> dict[str, Any] | None:
    """容器内裸 JSON 形态解析（生产 2026-09-15 16:52 实证）。

    形态：``{"name": <工具名>, "arguments": <对象|JSON 字符串>}``。arguments
    为字符串时原样透传（契约的 arguments 本就是 JSON 字符串；非 JSON 的坏
    字符串交由下游 tool 层可见失败，不在此猜测修复）；其余类型 json.dumps。
    body 非 JSON 对象或缺 name → None（不识别，交由调用方保留原文）。
    """
    try:
        obj = json.loads(body.strip())
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_args = obj.get("arguments", {})
    args_out = raw_args if isinstance(raw_args, str) else json.dumps(raw_args, ensure_ascii=False)
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "name": name,
        "arguments": args_out,
    }


def parse_minimax_native_tool_calls(text: str) -> tuple[list[dict[str, Any]], str]:
    """解析正文中的 MiniMax 原生工具调用块。

    容器体内两形态：invoke/parameter 子标签（官方模板）或裸 JSON 对象
    （name + arguments，arguments 可为对象或 JSON 字符串——生产 16:52 实证）。

    Args:
        text: LLM 返回的原始正文

    Returns:
        (tool_calls, cleaned_text)。tool_calls 与 adapter 流式/非流式解析
        契约同构（id=call_<hex>、name、arguments=JSON 字符串，参数序保持）；
        无可识别调用块时返回 ([], 原文)——不误吞无法解读的正文。
        cleaned_text 仅去空白不做猜测性改写。
    """
    blocks = list(_MINIMAX_BLOCK_RE.finditer(text))
    if not blocks:
        return [], text

    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        body = block.group(1)
        invokes = list(_MINIMAX_INVOKE_RE.finditer(body))
        if invokes:
            for inv in invokes:
                params: dict[str, str] = {}
                for prm in _MINIMAX_PARAM_RE.finditer(inv.group(2)):
                    params[prm.group(1)] = prm.group(2).strip()
                tool_calls.append(
                    {
                        "id": f"call_{uuid.uuid4().hex[:24]}",
                        "name": inv.group(1),
                        "arguments": json.dumps(params, ensure_ascii=False),
                    }
                )
        else:
            bare = _parse_bare_json_tool_call(body)
            if bare is not None:
                tool_calls.append(bare)

    if not tool_calls:
        return [], text
    cleaned = _MINIMAX_BLOCK_RE.sub("", text).strip()
    return tool_calls, cleaned


