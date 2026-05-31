"""真实 LLM 调用测试 — 验证上下文组装并调用大模型。

测试内容：
1. 启动完整的 pipeline 插件链
2. 验证 prompt_build 产出 system_message + dynamic_vars
3. 验证 LLMCore._build_messages 组装顺序
4. 调用真实 LLM 并获取响应
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import os
import pytest
from pathlib import Path
from typing import Any

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

# 精简第三方库的日志
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
logging.getLogger("LiteLLM.proxy").setLevel(logging.WARNING)
logging.getLogger("LiteLLM.router").setLevel(logging.WARNING)
logging.getLogger("LiteLLM.litellm_logging").setLevel(logging.WARNING)
logging.getLogger("LiteLLM.http_handler").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def _set_env():
    """设置环境变量（从 llm.yaml 读取的值）。"""
    os.environ.setdefault("MINIMAX_API_KEY",
        "[REDACTED]")


@pytest.mark.integration
async def test_assembly_and_llm_call():
    """测试完整的上下文组装 + 真实 LLM 调用。"""
    _set_env()

    from config.models import ModelConfigLoader
    from pipeline.config import build_plugin_registry, load_pipeline_config
    from pipeline.plugin import PluginContext
    from pipeline.chain import PluginChain
    from pipeline.types import StateKeys
    from plugins.core.llm_core import LLMCore

    config_path = _PROJECT_ROOT / "config" / "pipelines" / "default.yaml"
    model_loader = ModelConfigLoader()

    # 1. 加载管道配置
    logger.info("=" * 60)
    logger.info("步骤 1: 加载管道配置")
    pipeline_config = load_pipeline_config(config_path, model_loader=model_loader)
    plugin_registry = build_plugin_registry(pipeline_config)
    logger.info("管道配置加载完成: name=%s", pipeline_config.name)

    # 2. 构建服务
    logger.info("=" * 60)
    logger.info("步骤 2: 构建服务")
    services = _build_minimal_services()
    logger.info("服务构建完成: %s", list(services.keys()))

    # 3. 加载 Agent 配置
    logger.info("=" * 60)
    logger.info("步骤 3: 加载 Agent 配置")
    from agents.registry import AgentRegistry
    agent_registry = AgentRegistry()
    agent_config_dir = _PROJECT_ROOT / "config" / "agents"
    agent_registry.load_directory(agent_config_dir)
    agent_config = agent_registry.get("lingxi") or agent_registry.get("default")
    if agent_config:
        logger.info("Agent: %s (%s)", agent_config.config_id, agent_config.display_name)
    else:
        logger.warning("未找到 Agent 配置，使用默认值")
        agent_config = None

    # 4. 构建 Agent state
    logger.info("=" * 60)
    logger.info("步骤 4: 构建 Agent state")
    state: dict[str, Any] = {
        StateKeys.ITERATION: 0,
        StateKeys.ENDED: False,
        "user_input": "你好，请用一句话介绍你自己",
        "messages": [{"role": "user", "content": "你好，请用一句话介绍你自己"}],
    }
    if agent_config and hasattr(agent_config, "to_state"):
        agent_state = agent_config.to_state()
        state.update(agent_state)
        logger.info("Agent state keys: %s", [k for k in agent_state.keys() if not k.startswith("_")])

    # 5. 执行 Input 插件链
    logger.info("=" * 60)
    logger.info("步骤 5: 执行 Input 插件链")
    input_route_table = pipeline_config.input_route_table
    plugin_names, target = input_route_table.resolve(state)
    logger.info("Input 路由解析: plugins=%s, target=%s", plugin_names, target)

    input_plugins = []
    for name in plugin_names:
        plugin = plugin_registry.get(name)
        if plugin is not None:
            input_plugins.append(plugin)
            logger.info("  Input 插件: %s (%s)", name, type(plugin).__name__)

    if input_plugins:
        input_ctx = PluginContext(state=dict(state), config={}, _services=services)
        input_chain = PluginChain(input_plugins)
        input_results = await input_chain.execute(input_ctx)
        for result in input_results:
            if result.state_updates:
                state.update(result.state_updates)

    # 6. 验证 prompt_build 产出
    logger.info("=" * 60)
    logger.info("步骤 6: 验证 prompt_build 产出")
    system_message = state.get("system_message")
    dynamic_vars = state.get("prompt.dynamic_vars", "")
    if dynamic_vars and isinstance(dynamic_vars, dict):
        dynamic_vars_text = dynamic_vars.get("content", "")
    else:
        dynamic_vars_text = str(dynamic_vars)
    tool_schemas = state.get("tool_schemas", [])

    if system_message:
        logger.info("✅ system_message 存在")
        logger.info("  role: %s", system_message.get("role"))
        content = system_message.get("content", "")
        logger.info("  content 长度: %d 字符", len(content))
        logger.info("  content 前 300 字符:\n%s", content[:300])
        logger.info("  ...")
        logger.info("  content 后 200 字符:\n%s", content[-200:])
    else:
        logger.error("❌ system_message 不存在！")

    if dynamic_vars:
        logger.info("✅ dynamic_vars 存在")
        logger.info("  内容: %s", dynamic_vars_text)
    else:
        logger.warning("⚠️ dynamic_vars 为空")

    if tool_schemas:
        logger.info("✅ tool_schemas 存在 (%d 个)", len(tool_schemas))
    else:
        logger.info("ℹ️ tool_schemas 为空（无工具注册）")

    # 7. LLMCore._build_messages 组装
    logger.info("=" * 60)
    logger.info("步骤 7: LLMCore._build_messages 组装")
    llm_core = plugin_registry.get_core("llm_call")
    if llm_core is None:
        # 手动创建
        llm_core = LLMCore(config={
            "provider": "minimax",
            "model_name": "MiniMax-M2.7",
            "api_base": "https://api.minimaxi.com/v1",
            "api_key": os.environ.get("MINIMAX_API_KEY", ""),
            "default_params": {"temperature": 0.7, "max_tokens": 1024},
        })

    messages = llm_core._build_messages(state)
    logger.info("最终 messages 数量: %d", len(messages))

    for i, msg in enumerate(messages):
        role = msg.get("role", "unknown")
        content = str(msg.get("content", ""))
        logger.info("  [%d] role=%s | content_len=%d | preview=%.100s", i, role, len(content), content[:100])

    # 验证组装顺序
    logger.info("=" * 60)
    logger.info("步骤 8: 验证组装顺序")
    errors = []
    if len(messages) < 3:
        errors.append(f"messages 数量不足 3 条（期望: system + user + dynamic），实际 {len(messages)}")
    else:
        if messages[0].get("role") != "system":
            errors.append(f"第一条应该是 system，实际是 {messages[0].get('role')}")
        if messages[1].get("role") != "user":
            errors.append(f"第二条应该是 user，实际是 {messages[1].get('role')}")
        if messages[-1].get("role") == "system" and dynamic_vars:
            # 最后一条是 dynamic_vars
            if "日期" not in messages[-1].get("content", ""):
                errors.append("最后一条 system 消息应包含动态变量")

    if errors:
        for e in errors:
            logger.error("❌ %s", e)
    else:
        logger.info("✅ 组装顺序验证通过")

    # 9. 真实 LLM 调用
    logger.info("=" * 60)
    logger.info("步骤 9: 真实 LLM 调用")
    try:
        core_ctx = PluginContext(state=dict(state), config={}, _services=services)
        result = await llm_core.execute(core_ctx)

        raw_result = result.get(StateKeys.RAW_RESULT, "")
        raw_error = result.get(StateKeys.RAW_ERROR)
        raw_tool_calls = result.get(StateKeys.RAW_TOOL_CALLS)
        updated_messages = result.get("messages", [])

        if raw_error:
            logger.error("❌ LLM 调用失败: %s", raw_error)
        else:
            logger.info("✅ LLM 调用成功")
            if raw_result:
                logger.info("  回复内容 (%d 字符):", len(raw_result))
                logger.info("  %s", raw_result[:500])
            if raw_tool_calls:
                logger.info("  工具调用: %s", json.dumps(raw_tool_calls, ensure_ascii=False, indent=2))
            logger.info("  更新后 messages 数量: %d", len(updated_messages))

            # 验证 messages 不包含 system
            system_in_messages = any(m.get("role") == "system" for m in updated_messages)
            if system_in_messages:
                logger.error("❌ state['messages'] 不应包含 system 消息！")
            else:
                logger.info("✅ state['messages'] 不包含 system 消息（正确）")

    except Exception as exc:
        logger.error("❌ LLM 调用异常: %s — %s", type(exc).__name__, exc)
        import traceback
        traceback.print_exc()

    logger.info("=" * 60)
    logger.info("测试完成")


def _build_minimal_services() -> dict[str, Any]:
    """构建最小化服务集合。"""
    services: dict[str, Any] = {}

    try:
        from memory.storage.json_store import JsonMemoryStore
        json_store = JsonMemoryStore()
        services["memory_store"] = json_store
        services["semantic_storage"] = json_store
        services["retriever"] = json_store
        logger.info("服务: JsonMemoryStore")
    except Exception as exc:
        logger.warning("JsonMemoryStore 创建失败: %s", exc)

    try:
        from memory.tag_service import TagService
        tag_service = TagService(content_store=services.get("memory_store"))
        services["tag_service"] = tag_service
        logger.info("服务: TagService")
    except Exception as exc:
        logger.warning("TagService 创建失败: %s", exc)

    try:
        from memory.chunk_service import ChunkService
        chunk_service = ChunkService(content_store=services.get("memory_store"))
        services["chunk_service"] = chunk_service
        logger.info("服务: ChunkService")
    except Exception as exc:
        logger.warning("ChunkService 创建失败: %s", exc)

    return services


if __name__ == "__main__":
    asyncio.run(test_assembly_and_llm_call())
