"""对照实验：逐个加上 agent 框架的特征，找出哪个破坏 prompt cache。

直连脚本 baseline 命中率 97%。agent 框架 6%。两者差异逐个复现：
  - mode=baseline         纯净（已知 97%）
  - mode=multi_system     多条 system 消息（compressed/state_snapshot）
  - mode=reasoning        assistant 带 reasoning_content 字段
  - mode=name_field       system 带 name 字段
  - mode=tool_calls       assistant 带 tool_calls + tool 消息
  - mode=all             全部 agent 特征叠加

用法：
    TEST_API_BASE=... TEST_API_KEY=... TEST_MODEL=... \
    CACHE_MODE=multi_system python scripts/test_cache_features.py
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
MODE = os.environ.get("CACHE_MODE", "baseline")

# 是否带 tools 参数（复现 agent 框架的 function calling）
USE_TOOLS = os.environ.get("USE_TOOLS", "0") == "1"
TOOLS_FILE = os.environ.get("TOOLS_FILE", "logs/payload_diag/_real_tools.json")

TARGET_TOKENS = 200_000
MAX_ROUNDS = 25
RANDOM_CHARS_PER_ROUND = 10_000

SYSTEM_PROMPT = """你是一个测试助手。请简洁回答。

# 规则一
事实性信息必须标注来源。来源不可追溯时标注[未验证]。
无材料支撑时不编写该内容，如实报告"未获取到材料"。

# 规则二
产出必须如实反映目标达成度。未达成的部分明确标注，不以替代品冒充目标产出。

