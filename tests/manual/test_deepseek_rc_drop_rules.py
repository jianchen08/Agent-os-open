"""DeepSeek reasoning_content 丢弃规则精测。

核心问题：messages 中有多个 assistant(tool_calls)，中间被 user 消息隔开时，
前面那些 assistant 的 reasoning_content 能否丢掉？

测试矩阵（5 个变体）：
M1: 2组tc, 中间无user, 都不传rc        → 已知 400
M2: 2组tc, 中间有user, 都不传rc        → 真实场景
M3: 2组tc, 中间有user, 只传后面那个rc  → 关键！能否省前面的
M4: 2组tc, 中间有user, 只传前面那个rc  → 对照
M5: 2组tc, 中间有user, 都传rc         → 基线

附带：
M6: 验证缓存命中 - 比较 "都传完整rc" vs "前面截断rc" 的 cached_tokens
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
            err = r.text[:200]
            return {"ok": False, "status": r.status_code, "err": err,
                    "is_rc": "reasoning_content" in err, "dt": dt}
        data = r.json()
        usage = data.get("usage", {})
        prompt_details = usage.get("prompt_tokens_details", {})
        return {"ok": True, "status": 200, "dt": dt,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "cached_tokens": prompt_details.get("cached_tokens", 0),
                "cache_hit": prompt_details.get("cached_tokens", 0) > usage.get("prompt_tokens", 0) * 0.3}
    except Exception as e:
        return {"ok": False, "err": str(e)[:200], "is_rc": False, "dt": 0}


def tc(id_, q='{"q":"x"}'):
    return {"id": id_, "type": "function",
            "function": {"name": "search", "arguments": q}}


def asst(id_, rc=None):
    """构造 assistant(tool_calls) 消息。rc=None 表示不带 reasoning_content 字段。"""
    m = {"role": "assistant", "content": "", "tool_calls": [tc(id_)]}
    if rc is not None:
        m["reasoning_content"] = rc
    return m


def tool_r(id_, content="result"):
    return {"role": "tool", "tool_call_id": id_, "content": content}


RC1 = "我需要先查询一下相关信息来回答用户的问题。"
RC2 = "继续分析第二部分的资料。"


def M1():
    """2组tc, 中间无user, 都不传rc。"""
    return [
        {"role": "user", "content": "q1"},
        asst("c1"),              # 不带 rc
        tool_r("c1"),
        asst("c2"),              # 不带 rc
        tool_r("c2"),
    ]

def M2():
    """2组tc, 中间有user, 都不传rc。"""
    return [
        {"role": "user", "content": "q1"},
        asst("c1"),              # 不带 rc
        tool_r("c1"),
        {"role": "user", "content": "继续"},  # 中间 user
        asst("c2"),              # 不带 rc
        tool_r("c2"),
    ]

def M3():
    """2组tc, 中间有user, 只传后面那个rc（前面丢）。"""
    return [
        {"role": "user", "content": "q1"},
        asst("c1"),              # 不带 rc（前面丢掉）
        tool_r("c1"),
        {"role": "user", "content": "继续"},
        asst("c2", rc=RC2),      # 只带后面的 rc
        tool_r("c2"),
    ]

def M4():
    """2组tc, 中间有user, 只传前面那个rc（后面丢）。"""
    return [
        {"role": "user", "content": "q1"},
        asst("c1", rc=RC1),      # 只带前面的 rc
        tool_r("c1"),
        {"role": "user", "content": "继续"},
        asst("c2"),              # 不带 rc（后面丢掉）
        tool_r("c2"),
    ]

def M5():
    """2组tc, 中间有user, 都传rc（基线）。"""
    return [
        {"role": "user", "content": "q1"},
        asst("c1", rc=RC1),
        tool_r("c1"),
        {"role": "user", "content": "继续"},
        asst("c2", rc=RC2),
        tool_r("c2"),
    ]


def run_case(name, builder, prefill_cache=False):
    """跑单个测试用例。prefill_cache=True 表示先跑一次填充缓存。"""
    msgs = builder()
    if prefill_cache:
        # 先跑一次相同 messages 填充缓存
        call(msgs, tag=f"{name}-prefill")
        time.sleep(0.5)
    r = call(msgs, tag=name)
    status = "✅ OK" if r["ok"] else f"❌ {r['status']}"
    rc_flag = " [REASONING!]" if r.get("is_rc") else ""
    cache_info = ""
    if r["ok"]:
        cache_info = f" | cached={r.get('cached_tokens', 0)}/{r.get('prompt_tokens', 0)}"
    print(f"{name:35} → {status}{rc_flag}{cache_info}  ({r['dt']}s)")
    return r


def main():
    if not API_KEY:
        print("ERROR", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"目标：精确定位 reasoning_content 丢弃规则 + 缓存影响\n")

    print("【阶段1：丢弃规则】")
    print("-" * 70)
    run_case("M1: 2组tc无user都不传rc", M1)
    run_case("M2: 2组tc有user都不传rc", M2)
    run_case("M3: 2组tc有user只传后rc", M3)
    run_case("M4: 2组tc有user只传前rc", M4)
    run_case("M5: 2组tc有user都传rc", M5)

    print("\n【阶段2：缓存命中影响】")
    print("-" * 70)
    print("先填充缓存（跑 M5 两次），再对比改动 rc 后的 cache 命中：")
    print()
    print("→ 先跑 M5（完整 rc）两次填充缓存：")
    r5a = run_case("M5a 首次（冷）", M5)
    time.sleep(1)
    r5b = run_case("M5b 二次（应命中缓存）", M5)
    print()
    print("→ 改为 M3（前面 rc 丢失）看缓存是否还在：")
    time.sleep(1)
    r3 = run_case("M3 前面rc丢失", M3)
    print()
    print("→ 结论对比：")
    if r5b.get("ok") and r3.get("ok"):
        c5 = r5b.get("cached_tokens", 0)
        c3 = r3.get("cached_tokens", 0)
        if c3 < c5 * 0.5:
            print(f"  ⚠️  丢前面 rc 导致缓存命中率大幅下降：{c5} → {c3}")
        else:
            print(f"  ✅ 缓存基本保持：{c5} → {c3}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
