"""任务 id 短形式工具（LLM 工具面短 id ↔ 内部全 id 解析）。

给大模型看的 id 不宜过长——32 位 hex 的 pipeline_id 抄写/引用/记忆都容易错。
规则：
- **内部权威 id 不动**：state 键（task.id / task.owned.<id>）、前端 API、
  内核契约全部保持全 id；
- **LLM 工具面短化**：task_submit / task_manage / task_evaluate 的返回
  （task_id / pipeline_id / 列表 / 文本引用）用 `short_id()`（前 12 位）；
- **入参前缀解析**：LLM 回传的短 id 经 `resolve_id()` 在 state 聚合行里
  前缀唯一匹配解析回全 id（精确命中优先；多命中报歧义；无命中原样返回
  让既有"任务不存在"路径处理）。
"""

from __future__ import annotations

from typing import Any

# 短 id 长度（48 bit 熵，够唯一且短）
SHORT_ID_LEN = 12


def short_id(full_id: str) -> str:
    """全 id → 短 id（前 12 位；短 id 原样返回）。"""
    if not full_id:
        return full_id
    return full_id[:SHORT_ID_LEN]


async def resolve_id(rows: list[dict[str, Any]] | None, candidate: str) -> str:
    """LLM 入参 id → 内部全 id（前缀唯一解析）。

    规则：
    1. 候选非 str/空 → 原样返回（让既有校验报错）；
    2. 候选是 12 位短 id → 在聚合行里前缀匹配（pipeline_id + task.owned.<id>）；
       唯一命中返回全 id，多命中返回 `AMBIGUOUS:<ids>`（调用方报歧义），
       无命中原样返回（既有"任务不存在"路径处理）；
    3. 候选已是全 id（精确命中）→ 原样返回。
    """
    if not candidate:
        return candidate
    # 精确命中：聚合里已有该全 id → 原样
    for row in (rows or []):
        if str(row.get("pipeline_id") or "") == candidate:
            return candidate
        if any(str(k).startswith(f"task.owned.{candidate}.") for k in row.keys()):
            return candidate
    # 短 id 前缀匹配
    if len(candidate) <= SHORT_ID_LEN:
        hits: list[str] = []
        for row in (rows or []):
            pid = str(row.get("pipeline_id") or "")
            if pid.startswith(candidate):
                hits.append(pid)
            for k in row.keys():
                ks = str(k)
                if ks.startswith("task.owned."):
                    owned_id = ks[len("task.owned."):].split(".", 1)[0]
                    if owned_id.startswith(candidate) and owned_id not in hits:
                        hits.append(owned_id)
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return f"AMBIGUOUS:{candidate}"
    return candidate
