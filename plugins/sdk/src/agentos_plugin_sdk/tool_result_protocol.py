"""工具结果协议——固定函数路径（ADR 2026-09-28 工具拦截结果预填机制）。

所有构造工具结果结构的代码一律经本模块，禁止散建：

- :func:`tool_result_entry`：规范 ToolResult entry 形状。guards 写
  ``pre_decided_results``、任何插件伪造工具结果，一律经它构造；
  协议扩展位（如 retry_allowed/arguments，BUG-41）经 ``**extra`` 顶层透传。
- :func:`build_tool_result_ops`：配对 role=tool 消息的增量 ops
  （``{"_ops": [{"op": "set", "msg": ...}]}``，set 无 seq = 引擎 append），
  产物形状 = tool_core ``messages::rebuild`` 追加段（含 tool_result envelope）。
- :func:`serialize_for_content`：结果数据 → content 文本（YAML）。

Rust（tool_core，形状定义者）/ Python（本模块）双实现的单一真值 =
契约夹具 ``tests/contracts/tool_result_messages.fixture.json``：本模块
pytest 与 tool_core cargo 单测双车道消费同一份向量，两边必须同绿——
漂移即红；删值实验 = 故意改坏一条向量，两车道必须同时红。

先例：本模块与 ``tool_result_cache``（"SDK 单一真值源"）同为跨插件
共享的纯数据协议件，不走 capability 服务（纯数据变换服务化零收益，
ADR 被否案）。
"""

from __future__ import annotations

from typing import Any

import yaml  # type: ignore[import-untyped]  # 第三方 stubs 缺失


def tool_result_entry(
    tool_name: str,
    *,
    call_id: str | None = None,
    success: bool = True,
    error: str | None = None,
    data: Any = None,
    metadata: dict[str, Any] | None = None,
    duration_ms: float = 0.0,
    **extra: Any,
) -> dict[str, Any]:
    """构造规范 ToolResult entry。

    Args:
        tool_name: 工具名（与被拒/被伪调用一致，供归因与整形）。
        call_id: 工具调用 ID（pre_decided_results 匹配键；无 id 的调用不预填）。
        success: 是否成功（guards 预填拒绝恒 False）。
        error: 失败原因（成功时 None）。
        data: 结果数据。
        metadata: 元数据（``decided_by`` 等 guard 归因）。
        duration_ms: 耗时（预定结果恒 0.0）。
        **extra: 协议扩展位顶层透传（如 retry_allowed/arguments，由下游
            消费方按键认读；不进 envelope，envelope 七键封闭）。

    Returns:
        entry dict（call_id 存在时含之；键序对齐 tool_core envelope）。
    """
    entry: dict[str, Any] = {}
    if call_id is not None:
        entry["call_id"] = call_id
    entry.update(
        {
            "tool_name": tool_name,
            "success": success,
            "error": error,
            "data": data,
            "metadata": metadata,
            "duration_ms": duration_ms,
        }
    )
    entry.update(extra)
    return entry


def serialize_for_content(data: Any) -> str:
    """结果数据 → 消息 content 文本（YAML，语义对齐 tool_core Rust 侧）。

    - None → 空串（避免 "null" 进上下文）；
    - 其余 → YAML 块风格、键排序（对齐 serde_json BTreeMap 键序）；
      pyyaml 对纯标量附加的文档结束标记 ``...`` 剥离，对齐 serde_yaml
      （其标量输出不带文档结束标记）；
    - YAML 无法表示的值（自定义对象等，缓存可存任意 Python 对象）回落
      ``str(data)``——读面不因数据形态崩（对齐旧 json.dumps default=str
      的兜底语义）。
    """
    if data is None:
        return ""
    try:
        dumped = str(yaml.safe_dump(data, default_flow_style=False, allow_unicode=True, sort_keys=True))
    except yaml.YAMLError:
        return str(data)
    if dumped.endswith("...\n"):
        dumped = dumped[: -len("...\n")]
    return dumped


def merge_pre_decided(
    existing: list[dict[str, Any]] | None,
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """合并预定结果（多 guard 组合写同一键的唯一正确姿势）。

    prepare 链多个拦截方（tool_schema_validator / isolation_guard /
    level_guard / security_check）先后写 ``pre_decided_results``：各自读
    state 现值经本函数合并后整体写回。去重键：有 call_id 按 call_id（同
    call_id 后写赢——链上更靠后的 guard 判定覆盖），无 call_id 按
    ``__name__<tool_name>``（名字级兜底：tool_core 对无 id 的调用按
    工具名命中，保住旧按名执法对幻觉调用的拦截面）。

    Args:
        existing: state 现值（无键/None 视为空）。
        entries: 本次新增条目（:func:`tool_result_entry` 构造；被拦调用
            有 id 带 id、无 id 亦可入——走名字兜底）。

    Returns:
        合并后的完整列表（整体写回 ``pre_decided_results``）。
    """

    def dedup_key(e: dict[str, Any]) -> str:
        cid = e.get("call_id")
        if cid is not None:
            return str(cid)
        return f"__name__{e.get('tool_name', '')}"

    by_key: dict[str, dict[str, Any]] = {}
    for e in existing or []:
        by_key[dedup_key(e)] = e
    for e in entries:
        by_key[dedup_key(e)] = e
    return list(by_key.values())


def build_tool_result_ops(
    tool_calls: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """构造配对 role=tool 消息的增量 ops。

    Args:
        tool_calls: 本轮 raw_tool_calls（含 id）。
        results: 与 tool_calls 按下标一一对应的 entry 列表
            （:func:`tool_result_entry` 产物或同形 dict；不接受裸值，
            裸值先经 tool_result_entry 包装）。长度必须一致，不一致
            = 调用方配对错误，显式报错（不静默截断）。

    Returns:
        ``{"_ops": [{"op": "set", "msg": {role/tool_call_id/content/
        tool_result}}]}``——set 无 seq = 引擎 append；tool_result envelope
        七键（call_id、tool_name、success、error、data、metadata、duration_ms），
        形状 = tool_core ``messages::rebuild`` 追加段（契约夹具锚定）。
    """
    if len(tool_calls) != len(results):
        raise ValueError(
            f"tool_calls 与 results 长度不一致（{len(tool_calls)} != {len(results)}）"
            "——配对是按下标的调用方契约，不静默截断"
        )
    ops: list[dict[str, Any]] = []
    for i, (tc, entry) in enumerate(zip(tool_calls, results, strict=True)):
        call_id = entry.get("call_id") or tc.get("id") or f"call_{i}"
        success = bool(entry.get("success", True))
        error = entry.get("error")
        content = serialize_for_content(entry.get("data")) if success else f"Error: {error or 'unknown'}"
        envelope = {
            "call_id": call_id,
            "tool_name": entry.get("tool_name") or tc.get("name", ""),
            "success": success,
            "error": error,
            "data": entry.get("data"),
            "metadata": entry.get("metadata"),
            "duration_ms": entry.get("duration_ms", 0.0),
        }
        ops.append(
            {
                "op": "set",
                "msg": {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": content,
                    "tool_result": envelope,
                },
            }
        )
    return {"_ops": ops}
