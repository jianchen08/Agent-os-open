"""DeepSeek KV Cache 是否包含 reasoning_content？精确判定。

设计思路：
- 用一个长的 rc 填充缓存
- 然后改变 rc 的内容（哪怕一个字符）
- 观察 cached_tokens 变化

如果 rc 参与前缀缓存：
  改 rc → 前缀断裂 → cached_tokens 大幅下降

如果 rc 不参与缓存：
  改 rc → 缓存还在 → cached_tokens 保持

附带对比：
- 改 content（已知参与缓存）作为对照
- 改 tool_call arguments（已知参与缓存）作为对照
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
API_BASE = "https://api.deepseek.com/v1"
MODEL = "deepseek-v4-pro"

HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

TOOL = {"type": "function", "function": {
    "name": "search", "description": "搜索",
    "parameters": {"type": "object",
                   "properties": {"q": {"type": "string"}},
                   "required": ["q"]}}}

# 一个足够长的 reasoning_content，让缓存差异明显
LONG_RC_V1 = "我需要仔细分析这个问题。" * 50  # ~450 字符
LONG_RC_V2 = "我需要仔细分析这个问题！" * 50  # 改了一个标点（V1 vs V2 差异）


def call(messages, tag=""):
    payload = {"model": MODEL, "messages": messages,
               "thinking": {"type": "enabled"}, "reasoning_effort": "max",
               "tools": [TOOL]}
    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=120.0) as c:
            r = c.post(f"{API_BASE}/chat/completions", headers=HEADERS, json=payload)
        dt = round(time.monotonic() - t0, 2)
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code,
                    "err": r.text[:200], "dt": dt}
        data = r.json()
        usage = data.get("usage", {})
        prompt_details = usage.get("prompt_tokens_details", {})
        cache_miss = usage.get("prompt_cache_miss_tokens", 0)
        cache_hit = usage.get("prompt_cache_hit_tokens", 0)
        return {
            "ok": True, "dt": dt,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "cached_tokens": prompt_details.get("cached_tokens", 0),
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
        }
    except Exception as e:
        return {"ok": False, "err": str(e)[:200], "dt": 0}


def asst_with_rc(rc_text):
    return {"role": "assistant", "content": "",
            "reasoning_content": rc_text,
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "search",
                                         "arguments": '{"q":"test"}'}}]}


def base_msgs(rc_text, user_content="用户问题A"):
    """基础消息结构，rc_text 和 user_content 可变。"""
    return [
        {"role": "user", "content": user_content},
        asst_with_rc(rc_text),
        {"role": "tool", "tool_call_id": "c1", "content": "tool result"},
        {"role": "user", "content": "请继续分析"},
    ]


def main():
    if not API_KEY:
        print("ERROR", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"目标：判定 reasoning_content 是否参与 KV Cache 前缀匹配\n")
    print(f"LONG_RC_V1 长度: {len(LONG_RC_V1)} 字符")
    print(f"LONG_RC_V2 长度: {len(LONG_RC_V2)} 字符（仅标点差异）")
    print()

    print("=" * 70)
    print("【测试 1：填充缓存 - 用 V1 跑两次】")
    print("=" * 70)
    msgs_v1 = base_msgs(LONG_RC_V1)
    r1 = call(msgs_v1, "v1-cold")
    print(f"V1 冷启动: cached={r1.get('cached_tokens', 0)}/{r1.get('prompt_tokens', 0)} "
          f"hit={r1.get('cache_hit_tokens', 0)} miss={r1.get('cache_miss_tokens', 0)}")
    time.sleep(1)
    r1b = call(msgs_v1, "v1-warm")
    print(f"V1 二次:   cached={r1b.get('cached_tokens', 0)}/{r1b.get('prompt_tokens', 0)} "
          f"hit={r1b.get('cache_hit_tokens', 0)} miss={r1b.get('cache_miss_tokens', 0)}")
    warm_cached = r1b.get('cached_tokens', 0)

    print()
    print("=" * 70)
    print("【测试 2：改变 rc 内容（V1→V2），看缓存是否还命中】")
    print("=" * 70)
    print("（如果 cached_tokens 大幅下降 → rc 参与缓存）")
    time.sleep(1)
    msgs_v2 = base_msgs(LONG_RC_V2)  # 只改了 rc 的一个标点
    r2 = call(msgs_v2, "v2-rc-changed")
    print(f"V2 (rc改): cached={r2.get('cached_tokens', 0)}/{r2.get('prompt_tokens', 0)} "
          f"hit={r2.get('cache_hit_tokens', 0)} miss={r2.get('cache_miss_tokens', 0)}")

    print()
    print("=" * 70)
    print("【测试 3：完全去掉 rc，看缓存命中范围】")
    print("=" * 70)
    time.sleep(1)
    msgs_no_rc = base_msgs(LONG_RC_V1)
    # 移除 assistant 的 reasoning_content
    msgs_no_rc[1].pop("reasoning_content")
    r3 = call(msgs_no_rc, "no-rc")
    print(f"无 rc:     cached={r3.get('cached_tokens', 0)}/{r3.get('prompt_tokens', 0)} "
          f"hit={r3.get('cache_hit_tokens', 0)} miss={r3.get('cache_miss_tokens', 0)}")

    print()
    print("=" * 70)
    print("【测试 4：对照组 - 改 user 消息内容（必参与缓存）】")
    print("=" * 70)
    time.sleep(1)
    msgs_diff_user = base_msgs(LONG_RC_V1, user_content="完全不同的问题B")
    r4 = call(msgs_diff_user, "diff-user")
    print(f"改user:    cached={r4.get('cached_tokens', 0)}/{r4.get('prompt_tokens', 0)} "
          f"hit={r4.get('cache_hit_tokens', 0)} miss={r4.get('cache_miss_tokens', 0)}")

    print()
    print("=" * 70)
    print("【判定】")
    print("=" * 70)
    v1_warm = r1b.get('cached_tokens', 0)
    v2_cached = r2.get('cached_tokens', 0)
    no_rc_cached = r3.get('cached_tokens', 0)
    diff_user_cached = r4.get('cached_tokens', 0)

    print(f"V1 暖（基线）:       cached={v1_warm}")
    print(f"V2 改 rc 一个标点:   cached={v2_cached}  差异={v1_warm - v2_cached}")
    print(f"完全去掉 rc:         cached={no_rc_cached}  差异={v1_warm - no_rc_cached}")
    print(f"改 user 内容（对照）: cached={diff_user_cached}  差异={v1_warm - diff_user_cached}")
    print()
    if v2_cached < v1_warm * 0.7:
        print(">>> ❌ rc 参与缓存！改 rc 标点就破坏前缀匹配")
        print(">>> 方案需要修正：不能丢前面的 rc")
    elif no_rc_cached < v1_warm * 0.7:
        print(">>> ⚠️  去 rc 后缓存下降，rc 部分参与缓存")
    else:
        print(">>> ✅ rc 不参与缓存前缀匹配，可以丢")

    return 0


if __name__ == "__main__":
    sys.exit(main())
