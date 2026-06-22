"""DeepSeek reasoning_content 空字段方案精确测试。

用户提出：能否把前面轮次的 rc 改成空字段（None / "" / 缺省）来省 token？

测试矩阵：
A) 完整 rc（基线）             → 缓存命中
B) rc = None                  → ?
C) rc = ""（空字符串）          → ?
D) 完全省略 rc 字段             → 已知 400
E) rc = "占位符"               → ?

每个变体测两次：
1. 先用完整 rc 填充缓存
2. 再用空字段版本，看 cached_tokens 是否保持

如果空字段保留缓存 → 完美方案（省 token + 保缓存）
如果空字段破坏缓存 → 不如直接用完整 rc
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

# 长 rc 用于让缓存差异明显
LONG_RC = ("用户问的是天气查询，我需要先调用 search 工具获取信息。"
           "考虑到用户可能想要详细的天气情况，包括温度、湿度、风力等，"
           "我应该一次性查询所有相关信息。" * 8)  # ~600 字符


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
                    "err": r.text[:200],
                    "is_rc_err": "reasoning_content" in r.text,
                    "dt": dt}
        data = r.json()
        usage = data.get("usage", {})
        prompt_details = usage.get("prompt_tokens_details", {})
        return {
            "ok": True, "dt": dt,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "cached_tokens": prompt_details.get("cached_tokens", 0),
            "cache_hit": usage.get("prompt_cache_hit_tokens", 0),
            "cache_miss": usage.get("prompt_cache_miss_tokens", 0),
        }
    except Exception as e:
        return {"ok": False, "err": str(e)[:200], "is_rc_err": False, "dt": 0}


def build_messages(*, rc1=None, rc2=None, omit_rc1=False, omit_rc2=False):
    """构造 2 组 tool_calls 的 messages。

    rc1/rc2 控制第一条/第二条 assistant 的 reasoning_content：
    - None: 不设置该字段
    - "": 设置为空字符串
    - 字符串: 设置为该内容
    omit=True: 完全不设置字段（用于测试缺省）
    """
    def make_asst(rc, omit):
        m = {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "search",
                                          "arguments": '{"q":"x"}'}}]}
        # 给每条 assistant 用不同 id 避免冲突
        return m

    # 重新构造，使用不同 id
    def make_asst_with_id(aid, rc, omit):
        m = {"role": "assistant", "content": "",
             "tool_calls": [{"id": aid, "type": "function",
                             "function": {"name": "search",
                                          "arguments": '{"q":"x"}'}}]}
        if not omit and rc is not None:
            m["reasoning_content"] = rc
        elif not omit and rc == "":
            m["reasoning_content"] = ""
        return m

    return [
        {"role": "user", "content": "问题1"},
        make_asst_with_id("c1", rc1, omit_rc1),
        {"role": "tool", "tool_call_id": "c1", "content": "结果1"},
        {"role": "user", "content": "继续"},
        make_asst_with_id("c2", rc2, omit_rc2),
        {"role": "tool", "tool_call_id": "c2", "content": "结果2"},
    ]


def report(name, r):
    if r["ok"]:
        print(f"  {name:30} ✅ cached={r['cached_tokens']:>4}/{r['prompt_tokens']:>4} "
              f"hit={r['cache_hit']} miss={r['cache_miss']}  ({r['dt']}s)")
    else:
        flag = " [RC错误!]" if r.get("is_rc_err") else ""
        print(f"  {name:30} ❌ {r['status']}{flag}  ({r['dt']}s)")


def main():
    if not API_KEY:
        print("ERROR", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"目标：测试空字段方案（None/空串/缺省）能否省 token + 保缓存\n")
    print(f"LONG_RC 长度: {len(LONG_RC)} 字符\n")

    # ── 阶段 1：用完整 rc 填充缓存 ──
    print("=" * 70)
    print("【阶段 1：完整 rc 填充缓存】")
    print("=" * 70)
    msgs_full = build_messages(rc1=LONG_RC, rc2=LONG_RC)
    r1 = call(msgs_full, "full-cold")
    report("完整 rc 冷启动", r1)
    time.sleep(1)
    r1b = call(msgs_full, "full-warm")
    report("完整 rc 二次（基线）", r1b)
    baseline_cached = r1b.get("cached_tokens", 0)
    baseline_prompt = r1b.get("prompt_tokens", 0)
    print(f"\n  >>> 基线: cached={baseline_cached} prompt={baseline_prompt}")

    # ── 阶段 2：第二组 rc 替换为空值 ──
    print()
    print("=" * 70)
    print("【阶段 2：第二组 rc 改为各种空值，看缓存 + 是否 400】")
    print("=" * 70)
    print("（第一组 rc 保持完整，所以前缀缓存应该命中到第一组结束）")
    print()

    # B) rc2 = None（不写该字段）
    time.sleep(1)
    msgs_b = build_messages(rc1=LONG_RC, omit_rc2=True)
    r2b = call(msgs_b, "rc2-omitted")
    report("B: rc2 完全省略", r2b)

    # C) rc2 = ""（空字符串）
    time.sleep(1)
    msgs_c = build_messages(rc1=LONG_RC, rc2="")
    r2c = call(msgs_c, "rc2-empty-string")
    report("C: rc2 = ''", r2c)

    # D) rc2 = 占位符
    time.sleep(1)
    msgs_d = build_messages(rc1=LONG_RC, rc2="[thinking omitted]")
    r2d = call(msgs_d, "rc2-placeholder")
    report("D: rc2 = 占位符", r2d)

    # ── 阶段 3：第一组也改空值（最激进，看是否 400） ──
    print()
    print("=" * 70)
    print("【阶段 3：第一组 rc 也改空值（多轮场景的核心）】")
    print("=" * 70)

    # E) 两组都 None（已知会 400，对照组）
    time.sleep(1)
    msgs_e = build_messages(omit_rc1=True, omit_rc2=True)
    r3e = call(msgs_e, "both-omitted")
    report("E: 两组都省略（对照）", r3e)

    # F) 两组都空串
    time.sleep(1)
    msgs_f = build_messages(rc1="", rc2="")
    r3f = call(msgs_f, "both-empty-string")
    report("F: 两组都 = ''", r3f)

    # G) 两组都占位符
    time.sleep(1)
    msgs_g = build_messages(rc1="[omitted]", rc2="[omitted]")
    r3g = call(msgs_g, "both-placeholder")
    report("G: 两组都占位符", r3g)

    # ── 判定 ──
    print()
    print("=" * 70)
    print("【判定】")
    print("=" * 70)
    print(f"基线 cached_tokens: {baseline_cached}")
    print()
    print("如果空字段方案可行，需要同时满足：")
    print("  1. 不触发 400")
    print("  2. 缓存命中率保持")
    print()
    ok_cases = []
    for name, r in [("B:rc2省略", r2b), ("C:rc2=''", r2c), ("D:rc2占位", r2d),
                    ("F:都=''", r3f), ("G:都占位", r3g)]:
        if r["ok"]:
            cache_drop = baseline_cached - r.get("cached_tokens", 0)
            verdict = "✅ 可行" if cache_drop < baseline_cached * 0.2 else f"⚠️ 缓存掉{cache_drop}"
            print(f"  {name}: {verdict}")
            if cache_drop < baseline_cached * 0.2:
                ok_cases.append(name)
        else:
            print(f"  {name}: ❌ 400 不可行")

    print()
    if ok_cases:
        print(f">>> 可行方案: {ok_cases}")
    else:
        print(">>> 所有空字段方案都不可行，必须全量保留 rc")

    return 0


if __name__ == "__main__":
    sys.exit(main())
