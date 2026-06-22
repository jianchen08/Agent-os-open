"""DeepSeek thinking 模式 - flash vs pro 对比测试。

重点测试：
1. 复杂任务（强制长思考）+ 多轮工具调用 + 不传 reasoning_content
2. flash 和 pro 都跑，对比行为差异
3. 故意触发各种异常结构，看实际错误信息

目标：搞清楚项目日志里的 reasoning_content must be passed back 到底怎么触发。
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

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取指定路径的文件内容",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def call_api(model: str, messages: list[dict], *, tools=None, tag: str = "") -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "max",
    }
    if tools:
        payload["tools"] = tools

    t0 = time.monotonic()
    try:
        with httpx.Client(timeout=180.0) as client:
            resp = client.post(f"{API_BASE}/chat/completions", headers=HEADERS, json=payload)
        elapsed = time.monotonic() - t0
        if resp.status_code != 200:
            return {
                "tag": tag, "ok": False, "status": resp.status_code,
                "error": resp.text[:500], "elapsed": round(elapsed, 2),
            }
        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        usage = data.get("usage", {})
        return {
            "tag": tag, "ok": True, "status": 200,
            "content": msg.get("content"),
            "reasoning_content": msg.get("reasoning_content"),
            "tool_calls": msg.get("tool_calls"),
            "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0),
            "usage": usage,
            "elapsed": round(elapsed, 2),
        }
    except Exception as exc:
        return {"tag": tag, "ok": False, "status": -1,
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed": round(time.monotonic() - t0, 2)}


def run_complex_multiturn(model: str) -> dict:
    """复杂任务 + 4 轮工具调用 + 完全不传 reasoning_content。

    用真实代码审查任务（强制深度思考），多轮工具调用，每轮都丢掉 reasoning_content。
    """
    print(f"\n{'='*70}")
    print(f"模型 {model}：复杂任务 + 4 轮工具调用（不传 reasoning_content）")
    print(f"{'='*70}")

    files = {
        "/tmp/auth.py": "def login(u,p): return u=='admin' and p=='123456'\n# 明文密码",
        "/tmp/db.py": "import sqlite3\nconn = sqlite3.connect('app.db')\n# 无连接池",
        "/tmp/routes.py": "from flask import Flask\napp=Flask(__name__)\n@app.route('/')\ndef h(): return 'ok'\n# 无认证",
        "/tmp/utils.py": "def hash(s): return s\n# 明文未加密",
    }
    prompts = [
        "请用 read_file 读取 /tmp/auth.py",
        "继续读 /tmp/db.py",
        "继续读 /tmp/routes.py",
        "最后读 /tmp/utils.py，总结这4个文件的安全问题",
    ]

    messages = []
    for i, prompt in enumerate(prompts):
        messages.append({"role": "user", "content": prompt})
        r = call_api(model, messages, tools=[CODE_TOOL], tag=f"{model}-round{i+1}")
        rc_len = len(r.get("reasoning_content") or "")
        rc_tokens = r.get("reasoning_tokens", 0)
        print(f"[{model}-round{i+1}] ok={r['ok']} rc_len={rc_len} rc_tokens={rc_tokens} has_tc={bool(r.get('tool_calls'))}")
        if not r["ok"]:
            print(f"  ❌ 错误: {r.get('error', '')[:300]}")
            err = r.get('error', '')
            if 'reasoning_content' in err:
                print(f"  >>> 复现 reasoning_content 错误！轮次: {i+1}")
                return {"model": model, "result": f"REASONING_400_AT_ROUND_{i+1}", "r": r}
            return {"model": model, "result": f"OTHER_400_AT_ROUND_{i+1}", "error": err[:200]}
        if not r.get("tool_calls"):
            print(f"  round{i+1} 没调工具，跳过")
            continue

        tc = r["tool_calls"][0]
        # 关键：不传 reasoning_content（模拟项目 bug）
        new_msg = {
            "role": "assistant",
            "content": r.get("content") or "",
            "tool_calls": [{
                "id": tc["id"], "type": "function",
                "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]},
            }],
            # 故意不写 reasoning_content
        }
        messages.append(new_msg)
        # 加 tool 结果
        args_str = tc["function"]["arguments"]
        try:
            path = json.loads(args_str).get("path", "")
        except Exception:
            path = ""
        messages.append({
            "role": "tool", "tool_call_id": tc["id"],
            "content": files.get(path, "not found"),
        })

    final_usage = r.get("usage", {}) if r else {}
    print(f"[{model}-final] prompt_tokens={final_usage.get('prompt_tokens', 0)} total={final_usage.get('total_tokens', 0)}")
    return {
        "model": model, "result": "ALL_OK_NO_REASONING",
        "final_prompt_tokens": final_usage.get("prompt_tokens", 0),
    }


def run_extreme_long_thinking(model: str) -> dict:
    """极端场景：强制超长思考（复杂算法题），然后多轮。

    用一个需要深度推理的算法题，强制 reasoning_tokens > 1000。
    """
    print(f"\n{'='*70}")
    print(f"模型 {model}：极端长思考 + 多轮（不传 reasoning_content）")
    print(f"{'='*70}")

    # 复杂算法题（强制长思考）
    hard_prompt = """请分析以下算法的时间复杂度和空间复杂度，并给出优化方案。

