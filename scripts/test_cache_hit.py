"""直连 DeepSeek 官方 API 的 prompt cache 命中实验。

绕开所有 agent 框架，纯 HTTP 调用。每轮往对话历史里追加 1 万随机字符，
观察 cached_tokens 如何随 input_tokens 增长而变化。

目标：判断 DeepSeek 服务端的 context cache 是「只缓存 system 段」还是
「能缓存稳定前缀」。

用法：
    DEEPSEEK_API_KEY=xxx python scripts/test_cache_hit.py
"""

from __future__ import annotations

import os
import random
import string
import sys
import time

import httpx

API_BASE = os.environ.get("TEST_API_BASE", "https://api.deepseek.com/v1")
API_KEY = os.environ.get("TEST_API_KEY", "")
MODEL = os.environ.get("TEST_MODEL", "deepseek-chat")

# 固定 system prompt：约 2000 字符，模拟真实场景的固定前缀
SYSTEM_PROMPT = """你是一个测试助手。请简洁回答。

# 规则一
事实性信息必须标注来源。来源不可追溯时标注[未验证]。
无材料支撑时不编写该内容，如实报告"未获取到材料"。

# 规则二
产出必须如实反映目标达成度。未达成的部分明确标注，不以替代品冒充目标产出。
部分完成时标注完成度和缺失项。

# 规则三
失败必须如实记录：尝试了什么、失败原因、哪些内容缺失。
禁止用自身知识冒充实际调研结果。禁止将部分完成包装为完全完成。
禁止隐藏失败记录，只展示成功部分。

# 任务说明
你负责回答用户的问题。回答使用中文。保持简洁。不要赘述。
当前是一个多轮对话测试，用户会发送大量随机文本，你只需简短回应即可。
每次回复控制在 20 字以内。""" + ("测试填充内容。" * 60)

# 目标：跑到 30 万 token 或最多 40 轮
TARGET_TOKENS = 300_000
MAX_ROUNDS = 40
RANDOM_CHARS_PER_ROUND = 10_000  # 每轮追加 1 万随机字符


def gen_random_text(n: int, seed: int) -> str:
    """生成 n 个随机字符（可重复 seed 复现）。"""
    rng = random.Random(seed)
    # 用中文字符 + 字母数字混合，让 token 数更接近真实场景
    pool = string.ascii_letters + string.digits + "测试随机内容填充字符数据文本信息"
    return "".join(rng.choice(pool) for _ in range(n))


def call_deepseek(messages: list[dict]) -> dict:
    """非流式调用 DeepSeek，返回 usage。"""
    url = f"{API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 16,
        "temperature": 0.0,
    }
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()
    return data


def main() -> int:
    if not API_KEY:
        print("ERROR: DEEPSEEK_API_KEY 未设置", file=sys.stderr)
        return 1

    print(f"模型: {MODEL}")
    print(f"API: {API_BASE}")
    print(f"system_prompt 长度: {len(SYSTEM_PROMPT)} 字符")
    print(f"每轮追加: {RANDOM_CHARS_PER_ROUND} 随机字符")
    print(f"目标: {TARGET_TOKENS} token 或 {MAX_ROUNDS} 轮")
    print()
    print(f"{'轮次':<5} {'input':>8} {'cached':>8} {'命中率':>8} {'耗时':>7}")
    print("-" * 45)

    # 固定前缀：system + 第一条 user
    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "开始测试，请回复\"收到\"。"},
    ]

    for rnd in range(1, MAX_ROUNDS + 1):
        t0 = time.time()
        try:
            data = call_deepseek(messages)
        except Exception as e:
            print(f"轮{rnd} 调用失败: {e}", file=sys.stderr)
            break

        usage = data.get("usage", {})
        # DeepSeek usage 结构：prompt_tokens / completion_tokens / prompt_cache_hit_tokens
        input_tokens = usage.get("prompt_tokens", 0)
        # cached 兼容两种字段名
        cached = usage.get("prompt_cache_hit_tokens", 0) or usage.get("cached_tokens", 0)
        # 也有 prompt_cache_miss_tokens
        miss = usage.get("prompt_cache_miss_tokens", 0)
        elapsed = time.time() - t0

        hit_rate = (cached / input_tokens * 100) if input_tokens else 0
        print(
            f"{rnd:<5} {input_tokens:>8} {cached:>8} {hit_rate:>7.1f}% {elapsed:>6.1f}s"
            f"  miss={miss}"
        )

        if input_tokens >= TARGET_TOKENS:
            print(f"\n达到目标 {TARGET_TOKENS} token，停止。")
            break

        # 把模型回复加入历史
        reply = data["choices"][0]["message"]["content"]

        # 追加本轮 assistant 回复
        messages.append({"role": "assistant", "content": reply})

        # 追加下一条 user：1 万随机字符（seed 用轮次，保证可复现且每轮不同）
        rand_text = gen_random_text(RANDOM_CHARS_PER_ROUND, seed=rnd)
        messages.append({"role": "user", "content": f"第{rnd}轮随机数据：\n{rand_text}"})

    print("\n实验结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
