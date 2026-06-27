"""工具调用长内容接收对照实验。

目标：摸清"多大的 file_write tool_call 能完整接收"，对照三条路径：
  A. litellm 直连（litellm.acompletion，不经项目封装）
  B. 项目 KeyPoolAdapter 客户端（build_adapter，真实运行路径）
  C. 并行 2 个（验证并发是否互相挤死）

每组让模型生成一个 file_write 工具调用，要求内容为 N 行文本（N 递增），
测量：
  - 是否拿到 tool_calls（finish_reason）
  - arguments 字符数（完整 vs 截断）
  - 流式 chunk 总数 / 耗时 / 速度
  - 是否触发 STREAM TIMEOUT / 异常

用法（PYTHONPATH=src）：
  python scripts/probe_toolcall_timeout.py            # 全量 A+B+C
  python scripts/probe_toolcall_timeout.py --path A   # 只跑直连
  python scripts/probe_toolcall_opcode.py --path C --lines 2000,5000
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from typing import Any

# 确保 src 在 path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# 加载 .env
from pathlib import Path  # noqa: E402
_env = Path(__file__).resolve().parent.parent / ".env"
if _env.exists():
    for line in _env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _build_messages(target_lines: int) -> list[dict]:
    """构造让模型生成一个大 file_write 工具调用的 prompt。"""
    return [
        {
            "role": "user",
            "content": (
                f"请用 file_write 工具创建文件 big_file.txt，内容必须是【恰好 {target_lines} 行】"
                f"的连续编号文本，每行格式：第0001行：测试数据xxxxxxxxxx。"
                f"从第0001行一直写到第{target_lines:04d}行，一行不少，全部放在一次 file_write 调用的 content 参数里。"
                f"不要省略，不要用省略号，必须完整输出全部 {target_lines} 行。"
            ),
        }
    ]


def _tools() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "file_write",
                "description": "写入文件",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "content": {"type": "string"},
                    },
                    "required": ["path", "content"],
                },
            },
        }
    ]


# ───────────────────── 路径 A：litellm 直连 ─────────────────────

async def run_direct(model: str, api_key: str, api_base: str,
                     target_lines: int, read_timeout: float) -> dict:
    """litellm.acompletion 直连，可调 httpx read timeout。"""
    import litellm

    t0 = time.monotonic()
    chunks = 0
    finish_reason = None
    tool_args_len = 0
    err = None
    last_chunk_at = t0
    max_gap = 0.0          # 最大 chunk 间隔（秒）
    last_progress_at = t0  # 最后一次收到有效内容的时间
    first_chunk_at = None
    tc_chunks = 0          # 含 tool_calls 的 chunk 数（验证 tool_stream 是否分块）
    tc_sample_sizes = []   # 前 10 个 tool_call 分片大小
    try:
        resp = await litellm.acompletion(
            model=model,
            messages=_build_messages(target_lines),
            tools=_tools(),
            stream=True,
            tool_stream=True,              # Z.ai 工具调用流式：让大 tool_call 分块传输
            api_key=api_key,
            api_base=api_base,
            timeout=read_timeout,            # httpx read timeout（socket 层）
            num_retries=0,
            max_tokens=100000,
        )
        async for chunk in resp:
            now = time.monotonic()
            if first_chunk_at is None:
                first_chunk_at = now
            gap = now - last_chunk_at
            if gap > max_gap:
                max_gap = gap
            last_chunk_at = now
            chunks += 1
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            if getattr(chunk.choices[0], "finish_reason", None):
                finish_reason = chunk.choices[0].finish_reason
            tc = getattr(d, "tool_calls", None)
            if tc:
                tc_chunks += 1
                for c in tc:
                    fn = getattr(c, "function", None)
                    if fn and getattr(fn, "arguments", None):
                        al = len(fn.arguments)
                        tool_args_len += al
                        last_progress_at = now
                        if len(tc_sample_sizes) < 10:
                            tc_sample_sizes.append(al)
            # 快速验证模式：收到足够多 tool_call 分片即可判定，不必等全部
            if tc_chunks >= 30 and finish_reason is None:
                finish_reason = "(early-stop:已收30+个tool_call分片)"
                break
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:200]}"

    # 卡死诊断：最后收到内容到结束的静默时长
    silent_after = round(time.monotonic() - last_progress_at, 1) if tool_args_len else None
    return {
        "path": "A_direct",
        "target_lines": target_lines,
        "read_timeout": read_timeout,
        "chunks": chunks,
        "tc_chunks": tc_chunks,
        "tc_sample_sizes": tc_sample_sizes,
        "finish_reason": finish_reason,
        "tool_args_len": tool_args_len,
        "elapsed": round(time.monotonic() - t0, 1),
        "max_gap": round(max_gap, 1),
        "first_chunk_after": round((first_chunk_at or t0) - t0, 1),
        "silent_after_progress": silent_after,
        "error": err,
    }


# ───────────────────── 路径 B：项目 KeyPoolAdapter ─────────────────────

async def run_project_adapter(target_lines: int) -> dict:
    """走项目真实路径：build_adapter → KeyPoolAdapter.completion(stream)。"""
    from config.models import get_model_config_loader
    from llm.router_factory import build_adapter

    loader = get_model_config_loader()
    adapter = build_adapter(loader)

    t0 = time.monotonic()
    chunk_types: dict[str, int] = {}
    err = None
    resp = None
    try:
        resp = await adapter.completion(
            model="glm-5.2-yichengc",
            messages=_build_messages(target_lines),
            tools=_tools(),
            stream=True,
        )
        # 项目 adapter 流式内部已消费完，返回 LLMResponse
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:200]}"

    tool_args_len = 0
    finish_reason = None
    if resp is not None:
        tcs = getattr(resp, "tool_calls", None) or []
        for tc in tcs:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            args = fn.get("arguments", "")
            tool_args_len += len(args)
        # 有 tool_calls 内容即视为完成（LLMResponse 无 finish_reason 字段）
        finish_reason = "tool_calls" if tool_args_len else None

    return {
        "path": "B_project",
        "target_lines": target_lines,
        "chunks": None,
        "finish_reason": finish_reason,
        "tool_args_len": tool_args_len,
        "elapsed": round(time.monotonic() - t0, 1),
        "error": err,
    }


# ───────────────────── 报告 ─────────────────────

def _verdict(r: dict, target_lines: int) -> str:
    if r.get("error"):
        return "❌异常"
    if not r.get("finish_reason"):
        return "❌无finish(流被截断/卡死)"
    al = r.get("tool_args_len", 0)
    # 每行约 20 字节，加上 JSON 转义膨胀，估算期望长度
    expected_min = target_lines * 15
    if al < expected_min:
        return f"⚠️截断(args={al}c < 期望~{expected_min}c)"
    return f"✅完整(args={al}c)"


def _print(r: dict) -> None:
    verdict = _verdict(r, r["target_lines"])
    diag = ""
    if "max_gap" in r:
        diag = (f" max_gap={r['max_gap']}s first={r.get('first_chunk_after')}s"
                f" tc_chunks={r.get('tc_chunks')} tc_samples={r.get('tc_sample_sizes')}"
                f" silent={r.get('silent_after_progress')}")
    print(
        f"  [{r['path']}] lines={r['target_lines']} "
        f"耗时={r['elapsed']}s chunks={r.get('chunks')} "
        f"finish={r.get('finish_reason')} args={r.get('tool_args_len')}c "
        f"err={r.get('error') or '-'}{diag}  → {verdict}"
    )


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default="A,B,C",
                    help="跑哪些路径，逗号分隔：A=直连 B=项目 C=并行")
    ap.add_argument("--lines", default="500,2000,5000",
                    help="目标行数，逗号分隔")
    ap.add_argument("--read-timeout", type=float, default=120.0,
                    help="路径A的 httpx read timeout（秒），对照用")
    ap.add_argument("--model", default="openai/glm-5.2",
                    help="路径A直连用的 litellm model 字符串（带前缀）")
    ap.add_argument("--project-model", default="glm-5.2-yichengc",
                    help="路径B项目客户端用的 model_id")
    args = ap.parse_args()

    paths = {p.strip() for p in args.path.split(",") if p.strip()}
    line_list = [int(x) for x in args.lines.split(",") if x.strip()]
    api_key = os.environ.get("YICHENGC_API_KEY", "")
    api_base = "https://ai.1cc.ai/v1"

    print("=" * 70)
    print("工具调用长内容接收对照实验")
    print(f"  model={args.model}  api_base={api_base}")
    print(f"  路径={sorted(paths)}  行数={line_list}  read_timeout(A)={args.read_timeout}s")
    print("=" * 70)

    # A: 直连，逐个行数
    if "A" in paths:
        print("\n━━━ 路径 A：litellm 直连 (timeout 可控) ━━━")
        for n in line_list:
            r = await run_direct(args.model, api_key, api_base, n, args.read_timeout)
            _print(r)

    # B: 项目 KeyPoolAdapter，逐个行数
    if "B" in paths:
        print("\n━━━ 路径 B：项目 KeyPoolAdapter 客户端 ━━━")
        for n in line_list:
            r = await run_project_adapter(n)
            _print(r)

    # C: 并行 2 个，验证并发互相挤死
    if "C" in paths:
        print("\n━━━ 路径 C：并行 2 个（同模型，不同行数）━━━")
        # 取最大的两个行数并行
        pair = sorted(line_list)[-2:] if len(line_list) >= 2 else line_list * 2
        print(f"  并行任务行数: {pair}")
        t0 = time.monotonic()
        results = await asyncio.gather(
            run_direct(args.model, api_key, api_base, pair[0], args.read_timeout),
            run_direct(args.model, api_key, api_base, pair[1], args.read_timeout),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                print(f"  [C_parallel] ❌异常: {type(r).__name__}: {str(r)[:150]}")
            else:
                _print(r)
        print(f"  并行总耗时={round(time.monotonic()-t0,1)}s")

    print("\n实验完成。")


if __name__ == "__main__":
    asyncio.run(main())