```python
def find_duplicates(arr):
    result = []
    for i in range(len(arr)):
        for j in range(i+1, len(arr)):
            if arr[i] == arr[j] and arr[i] not in result:
                result.append(arr[i])
    return result

# 调用 find_duplicates 后，再用 read_file 读取 /tmp/auth.py
```

先用 read_file 读取 /tmp/auth.py，然后分析。"""

    r1 = call_api(
        model,
        [{"role": "user", "content": hard_prompt}],
        tools=[CODE_TOOL],
        tag=f"{model}-extreme-r1",
    )
    rc_tokens = r1.get("reasoning_tokens", 0)
    rc_len = len(r1.get("reasoning_content") or "")
    print(f"[{model}-extreme-r1] ok={r1['ok']} rc_len={rc_len} rc_tokens={rc_tokens}")
    if not r1["ok"]:
        print(f"  ❌ r1 错误: {r1.get('error', '')[:300]}")
        return {"model": model, "result": "BLOCKED_AT_R1"}
    if not r1.get("tool_calls"):
        print(f"  r1 没调工具")
        return {"model": model, "result": "NO_TOOL_CALLS", "rc_tokens": rc_tokens}

    tc1 = r1["tool_calls"][0]
    # 第二轮：不传 reasoning_content（哪怕第一轮 reasoning_tokens 很高）
    msgs = [
        {"role": "user", "content": hard_prompt},
        {
            "role": "assistant",
            "content": r1.get("content") or "",
            "tool_calls": [{
                "id": tc1["id"], "type": "function",
                "function": {"name": tc1["function"]["name"], "arguments": tc1["function"]["arguments"]},
            }],
            # 故意不写 reasoning_content，即使第一轮 rc_tokens > 1000
        },
        {"role": "tool", "tool_call_id": tc1["id"],
         "content": "def login(u,p): return u=='admin' and p=='123456'"},
        {"role": "user", "content": "现在分析两个问题：1) 算法复杂度 2) auth.py 安全问题"},
    ]
    r2 = call_api(model, msgs, tools=[CODE_TOOL], tag=f"{model}-extreme-r2-no-rc")
    print(f"[{model}-extreme-r2] ok={r2['ok']} status={r2['status']} rc_tokens_r1={rc_tokens}")
    if not r2["ok"]:
        err = r2.get('error', '')
        print(f"  ❌ r2 错误: {err[:400]}")
        if 'reasoning_content' in err:
            print(f"  >>> 复现！r1 的 reasoning_tokens={rc_tokens}，不传 rc 就报错")
            return {"model": model, "result": "REASONING_400_WITH_LONG_THINKING",
                    "r1_rc_tokens": rc_tokens}
        return {"model": model, "result": "OTHER_400", "error": err[:200]}
    print(f"  ✅ r2 content: {(r2.get('content') or '')[:150]}")
    return {"model": model, "result": "OK_DESPITE_NO_RC", "r1_rc_tokens": rc_tokens}


def main() -> int:
    if not API_KEY:
        print("ERROR", file=sys.stderr)
        return 1

    results = []
    # 对比测试：flash 和 pro 都跑
    for model in ["deepseek-v4-flash", "deepseek-v4-pro"]:
        print(f"\n{'#'*70}")
        print(f"# 开始测试 {model}")
        print(f"{'#'*70}")
        results.append(run_complex_multiturn(model))
        results.append(run_extreme_long_thinking(model))

    print("\n" + "=" * 70)
    print("总结对比")
    print("=" * 70)
    for r in results:
        model = r.get('model', '?')
        result = r.get('result', '?')
        extra = ""
        if 'r1_rc_tokens' in r:
            extra = f" (r1_rc_tokens={r['r1_rc_tokens']})"
        if 'final_prompt_tokens' in r:
            extra = f" (final_prompt={r['final_prompt_tokens']})"
        print(f"  [{model}] {result}{extra}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