# 规则三
失败必须如实记录：尝试了什么、失败原因、哪些内容缺失。
禁止用自身知识冒充实际调研结果。
""" + ("测试填充内容。" * 60)

# multi_system 模式用的额外 system 块（模拟 compressed/state_snapshot）
EXTRA_SYSTEM_BLOCKS = [
    "## 过程摘要\n用户与助手进行了多轮测试交互，验证 prompt cache 命中率。"
    "助手每次简短回复。这是一个固定的压缩块，不应每轮变化。" * 5,
    "## 决策记录\n采用 DeepSeek 直连测试方案，每轮追加随机字符观察缓存行为。" * 5,
]


def gen_random_text(n: int, seed: int) -> str:
    rng = random.Random(seed)
    pool = string.ascii_letters + string.digits + "测试随机内容填充字符数据文本信息"
    return "".join(rng.choice(pool) for _ in range(n))


def build_initial_messages(mode: str) -> list[dict]:
    """按模式构造初始消息序列（前缀部分）。"""
    msgs: list[dict] = []

    if mode == "multi_system" or mode == "all":
        # MSG[0] 主 system（无 name）
        msgs.append({"role": "system", "content": SYSTEM_PROMPT})
        # MSG[1..] 额外 system 块（带 name，模拟 compressed/state_snapshot）
        for i, block in enumerate(EXTRA_SYSTEM_BLOCKS):
            msgs.append({
                "role": "system",
                "name": "compressed" if i < 2 else "state_snapshot",
                "content": f"<compressed level=\"L1\">\n{block}\n</compressed>",
            })
    elif mode == "name_field" or mode == "tool_calls":
        # 单条 system 但带 name
        msgs.append({"role": "system", "name": "main", "content": SYSTEM_PROMPT})
    else:
        # baseline / reasoning: 单条 system 无 name
        msgs.append({"role": "system", "content": SYSTEM_PROMPT})

    msgs.append({"role": "user", "content": "开始测试，请回复\"收到\"。"})
    return msgs


def make_assistant_reply(text: str, mode: str) -> dict:
    """按模式构造 assistant 消息。"""
    msg: dict = {"role": "assistant", "content": text}
    if mode in ("reasoning", "all"):
        # 带 reasoning_content 字段（DeepSeek thinking 模式回传）
        msg["reasoning_content"] = "思考：用户发送了随机数据，我只需简短回复即可。"
    return msg


def call_api(messages: list[dict]) -> dict:
    url = f"{API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 16,
        "temperature": 0.0,
    }
    # reasoning 模式下加 thinking 参数（DeepSeek thinking 模式）
    if MODE in ("reasoning", "all"):
        payload["thinking"] = {"type": "enabled"}
    # 带工具（复现 agent 框架 function calling）
    if USE_TOOLS:
        import json as _json
        try:
            with open(TOOLS_FILE, encoding="utf-8") as fh:
                payload["tools"] = _json.load(fh)
        except Exception as e:
            print(f"WARNING: 读取 tools 失败: {e}", file=sys.stderr)
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        return resp.json()


def main() -> int:
    if not API_KEY:
        print("ERROR: TEST_API_KEY 未设置", file=sys.stderr)
        return 1

    print(f"模式: {MODE}  tools={USE_TOOLS}  trailing_dynamic={os.environ.get('TRAILING_DYNAMIC','0')}  dyn_role={os.environ.get('DYN_ROLE','system')}")
    print(f"模型: {MODEL}  API: {API_BASE}")
    print(f"目标: {TARGET_TOKENS} token / {MAX_ROUNDS} 轮")
    print(f"{'轮':<4} {'input':>8} {'cached':>8} {'命中率':>8} {'耗时':>6}")
    print("-" * 42)

    messages = build_initial_messages(MODE)

    # TRAILING_DYNAMIC=1 时，每轮发送前在末尾临时插一条每轮变化的 system 消息
    # （模拟 agent 框架的 dynamic_context：含 timestamp，每轮不同，位于最末尾）。
    # 发送后立即移除，不写入 history。这能验证"末尾每轮变的 system"是否破坏缓存。
    TRAILING_DYNAMIC = os.environ.get("TRAILING_DYNAMIC", "0") == "1"

    for rnd in range(1, MAX_ROUNDS + 1):
        # 模拟 dynamic_context：每轮 timestamp 不同
        # DYN_ROLE=user 时，末尾追加的动态消息用 user 角色（对照 system 角色）
        DYN_ROLE = os.environ.get("DYN_ROLE", "system")
        if TRAILING_DYNAMIC:
            from datetime import datetime
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            dyn = {
                "role": DYN_ROLE,
                "content": f"<dynamic_vars>\n- 时间: {ts}\n- 当前轮次: {rnd}\n</dynamic_vars>",
            }
            if DYN_ROLE == "system":
                dyn["name"] = "dynamic_context"
            send_msgs = messages + [dyn]
        else:
            send_msgs = messages

        t0 = time.time()
        data = None
        for attempt in range(3):
            try:
                data = call_api(send_msgs)
                break
            except Exception as e:
                if attempt == 2:
                    print(f"轮{rnd} 失败(重试3次): {e}", file=sys.stderr)
                    break
                time.sleep(2)
        if data is None:
            break
            print(f"轮{rnd} 失败: {e}", file=sys.stderr)
            break

        usage = data.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        cached = usage.get("prompt_cache_hit_tokens", 0) or usage.get("cached_tokens", 0)
        miss = usage.get("prompt_cache_miss_tokens", 0)
        elapsed = time.time() - t0
        hit_rate = (cached / input_tokens * 100) if input_tokens else 0
        print(f"{rnd:<4} {input_tokens:>8} {cached:>8} {hit_rate:>7.1f}% {elapsed:>5.1f}s miss={miss}")

        if input_tokens >= TARGET_TOKENS:
            print(f"\n达到 {TARGET_TOKENS} token，停止。")
            break

        reply = data["choices"][0]["message"]["content"]
        messages.append(make_assistant_reply(reply, MODE))

        rand_text = gen_random_text(RANDOM_CHARS_PER_ROUND, seed=rnd)
        messages.append({"role": "user", "content": f"第{rnd}轮：\n{rand_text}"})

    print("结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
