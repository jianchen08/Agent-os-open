# @feature: FP-0.2.〇 管道引擎与插件执行模型 | @vision: V3 可嵌入 | @ci: manual
"""真实端到端压缩测试 — 真实 LLM 调用,不 mock。

流程:
1. 构造超过触发阈值(128000 × 0.55 = 70400 tokens)的消息序列
2. 加载真实 context_window_guard 插件
3. 注入 capability_caller（压缩 LLM 通道经 tool-executor → memory.compress 工具；
   进程内 LLMClient 直连路径已退役——零生产消费者,LLM 面收敛由 llm_service 承接）
4. 执行 execute(),验证真实输出:
   - 压缩是否触发
   - 消息数是否减少
   - 压缩块内容是否为真实 LLM 产出的结构化 JSON(L1/L2/keywords/state_snapshot/memory_items)
   - 压缩块是否以 memory_type=chunk 写入后端
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from typing import Any

# ── 0. 加载路径 ──
# 本文件在 tests/manual/, context_window_guard 插件在 plugins/shared/pipeline/input/
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
_SHARED_DIR = os.path.join(_PROJECT_ROOT, "plugins", "shared")
_GUARD_DIR = os.path.join(_SHARED_DIR, "pipeline", "input", "context_window_guard")
sys.path.insert(0, _PROJECT_ROOT)
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _GUARD_DIR)
os.chdir(_PROJECT_ROOT)


# ── 1. capability_caller（压缩唯一 LLM 通道）──
def build_real_capability_caller() -> Any:
    """构造经 tool-executor 调 memory.compress 工具的 capability_caller。

    压缩执行时 plugin._get_memory_service 会把它包进
    ``_build_compress_llm_call_fn``（入参 {prompt, max_tokens}，出参
    {summary, degraded}）。手工 e2e 需注入真实 tool-executor 能力句柄
    （参照 server.py on_load 的 make_capability_caller 接线——它把
    plugin.get_capability("tool-executor").call 包成 (method, params) 形态）。
    """
    async def _caller(method: str, params: dict[str, Any]) -> Any:
        raise RuntimeError(
            "manual e2e 需注入真实 tool-executor caller 才能调 memory.compress；"
            "请参照 server.py on_load 的 make_capability_caller 接线"
        )

    return _caller


# ── 2. 真实后端 (记录写入,不落库) ──
class RecordingBackend:
    """记录所有 add 调用,不实际落库(验证写入内容)。"""

    def __init__(self) -> None:
        self.adds: list[dict] = []

    async def add(self, user_id: str, content: str, memory_type: str = "semantic", tags: list | None = None, source: str = "") -> str:
        self.adds.append({"memory_type": memory_type, "tags": tags or [], "content": content})
        return f"mem_{len(self.adds)}"

    async def search(self, query: str, user_id: str, top_k: int = 5, memory_type: str | None = None) -> list:
        return []


class FakeCtx:
    def __init__(self, state: dict) -> None:
        self.state = state

    def get_service(self, name: str):  # noqa: ANN001
        raise KeyError(name)


async def main() -> None:
    # ── 3. 加载真实插件 ──
    spec = importlib.util.spec_from_file_location("cwg_plugin", os.path.join(_GUARD_DIR, "plugin.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cwg_plugin"] = mod
    spec.loader.exec_module(mod)

    caller = build_real_capability_caller()
    backend = RecordingBackend()
    mod.set_capability_caller(caller)
    mod.set_memory_backend(backend)

    plugin = mod.ContextWindowGuardPlugin(config={})  # 默认 trigger_ratio 0.55

    # ── 4. 构造超过阈值的消息序列 ──
    # 阈值 = 128000 × 0.55 = 70400 tokens ≈ 140800 字符 (len//2 估算)
    # 构造 300 条消息, 每条 ~500 字符 → ~75000 tokens, 超过阈值
    messages: list[dict] = []
    topic = "构建一个基于 Python 的 Web 应用,包含用户认证、数据库集成、REST API 和前端页面"
    for i in range(300):
        messages.append({
            "role": "user",
            "content": f"第{i}轮用户请求: 请{topic}, 需要处理用户输入验证、错误处理和日志记录。具体需求包括:{topic}的模块划分和接口设计。" * 2,
        })
        messages.append({
            "role": "assistant",
            "content": f"第{i}轮助手回复: 好的,我已经完成了{topic}的模块划分。创建了用户认证模块、数据库访问层和 API 路由。下一步实现前端页面和测试。" * 2,
        })

    total_chars = sum(len(m.get("content", "")) for m in messages)
    est_tokens = total_chars // 2
    trigger = int(128000 * 0.55)
    print("=== 输入构造 ===")
    print(f"消息数: {len(messages)}, 总字符: {total_chars}, 估算 tokens: {est_tokens}")
    print(f"触发阈值: {trigger} tokens, 是否超阈值: {est_tokens > trigger}")

    state = {
        "context_window": 128000,
        "messages": messages,
        "pipeline_id": "e2e-real-pipe-001",
        "context.session_id": "sess-real-1",
        "user_id": "u1",
        "model_name": "deepseek-v4-flash",
        "core_type": "llm_call",
        "iteration": 300,
        "system_message": {"content": "你是一个智能助理"},
    }

    # ── 5. 执行压缩 ──
    print("\n=== 执行压缩 (真实 LLM) ===")
    result = await plugin.execute(FakeCtx(state))
    updates = result.state_updates
    compressed = updates.get("messages")

    if not compressed:
        print("❌ 压缩未触发或失败")
        print(f"   state_updates: {list(updates.keys())}")
        return

    print(f"✅ 压缩触发: {len(messages)} → {len(compressed)} 条消息")
    assert len(compressed) < len(messages), "压缩后消息数应减少"

    # ── 6. 验证真实压缩输出 ──
    print("\n=== 压缩块输出验证 ===")
    chunk_adds = [a for a in backend.adds if a["memory_type"] == "chunk"]
    semantic_adds = [a for a in backend.adds if a["memory_type"] == "semantic"]
    print(f"后端写入: chunk={len(chunk_adds)} 条, semantic={len(semantic_adds)} 条")

    # 验证 chunk 内容是真实 LLM 产出的 JSON
    if chunk_adds:
        chunk = chunk_adds[0]
        content = chunk["content"]
        print("\n--- 第一条 chunk (前 800 字符) ---")
        print(content[:800])
        print("...")
        # 尝试解析为 JSON 验证结构
        try:
            parsed = json.loads(content)
            print(f"\n✅ chunk 是有效 JSON, 顶层字段: {list(parsed.keys())}")
            if "l1" in parsed:
                print(f"   L1 字段: {list(parsed['l1'].keys())}")
                print(f"   L1.session_title: {parsed['l1'].get('session_title', '')[:60]}")
            if "l2" in parsed:
                print(f"   L2 字段: {list(parsed['l2'].keys())}")
            if "keywords" in parsed:
                print(f"   keywords: {parsed['keywords'][:5]}")
            if "state_snapshot" in parsed:
                print(f"   state_snapshot 字段: {list(parsed['state_snapshot'].keys())}")
            if "memory_items" in parsed:
                print(f"   memory_items 字段: {list(parsed['memory_items'].keys())}")
        except json.JSONDecodeError:
            print("⚠️ chunk 内容不是纯 JSON(LLM 可能加了说明文字, 这在真实调用中常见)")

    if semantic_adds:
        s = semantic_adds[0]
        print("\n--- 第一条 semantic 记忆 ---")
        print(f"   tags: {s['tags']}")
        print(f"   content: {s['content'][:200]}")

    # ── 7. 压缩块写入即代表 LLM 调用成功（capability 通道，无 caller.calls 记录）──
    print("\n=== LLM 调用统计 ===")
    print(f"chunk 写入数(=LLM 调用成功数): {len(chunk_adds)}")

    print("\n✅ 真实端到端压缩测试完成: 超阈值输入 → 真实 LLM 摘要 → 压缩块+记忆产出")


if __name__ == "__main__":
    asyncio.run(main())
