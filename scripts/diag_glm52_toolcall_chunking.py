"""诊断 GLM-5.2 工具调用：是整块一次性输出，还是分 chunk 增量输出。

用【同一个 prompt + 同一个工具】分别走两条链路，对比 tool_call 的 chunk 分布：

  A. 直连 httpx SSE：直接打 GLM coding endpoint，逐 chunk 解析上游真实分块。
     —— 看到的就是上游服务端「真正」怎么切分 tool_call 的。

  B. 项目客户端 LiteLLMAdapter：走 litellm zai/glm-5.2（项目 adapter 底层
     就是 litellm.acompletion，KeyPoolAdapter._direct_call_with_slot 也殊途同归）。
     —— 看项目实际收到时，litellm 层有没有改变分块。

判定指标（每个 tool_call，按 index）：
  - 跨几个 chunk（出现过 delta.tool_calls 的 chunk 数）
  - id / name 各在第几个 chunk 首次到达
  - arguments 分几段累加：仅 1 段 = 整块一次性；>1 段 = 分 chunk 增量

用法：
    python scripts/diag_glm52_toolcall_chunking.py            # 两种方式各跑 1 次
    python scripts/diag_glm52_toolcall_chunking.py --runs 3   # 各跑 3 次取众数
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from typing import Any

# ---------------------------------------------------------------------------
# 固定输入：同一个 prompt + 同一个多参数工具，确保两次测试公平
# ---------------------------------------------------------------------------

API_BASE = "https://open.bigmodel.cn/api/coding/paas/v4/"
API_KEY = os.environ["ZHIPU_API_KEY"]
MODEL = "glm-5.2"

MESSAGES: list[dict[str, Any]] = [
    {
        "role": "user",
        "content": (
            "帮我查一下北京地区评分最高的川菜馆，要求人均消费不超过150元（价位档3以内），"
            "结果按评分从高到低排序，最多返回3条。请直接调用 search_restaurants 工具完成查询。"
        ),
    },
]

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_restaurants",
            "description": "搜索指定城市的餐厅，支持按菜系、价位、排序方式筛选。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "城市名"},
                    "cuisine": {"type": "string", "description": "菜系，如 川菜/粤菜/日料"},
                    "price_level": {"type": "integer", "description": "价位档 1-5"},
                    "sort_by": {"type": "string", "enum": ["rating", "distance", "price"]},
                    "max_results": {"type": "integer", "description": "返回条数上限"},
                },
                "required": ["city"],
            },
        },
    },
]

MAX_TOKENS = 8192


# ---------------------------------------------------------------------------
# Trace 收集结构（两种链路共用）
# ---------------------------------------------------------------------------

def new_trace() -> dict[str, Any]:
    return {
        "total_chunks": 0,
        "reasoning_chunks": 0,
        "content_chunks": 0,
        "finish": None,
        "tc_chunk_nos": [],  # 所有出现过 delta.tool_calls 的 chunk 序号
        "tool_calls": {},    # index -> {id_at, name_at, name, arg_segs:[(no,len)]}
    }


def _record_tc(trace: dict[str, Any], chunk_no: int, idx: int,
               tc_id: str | None, name: str | None, args: str | None) -> None:
    tc = trace["tool_calls"].setdefault(
        idx,
        {"id_at": None, "name_at": None, "name": "", "arg_segs": []},
    )
    if tc_id and tc["id_at"] is None:
        tc["id_at"] = chunk_no
    if name:
        tc["name"] += name
        if tc["name_at"] is None:
            tc["name_at"] = chunk_no
    if args:
        tc["arg_segs"].append((chunk_no, len(args)))


# ---------------------------------------------------------------------------
# A. 直连 httpx SSE
# ---------------------------------------------------------------------------

async def run_direct() -> dict[str, Any]:
    import httpx

    url = API_BASE + "chat/completions"
    body = {
        "model": MODEL,
        "messages": MESSAGES,
        "tools": TOOLS,
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_tokens": MAX_TOKENS,
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    trace = new_trace()
    t0 = time.monotonic()
    timeout = httpx.Timeout(300.0, connect=30.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            print(f"  HTTP status: {resp.status_code}")
            if resp.status_code != 200:
                text = await resp.aread()
                print(f"  错误响应: {text[:300]!r}")
                return trace
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                obj = json.loads(data)
                trace["total_chunks"] += 1
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}) or {}
                fr = choices[0].get("finish_reason")
                if fr:
                    trace["finish"] = fr
                if delta.get("reasoning_content"):
                    trace["reasoning_chunks"] += 1
                if delta.get("content"):
                    trace["content_chunks"] += 1
                tcs = delta.get("tool_calls")
                if tcs:
                    trace["tc_chunk_nos"].append(trace["total_chunks"])
                    for tc in tcs:
                        fn = tc.get("function", {}) or {}
                        _record_tc(
                            trace, trace["total_chunks"],
                            tc.get("index", 0),
                            tc.get("id"),
                            fn.get("name"),
                            fn.get("arguments"),
                        )
    trace["elapsed"] = time.monotonic() - t0
    return trace


# ---------------------------------------------------------------------------
# B. 项目客户端 LiteLLMAdapter（litellm zai/glm-5.2）
# ---------------------------------------------------------------------------

async def run_via_adapter() -> dict[str, Any]:
    import litellm
    from llm.adapter import LiteLLMAdapter  # noqa: PLC0415

    adapter = LiteLLMAdapter()
    trace = new_trace()
    t0 = time.monotonic()

    # 直接拿 litellm 的流式 response，逐 chunk 走，不走 adapter.completion 的 on_chunk
    # 抽象（那个抽象会合并，看不到原始分块）。这里要的是 litellm 原始 chunk。
    response = await litellm.acompletion(
        model=f"zai/{MODEL}",
        messages=MESSAGES,
        tools=TOOLS,
        tool_choice="auto",
        api_base=API_BASE,
        api_key=API_KEY,
        stream=True,
        stream_options={"include_usage": True},
        max_tokens=MAX_TOKENS,
        timeout=300.0,
        drop_params=True,
    )
    # 引用 adapter 仅证明项目链路可初始化（且与生产 _do_completion 同源 litellm.acompletion）
    _ = adapter

    aiter = response.__aiter__()
    try:
        while True:
            try:
                chunk = await aiter.__anext__()
            except StopAsyncIteration:
                break
            trace["total_chunks"] += 1
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            fr = getattr(choice, "finish_reason", None)
            if fr:
                trace["finish"] = fr
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                trace["reasoning_chunks"] += 1
            if getattr(delta, "content", None):
                trace["content_chunks"] += 1
            tcs = getattr(delta, "tool_calls", None)
            if tcs:
                trace["tc_chunk_nos"].append(trace["total_chunks"])
                for tc in tcs:
                    fn = getattr(tc, "function", None)
                    _record_tc(
                        trace, trace["total_chunks"],
                        getattr(tc, "index", 0),
                        getattr(tc, "id", None),
                        getattr(fn, "name", None) if fn else None,
                        getattr(fn, "arguments", None) if fn else None,
                    )
    finally:
        if hasattr(response, "aclose"):
            await response.aclose()
    trace["elapsed"] = time.monotonic() - t0
    return trace


# ---------------------------------------------------------------------------
# 汇总与判定
# ---------------------------------------------------------------------------

def summarize(label: str, trace: dict[str, Any]) -> str:
    """生成一次测试的人类可读判定结论。"""
    lines = [f"=== [{label}] 结果 ==="]
    lines.append(
        f"  chunks 总数={trace['total_chunks']} "
        f"reasoning={trace['reasoning_chunks']}c "
        f"content={trace['content_chunks']}c "
        f"finish={trace['finish']} "
        f"耗时={trace.get('elapsed', 0):.1f}s"
    )
    tcs = trace["tool_calls"]
    if not tcs:
        lines.append("  ⚠ 未触发工具调用")
        return "\n".join(lines)

    for idx in sorted(tcs):
        tc = tcs[idx]
        arg_segs = tc["arg_segs"]
        total_arg_chars = sum(n for _, n in arg_segs)
        seg_count = len(arg_segs)
        first_no = trace["tc_chunk_nos"][0] if trace["tc_chunk_nos"] else "?"
        lines.append(f"  tool_call #{idx} ({tc['name'] or '?'}):")
        lines.append(f"    首个 tool_call chunk: #{first_no}")
        lines.append(f"    id 到达 chunk: #{tc['id_at']}")
        lines.append(f"    name 到达 chunk: #{tc['name_at']} → {tc['name']!r}")
        if seg_count <= 1:
            lines.append(
                f"    arguments: 共 {total_arg_chars} 字符，"
                f"仅在 1 个 chunk 内完整到达 #{arg_segs[0][0] if arg_segs else '?'}"
            )
            verdict = "【整块一次性输出】"
        else:
            seg_detail = ", ".join(f"#{no}:{n}c" for no, n in arg_segs)
            lines.append(
                f"    arguments: 共 {total_arg_chars} 字符，分 {seg_count} 段累加: [{seg_detail}]"
            )
            verdict = f"【分 chunk 增量输出】跨 {seg_count} 个 chunk"
        lines.append(f"    → 判定: {verdict}")
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1, help="每种方式重复次数（取众数判定）")
    args = parser.parse_args()

    print("=" * 70)
    print("GLM-5.2 工具调用分块诊断")
    print(f"  模型={MODEL}  工具=search_restaurants  每种方式跑 {args.runs} 次")
    print("=" * 70)

    for run in range(1, args.runs + 1):
        print(f"\n---------- 第 {run}/{args.runs} 轮 ----------")

        print("\n[A] 直连 httpx SSE（上游真实分块）...")
        try:
            tr_a = await run_direct()
            print(summarize("A 直连 httpx", tr_a))
        except Exception as exc:  # noqa: BLE001
            print(f"  [A] 失败: {type(exc).__name__}: {exc}")

        print("\n[B] 项目客户端 litellm zai/glm-5.2（litellm 层）...")
        try:
            tr_b = await run_via_adapter()
            print(summarize("B 项目客户端(litellm)", tr_b))
        except Exception as exc:  # noqa: BLE001
            print(f"  [B] 失败: {type(exc).__name__}: {exc}")

    print("\n" + "=" * 70)
    print("判定要点：arguments 是分 1 段（整块）还是多段（分 chunk）。")
    print("  · 直连和 litellm 结果一致 → 由上游 GLM-5.2 决定")
    print("  · 直连分块、litellm 整块  → litellm 在客户端做了合并")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
