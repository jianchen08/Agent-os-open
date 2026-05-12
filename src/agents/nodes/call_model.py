"""
调用 LLM 模型节点

提供 LangGraph StateGraph 中的 call_model_node 函数
"""

import json
import logging
from typing import Any

from langgraph.types import RunnableConfig

from src.agents.state import AgentState
from src.core.tokenizer import get_token_counter
from src.memory.builders.context_builder import ContextBuilder
from src.orchestration.concurrency_manager import ConcurrencyManager

logger = logging.getLogger(__name__)


def _build_tools_description(tools: list[Any]) -> str:
    """
    构建工具描述文本（用于第1层上下文）

    使用标准 OpenAI Function Calling JSON 格式，包含完整的工具定义和参数信息。
    这是 LLM 训练时学习的标准格式，能确保工具被正确识别和调用。

    Args:
        tools: 工具对象列表

    Returns:
        JSON 格式的工具描述字符串（不包含标题前缀，由 ContextBuilder 添加）
    """
    if not tools:
        return ""

    tools_list = []
    for tool in tools:
        # 获取工具的基本信息
        tool_name = getattr(tool, 'name', str(tool))
        tool_desc = getattr(tool, 'description', '无描述')

        # 获取工具的参数 schema
        # 优先尝试 input_schema（Tool 类型），然后是 args_schema（StructuredTool 类型）
        input_schema = None

        # 1. 尝试获取 input_schema（Tool 类型对象）
        raw_schema = getattr(tool, 'input_schema', None)
        if raw_schema and isinstance(raw_schema, dict):
            input_schema = raw_schema
            logger.debug(f"[_build_tools_description] 工具 {tool_name} 使用 input_schema（字典）")

        # 2. 如果没有 input_schema，尝试 args_schema（StructuredTool 类型）
        if input_schema is None:
            args_schema = getattr(tool, 'args_schema', None)
            if args_schema:
                # args_schema 是 Pydantic 模型类，需要调用 .schema() 获取 JSON schema
                if hasattr(args_schema, 'schema'):
                    try:
                        input_schema = args_schema.schema()
                        logger.debug(f"[_build_tools_description] 工具 {tool_name} 使用 args_schema.schema()")
                    except Exception as e:
                        logger.warning(f"[_build_tools_description] 工具 {tool_name} 调用 args_schema.schema() 失败: {e}")
                else:
                    logger.warning(f"[_build_tools_description] 工具 {tool_name} 的 args_schema 没有 schema() 方法")

        # 3. 如果仍然没有 schema，尝试从工具定义获取（通过 tool_registry）
        if input_schema is None:
            logger.warning(f"[_build_tools_description] 工具 {tool_name} 无法获取 schema，尝试从注册表获取")
            try:
                pass
                # 尝试从工具注册表获取工具定义
                # 这里可能需要通过其他方式获取，暂时记录警告
            except Exception:
                pass

        # 确保 schema 是字典格式
        if not input_schema or not isinstance(input_schema, dict):
            logger.warning(f"[_build_tools_description] 工具 {tool_name} 的 schema 无效，使用默认空 schema")
            input_schema = {
                "type": "object",
                "properties": {},
                "required": []
            }

        # 验证 schema 结构
        if 'type' not in input_schema:
            input_schema['type'] = 'object'
        if 'properties' not in input_schema:
            input_schema['properties'] = {}
        if 'required' not in input_schema:
            input_schema['required'] = []

        logger.debug(f"[_build_tools_description] 工具 {tool_name} 最终 schema 键: {list(input_schema.keys())}, properties: {list(input_schema.get('properties', {}).keys())}")

        # 构建标准 Function Calling 格式
        tool_def = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_desc,
                "parameters": input_schema
            }
        }
        tools_list.append(tool_def)

    if not tools_list:
        return ""

    # 返回 JSON 格式，便于 LLM 解析
    # 标题由 ContextBuilder._build_tools_description 统一添加
    return json.dumps({"tools": tools_list}, ensure_ascii=False, indent=2)


async def call_model_node(
    state: AgentState, config: RunnableConfig | None = None
) -> dict[str, Any]:
    """
    调用 LLM 模型节点

    Args:
        state: 当前状态
        config: 运行时配置（包含 configurable 中的运行时对象）

    Returns:
        状态更新字典
    """
    # 优先从 configurable 获取运行时对象（避免序列化问题）
    if config and "configurable" in config:
        llm_client = config["configurable"].get("llm_client")
        config["configurable"].get("tool_executor")
        layered_context_store = config["configurable"].get("layered_context_store")
    else:
        # 回退到从 state 获取（向后兼容）
        llm_client = state.get("llm_client")
        state.get("tool_executor")
        layered_context_store = state.get("layered_context_store")

    tools = state.get("tools", [])
    iteration = state.get("iteration", 0)
    enable_thinking = state.get("enable_thinking", False)
    output_schema = state.get("output_schema")

    # 获取执行上下文（用于数据库访问）
    context = state.get("context", {})
    session_id = context.get("session_id", "") if isinstance(context, dict) else ""

    # 构建消息列表 - 只使用 LayeredContextStore
    # 不再从 state["messages"] 读取消息
    if not layered_context_store:
        logger.error("[Agent.call_model] layered_context_store 未提供，无法构建消息")
        return {
            "error": "layered_context_store 未提供，无法构建消息",
            "should_stop": True,
        }

    try:
        logger.info("[Agent.call_model] 使用 ContextBuilder 构建消息")

        # 生成工具描述并设置到 LayeredContextStore（第1层）
        if tools and hasattr(layered_context_store, 'set_tools_description'):
            tools_description = _build_tools_description(tools)
            if tools_description:
                layered_context_store.set_tools_description(tools_description)
                logger.info(f"[Agent.call_model] 工具描述已设置到 LayeredContextStore | tools_count={len(tools)}")
                # 调试日志：记录工具描述格式
                logger.debug(f"[Agent.call_model] 工具描述内容预览: {tools_description[:500]}...")

        # 注意：压缩已移到 user_input.py 中，在 Agent Loop 流式执行之前调用
        # 这样可以避免压缩响应被 LangGraph 的 astream 捕获并发送到前端

        # 使用新的 ContextBuilder 构建完整上下文
        # 包含：系统提示 + L3/L2/L1摘要 + 完整消息历史（从数据库加载）
        # 传递 agent_config 以便根据配置决定是否添加动态变量
        agent_config = state.get("agent_config")

        # 调试日志：检查历史消息加载前的状态
        if hasattr(layered_context_store, '_messages'):
            all_messages = getattr(layered_context_store, '_messages', [])
            executor_id = getattr(layered_context_store, 'executor_id', None)
            logger.info(
                f"[Agent.call_model] LayeredContextStore 消息统计 | "
                f"总消息数={len(all_messages)} | "
                f"当前 executor_id={executor_id}"
            )
            # 统计当前 executor 的消息数
            if executor_id:
                executor_msgs = [m for m in all_messages if m.get('executor_id') == executor_id]
                logger.info(f"[Agent.call_model] 当前 executor 的消息数={len(executor_msgs)}")

        context_builder = ContextBuilder()
        logger.info(f"[Agent.call_model] 调用 ContextBuilder.build() | layered_context_store存在={layered_context_store is not None}")

        # 检查 LayeredContextStore 中的消息状态
        if hasattr(layered_context_store, '_messages'):
            all_msgs = getattr(layered_context_store, '_messages', [])
            logger.info(f"[Agent.call_model] LayeredContextStore._messages | 总数={len(all_msgs)}")
            for idx, msg in enumerate(all_msgs):
                logger.info(f"[Agent.call_model]   消息 #{idx} | role={msg.get('role')} | executor_id={msg.get('executor_id')} | content={msg.get('content', '')[:50]}...")

        messages, context_parts = await context_builder.build(layered_context_store, agent_config=agent_config)

        logger.info(f"[Agent.call_model] ContextBuilder.build() 完成 | messages数量={len(messages)}")
        for idx, msg in enumerate(messages):
            logger.info(f"[Agent.call_model]   构建后消息 #{idx} | type={type(msg).__name__} | content={getattr(msg, 'content', '')[:50]}...")

        if messages:
            # 统计各类型消息数量
            system_msg_count = sum(1 for m in messages if type(m).__name__ == 'SystemMessage')
            human_msg_count = sum(1 for m in messages if type(m).__name__ == 'HumanMessage')
            ai_msg_count = sum(1 for m in messages if type(m).__name__ == 'AIMessage')
            tool_msg_count = sum(1 for m in messages if type(m).__name__ == 'ToolMessage')
            logger.info(
                f"[Agent.call_model] 消息构建成功 | "
                f"总消息数={len(messages)} | "
                f"系统消息={system_msg_count} | "
                f"用户消息={human_msg_count} | "
                f"AI消息={ai_msg_count} | "
                f"工具消息={tool_msg_count}"
            )
        else:
            logger.error("[Agent.call_model] LayeredContextStore 返回空消息列表")
            return {
                "error": "LayeredContextStore 返回空消息列表",
                "should_stop": True,
            }

        # 检查工具描述开关配置
        # 从上下文窗口配置读取开关，控制数据流向：
        # - true: 工具描述加入提示词，不传入 API 的 tools 字段
        # - false: 工具描述不加入提示词，传入 API 的 tools 字段
        from src.config.system_config import get_system_config_manager
        manager = get_system_config_manager()
        context_config = manager.load_context_window_config()
        include_tools_in_prompt = context_config.get("include_tools_description_in_prompt", True)

        # 检查工具描述是否已在上下文中（第1层）
        tools_in_context = any(
            part.get("section") == "工具描述"
            for part in context_parts
        )

        if tools_in_context or include_tools_in_prompt:
            # 工具描述在提示词中，不需要再传给 API
            logger.info(f"[Agent.call_model] 工具描述在提示词中（开关={include_tools_in_prompt}），跳过工具绑定")
            tools = []
        else:
            logger.info(f"[Agent.call_model] 工具描述不在提示词中（开关={include_tools_in_prompt}），使用工具绑定 | tools_count={len(tools)}")

    except Exception as e:
        logger.error(f"[Agent.call_model] 消息构建失败 | error={e}")
        return {
            "error": f"消息构建失败: {str(e)}",
            "should_stop": True,
        }

    logger.info(f"[Agent.call_model] 最终消息列表 | messages_count={len(messages)}")

    # 关键检查：确保 messages 不为空
    if not messages:
        logger.error("[Agent.call_model] messages 列表为空，无法调用 LLM")
        return {
            "error": "messages 列表为空，无法调用 LLM",
            "should_stop": True,
        }

    # 验证每条消息的内容
    for i, msg in enumerate(messages):
        content = getattr(msg, "content", None)
        if not content or (isinstance(content, str) and not content.strip()):
            logger.warning(
                f"[Agent.call_model] 消息 {i} 的 content 为空: "
                f"type={type(msg).__name__}, content={content}"
            )

    # 记录最近几条消息的类型,用于调试
    if messages:
        recent_types = [type(msg).__name__ for msg in messages[-5:]]
        logger.info(f"[Agent.call_model] 最近5条消息类型: {recent_types}")

    logger.info(
        f"[Agent.call_model] 节点入口 | "
        f"iteration={iteration} | "
        f"messages_count={len(messages)} | "
        f"tools_count={len(tools)} | "
        f"enable_thinking={enable_thinking} | "
        f"use_layered_context={layered_context_store is not None}"
    )

    if not llm_client:
        logger.error("[Agent.call_model] LLM 客户端未初始化")
        return {
            "error": "LLM 客户端未初始化",
            "should_stop": True,
        }

    try:
        # 记录调用信息
        logger.info(
            f"[Agent.call_model] 准备调用 LLM | "
            f"iteration={iteration} | "
            f"messages_count={len(messages)} | "
            f"tools_count={len(tools)} | "
            f"思考模式={enable_thinking}"
        )

        # 记录消息摘要
        if messages:
            last_msg = messages[-1]
            msg_content = getattr(last_msg, "content", str(last_msg))
            if msg_content and len(msg_content) > 200:
                msg_content = msg_content[:200] + "..."
            logger.debug(
                f"[Agent.call_model] 最后消息 | "
                f"type={type(last_msg).__name__} | "
                f"content={msg_content}"
            )

        # 记录工具列表
        if tools:
            tool_names = [getattr(t, "name", str(t)) for t in tools]
            logger.debug(f"[Agent.call_model] 可用工具 | tools={tool_names}")

        # 调用 LLM
        config = {}

        # 处理结构化输出
        # 注意：只有部分 LLM 提供商支持 json_schema 类型的 response_format
        # 不支持的提供商：智谱 (zhipu)、深度求索 (deepseek) 等
        if output_schema:
            # 检查模型提供商是否支持结构化输出
            supports_json_schema = True

            # 从 llm_client 获取模型名并检查提供商
            # 注意：llm_client 可能是 LLMClientAdapter，需要访问其 client 属性
            if llm_client:
                # 尝试从适配器获取实际客户端的模型名
                if hasattr(llm_client, "client"):
                    model_name = getattr(llm_client.client, "model_name", "")
                    logger.debug(
                        f"[Agent.call_model] 从适配器获取模型名 | "
                        f"llm_client类型={type(llm_client).__name__} | "
                        f"client类型={type(llm_client.client).__name__} | "
                        f"model_name={model_name}"
                    )
                else:
                    model_name = getattr(llm_client, "model_name", "")
                    logger.debug(
                        f"[Agent.call_model] 直接获取模型名 | "
                        f"llm_client类型={type(llm_client).__name__} | "
                        f"model_name={model_name}"
                    )

                # 不支持 json_schema 的提供商列表
                unsupported_providers = ["zhipu", "deepseek", "glm"]
                if any(
                    provider in model_name.lower() for provider in unsupported_providers
                ):
                    supports_json_schema = False
                    logger.info(
                        f"[Agent.call_model] 模型 {model_name} 不支持 json_schema，跳过结构化输出"
                    )
                else:
                    logger.debug(
                        f"[Agent.call_model] 模型 {model_name} 支持结构化输出 | "
                        f"unsupported_providers={unsupported_providers}"
                    )

            if supports_json_schema:
                config["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "agent_response",
                        "strict": True,
                        "schema": output_schema,
                    },
                }
                logger.info(
                    f"[Agent.call_model] 启用结构化输出 | "
                    f"schema={output_schema.get('properties', {}).keys()}"
                )

        # 处理思考模式
        # 简单直接：enable_thinking=True 就切换到思考模型
        thinking_callback = state.get("thinking_callback")
        has_tools = len(tools) > 0

        # 调试日志：追踪思考模式状态
        logger.info(
            f"[Agent 节点] 思考模式检查 | "
            f"enable_thinking={enable_thinking} | "
            f"has_tools={has_tools} | "
            f"工具数量={len(tools)}"
        )

        if enable_thinking:
            agent_config = state.get("agent_config")
            logger.info(
                f"[Agent 节点] Agent 配置检查 | "
                f"agent_config存在={agent_config is not None}"
            )
            if agent_config:
                can_think = agent_config.can_use_thinking_mode()
                logger.info(f"[Agent 节点] can_use_thinking_mode={can_think}")
                if can_think:
                    thinking_model = agent_config.get_model_name(enable_thinking=True)
                    normal_model = agent_config.get_model_name(enable_thinking=False)
                    logger.info(
                        f"[Agent 节点] 模型配置 | "
                        f"normal_model={normal_model} | "
                        f"thinking_model={thinking_model}"
                    )

                    # 简单逻辑：如果思考模型不同，就切换
                    # 现代思考模型（如 deepseek-reasoner）支持工具调用，无需限制
                    if thinking_model != normal_model:
                        # 切换到思考模型（支持工具调用）
                        logger.info(
                            f"[Agent 节点] 切换到思考模型 | "
                            f"{normal_model} -> {thinking_model} | "
                            f"工具数量: {len(tools)}"
                        )
                        try:
                            from src.core.di import get_global_container

                            container = get_global_container()
                            llm_factory = container.get("llm_factory")
                            thinking_client = llm_factory.get_client(thinking_model)
                            llm_client = thinking_client.as_langchain()
                            logger.info(
                                f"[Agent 节点] 思考模型客户端创建成功 | "
                                f"adapter={type(llm_client).__name__}"
                            )
                        except Exception as e:
                            logger.error(f"[Agent 节点] 思考模型客户端创建失败: {e}")
                    else:
                        # 同一个模型，可能是 parameter_switch 类型
                        # 添加思考模式参数
                        thinking_params = agent_config.get_model_params(
                            enable_thinking=True
                        )
                        if thinking_params:
                            config.update(thinking_params)
                            logger.info(
                                f"[Agent 节点] 应用思考模式参数 | "
                                f"model={thinking_model} | params={thinking_params}"
                            )
                else:
                    logger.info("[Agent 节点] Agent 配置不支持思考模式，使用普通模型")

        # 检查是否使用流式调用
        # 智谱原生客户端的 astream_with_thinking 目前不支持工具调用
        # 当有工具时，使用标准的 bind_tools + ainvoke 方式（思考模式参数已在上面设置）
        use_streaming = (
            enable_thinking
            and thinking_callback
            and not tools  # 有工具时使用 bind_tools 方式
            and hasattr(llm_client, "_client")  # 检查是否有原生客户端
            and llm_client._client is not None  # 确保原生客户端存在
            and hasattr(llm_client, "astream_with_thinking")  # 检查是否支持流式思考
        )

        if use_streaming:
            # 流式调用 LLM，实时发送思考内容（无工具场景）
            logger.info("[Agent 节点] 使用流式调用（思考模式，无工具）")

            # 获取 LLM 并发控制信号量（根据提供商）
            provider = getattr(llm_client, "_provider", "default")
            if hasattr(llm_client, "provider"):
                provider = llm_client.provider
            semaphore = ConcurrencyManager().get_llm_semaphore(provider)

            response_content = ""
            thinking_content = ""

            async with semaphore:
                logger.debug("[Agent 节点] 获取信号量成功 | 当前并发受限")
                async for chunk in llm_client.astream_with_thinking(messages, **config):
                    # 提取思考内容
                    if hasattr(chunk, "additional_kwargs"):
                        reasoning = chunk.additional_kwargs.get("reasoning_content", "")
                        if reasoning and reasoning not in thinking_content:
                            thinking_content += reasoning
                            # 通过回调发送思考内容
                            try:
                                await thinking_callback(reasoning)
                            except Exception as e:
                                logger.warning(f"[Agent 节点] 思考回调失败: {e}")

                    # 提取普通内容
                    if hasattr(chunk, "content") and chunk.content:
                        response_content += chunk.content

            # 构建响应对象
            from langchain_core.messages import AIMessage

            response = AIMessage(content=response_content)
        elif tools:
            # 绑定工具后调用（思考模式参数通过 config 传递）
            logger.info(
                f"[Agent 节点] 使用工具调用模式 | "
                f"tools_count={len(tools)} | "
                f"enable_thinking={enable_thinking} | "
                f"config_keys={list(config.keys())}"
            )

            # 获取 LLM 并发控制信号量（根据提供商）
            provider = getattr(llm_client, "_provider", "default")
            if hasattr(llm_client, "provider"):
                provider = llm_client.provider
            semaphore = ConcurrencyManager().get_llm_semaphore(provider)

            # 记录工具绑定前的状态
            tool_names = [getattr(t, "name", str(t)) for t in tools]
            logger.info(
                f"[Agent 节点] 工具绑定前 | "
                f"工具列表={tool_names} | "
                f"llm_client_type={type(llm_client).__name__}"
            )

            # 关键安全检查：确保当前 llm_client 支持工具调用
            # ReasoningLangChainAdapter 默认不支持工具调用，但某些思考模型（如 DeepSeek R1）支持
            adapter_type = type(llm_client).__name__
            if adapter_type == "ReasoningLangChainAdapter":
                # 检查底层的思考模型是否支持工具调用
                supports_tools = False
                try:
                    # 尝试从适配器获取模型名称
                    model_name = getattr(
                        llm_client._reasoning_client, "model_name", None
                    )
                    if model_name:
                        from src.llm.reasoning_config import ReasoningConfig

                        supports_tools = ReasoningConfig.supports_tools(model_name)
                        logger.info(
                            f"[Agent 节点] 检查思考模型工具支持 | "
                            f"model={model_name} | supports_tools={supports_tools}"
                        )
                except Exception as e:
                    logger.warning(f"[Agent 节点] 检查工具支持失败: {e}")

                if not supports_tools:
                    logger.error(
                        f"[Agent 节点] 检测到不支持的适配器类型 | "
                        f"adapter_type={adapter_type} | "
                        f"工具列表={tool_names} | "
                        f"ReasoningLangChainAdapter 不支持工具调用！"
                    )
                    # 尝试恢复：从 agent_config 获取普通模型客户端
                    agent_config = state.get("agent_config")
                    if agent_config:
                        normal_model = agent_config.get_model_name(
                            enable_thinking=False
                        )
                        logger.warning(
                            f"[Agent 节点] 尝试恢复到普通模型 | model={normal_model}"
                        )
                        try:
                            from src.core.di import get_global_container
                            from src.llm.adapters import LLMClientAdapter

                            container = get_global_container()
                            llm_factory = container.get("llm_factory")
                            normal_client = llm_factory.get_client(normal_model)
                            llm_client = LLMClientAdapter(normal_client)
                            logger.info(
                                f"[Agent 节点] 成功切换到普通模型客户端 | "
                                f"adapter_type={type(llm_client).__name__}"
                            )
                        except Exception as e:
                            logger.error(f"[Agent 节点] 切换到普通模型失败 | error={e}")
                            # 继续使用原有的适配器（虽然会失败，但至少有错误日志）
                else:
                    logger.info(
                        f"[Agent 节点] 思考模型支持工具调用，继续使用 | "
                        f"adapter_type={adapter_type}"
                    )

            llm_with_tools = llm_client.bind_tools(tools)

            # 记录绑定后的状态
            if hasattr(llm_with_tools, "_bound_tools"):
                bound_tool_names = [
                    getattr(t, "name", str(t)) for t in llm_with_tools._bound_tools
                ]
                logger.info(
                    f"[Agent 节点] 工具绑定后 | "
                    f"绑定工具数={len(llm_with_tools._bound_tools)} | "
                    f"绑定工具列表={bound_tool_names}"
                )

                # 验证每个工具的 args_schema
                for idx, tool in enumerate(llm_with_tools._bound_tools):
                    tool_name = getattr(tool, "name", f"unknown_{idx}")
                    if hasattr(tool, "args_schema"):
                        args_schema = tool.args_schema
                        if args_schema:
                            if hasattr(args_schema, "schema"):
                                schema = args_schema.schema()
                                param_keys = list(schema.get("properties", {}).keys())
                                logger.debug(
                                    f"[Agent 节点] 工具 {idx}: {tool_name} | "
                                    f"args_schema类型={type(args_schema).__name__} | "
                                    f"parameters_keys={param_keys}"
                                )
                            else:
                                logger.debug(
                                    f"[Agent 节点] 工具 {idx}: {tool_name} | "
                                    f"args_schema类型={type(args_schema).__name__}"
                                )
                        else:
                            logger.warning(
                                f"[Agent 节点] 工具 {idx}: {tool_name} | args_schema 为 None"
                            )
                    else:
                        logger.warning(
                            f"[Agent 节点] 工具 {idx}: {tool_name} | 无 args_schema 属性"
                        )
            else:
                logger.warning(
                    f"[Agent 节点] 工具绑定后 | "
                    f"未找到 _bound_tools 属性 | "
                    f"adapter_type={type(llm_with_tools).__name__}"
                )

            # 将思考模式参数作为 kwargs 传递，而不是 config
            # 因为 LangChain 的 config 是 RunnableConfig，不是模型参数
            logger.debug(
                f"[Agent 节点] 调用 LLM | "
                f"messages_count={len(messages)} | "
                f"config={config}"
            )

            # 使用信号量控制并发
            async with semaphore:
                logger.debug("[Agent 节点] 获取信号量成功 | 当前并发受限")
                response = await llm_with_tools.ainvoke(messages, **config)

            # 关键修复：如果启用思考模式且有思考回调，手动提取并发送思考内容
            # 因为 ainvoke 返回完整消息，思考内容在 additional_kwargs 中
            if enable_thinking and thinking_callback:
                if hasattr(response, "additional_kwargs"):
                    reasoning_content = response.additional_kwargs.get(
                        "reasoning_content"
                    )
                    if reasoning_content:
                        logger.info(
                            f"[Agent 节点] 从响应中提取思考内容 | len={len(reasoning_content)}"
                        )
                        try:
                            # 发送思考内容到前端
                            await thinking_callback(reasoning_content)
                        except Exception as e:
                            logger.warning(f"[Agent 节点] 思考回调失败: {e}")

            # 记录响应状态
            logger.info(
                f"[Agent 节点] LLM 响应 | "
                f"has_tool_calls={hasattr(response, 'tool_calls') and bool(response.tool_calls)} | "
                f"response_type={type(response).__name__}"
            )
        else:
            # 非流式调用
            # 获取 LLM 并发控制信号量（根据提供商）
            provider = getattr(llm_client, "_provider", "default")
            if hasattr(llm_client, "provider"):
                provider = llm_client.provider
            semaphore = ConcurrencyManager().get_llm_semaphore(provider)

            # 使用信号量控制并发
            async with semaphore:
                logger.debug("[Agent 节点] 获取信号量成功 | 当前并发受限")
                response = await llm_client.ainvoke(messages, **config)

        # 保持使用 LangChain 的消息类型（AIMessage），确保与 OpenAI 客户端兼容
        # 不要转换为内部 Message 类型，否则会导致 LLM 客户端不支持

        # 调试：检查 response 的内容
        response_content = getattr(response, "content", "")
        logger.info(
            f"[Agent.call_model] LLM 响应内容 | "
            f"content_preview={response_content[:200] if response_content else 'None'}... | "
            f"content_length={len(response_content) if response_content else 0}"
        )

        # 将响应消息添加到分层上下文存储并创建执行记录
        try:
            # 关键修复：必须保存 tool_calls，否则 DeepSeek API 会报错
            # LangChain 消息对象的类型判断
            response_type = type(response).__name__
            if "HumanMessage" in response_type:
                role = "user"
            elif "AIMessage" in response_type:
                role = "assistant"
            elif "ToolMessage" in response_type:
                role = "tool"
            else:
                role = "assistant"

            response_dict = {
                "role": role,
                "content": getattr(response, "content", ""),
            }
            # 如果有 tool_calls，必须保存
            if hasattr(response, "tool_calls") and response.tool_calls:
                response_dict["tool_calls"] = response.tool_calls

            # 为 AI 消息创建执行记录（由 Agent 循环负责创建）
            # 注意：不再调用 layered_context_store.add_message()，因为
            # _create_ai_execution_record 已经保存到数据库，而 load_recent_messages
            # 会从数据库加载消息，避免重复
            if role == "assistant" and session_id:
                # 检查是否有外部传入的第二条AI消息ID（来自 stream_processor）
                second_ai_message_id = None
                if config and "configurable" in config:
                    agent_loop = config["configurable"].get("agent_loop")
                    if agent_loop and hasattr(agent_loop, "second_ai_message_id"):
                        second_ai_message_id = agent_loop.second_ai_message_id
                        # 使用后清除，避免重复使用
                        if second_ai_message_id:
                            agent_loop.second_ai_message_id = None
                            logger.info(
                                f"[Agent.call_model] 使用 stream_processor 生成的第二条AI消息ID | "
                                f"record_id={second_ai_message_id}"
                            )

                # 从 layered_context_store 获取 executor 信息
                executor_type = getattr(layered_context_store, 'executor_type', None)
                executor_id = getattr(layered_context_store, 'executor_id', None)
                executor_name = getattr(layered_context_store, 'executor_name', None)

                await _create_ai_execution_record(
                    session_id=session_id,
                    content=getattr(response, "content", ""),
                    tool_calls=getattr(response, "tool_calls", None),
                    thinking_content=None,  # TODO: 从 response 中提取思考内容
                    record_id=second_ai_message_id,  # 使用外部传入的ID（如果有）
                    executor_type=executor_type,
                    executor_id=executor_id,
                    executor_name=executor_name,
                )
        except Exception as e:
            logger.warning(
                f"[Agent.call_model] 处理响应消息失败: {e}"
            )

        # 构建结果 - 不再返回 "messages" 字段
        result: dict[str, Any] = {
            "iteration": iteration + 1,
            "pending_tool_calls": [],
        }

        # 检查是否有工具调用
        # 直接使用 response，因为 response_msg = response
        if hasattr(response, "tool_calls") and response.tool_calls:
            result["pending_tool_calls"] = []
            for i, tc in enumerate(response.tool_calls):
                # 处理不同类型的工具调用对象
                if isinstance(tc, dict):
                    tool_id = tc.get("id", f"call_{i}")
                    tool_name = tc.get("name", tc.get("function", {}).get("name", ""))
                    tool_args = tc.get("args", tc.get("function", {}).get("arguments", {}))
                else:
                    # 处理 ToolCall 对象或其他具有属性的对象
                    tool_id = getattr(tc, "id", getattr(tc, "tool_call_id", f"call_{i}"))
                    tool_name = getattr(tc, "name", getattr(tc, "function", {}).get("name", ""))
                    tool_args = getattr(tc, "args", getattr(tc, "arguments", {}))

                result["pending_tool_calls"].append({
                    "id": tool_id,
                    "name": tool_name,
                    "args": tool_args,
                })
            # 记录工具调用
            tool_call_names = []
            for tc in response.tool_calls:
                if isinstance(tc, dict):
                    tool_name = tc.get("name", tc.get("function", {}).get("name", ""))
                else:
                    tool_name = getattr(tc, "name", getattr(tc, "function", {}).get("name", ""))
                tool_call_names.append(tool_name)
            logger.info(f"[Agent 节点] LLM 请求工具调用 | tools={tool_call_names}")
            logger.debug(
                f"[Agent 节点] 工具调用详情 | calls={json.dumps(result['pending_tool_calls'], ensure_ascii=False)}"
            )
        else:
            # 无工具调用，设置最终输出
            # 检查是否是结构化输出（JSON 响应）
            structured_output = state.get("output_schema")
            if structured_output and response.content:
                # 结构化输出：解析 JSON
                try:
                    # 去除可能的 markdown 代码块标记
                    content_to_parse = response.content.strip()
                    if content_to_parse.startswith("```"):
                        # 移除 ```json 或 ``` 标记
                        lines = content_to_parse.split("\n")
                        if lines[0].startswith("```"):
                            # 移除第一行（```json 或 ```）
                            lines = lines[1:]
                        # 移除最后一行（```）
                        if lines and lines[-1].strip() == "```":
                            lines = lines[:-1]
                        content_to_parse = "\n".join(lines).strip()

                    # 尝试解析 JSON
                    parsed_content = None
                    parse_error = None

                    try:
                        parsed_content = json.loads(content_to_parse)
                    except json.JSONDecodeError as e:
                        parse_error = e
                        logger.warning(
                            f"[Agent 节点] JSON 解析失败，尝试修复转义问题 | error={e}"
                        )

                        # 尝试使用 json5（更宽松的 JSON 解析器）
                        try:
                            import json5

                            parsed_content = json5.loads(content_to_parse)
                            logger.info("[Agent 节点] 使用 json5 解析成功")
                        except ImportError:
                            # json5 不可用，尝试手动修复
                            logger.info(
                                "[Agent 节点] json5 不可用，尝试手动修复转义问题"
                            )

                            # 常见问题：LLM 在 JSON 字符串值中使用未转义的引号
                            # 尝试修复 JSON 字符串中的转义问题
                            # 这是一个启发式方法，可能不适用于所有情况

                            # 将 "content": "..." 中的单反斜杠替换为双反斜杠
                            # 这只针对字符串值，不影响 JSON 结构
                            import re

                            def fix_json_escapes(match):
                                """修复 JSON 字符串中的转义序列"""
                                quote = match.group(1)  # " 或 '
                                content = match.group(2)
                                # 在字符串内容中，将单反斜杠替换为双反斜杠
                                # 但要小心不要破坏已经正确的转义序列
                                # 简单方法：将 \n 替换为 \\n，\t 替换为 \\t 等
                                fixed = content.replace("\\", "\\\\")
                                # 然后修复过度转义：\\\\n -> \n，\\\\t -> \t 等
                                fixed = fixed.replace("\\\\n", "\\n")
                                fixed = fixed.replace("\\\\t", "\t")
                                fixed = fixed.replace('\\"', '"')
                                fixed = fixed.replace("\\'", "'")
                                return f"{quote}{fixed}{quote}"

                            # 尝试修复字符串值中的转义
                            # 匹配 "key": "value" 或 'key': 'value' 模式
                            def fix_escape(m):
                                return (
                                    m.group(1)
                                    + m.group(2).replace("\\", "\\\\")
                                    + m.group(1)
                                )

                            fixed_content = re.sub(
                                r'(["\'])([^"\']*?)\1(?=\s*[，\}])',
                                fix_escape,
                                content_to_parse,
                            )

                            try:
                                parsed_content = json.loads(fixed_content)
                                logger.info("[Agent 节点] 手动修复转义后解析成功")
                            except json.JSONDecodeError:
                                # 如果还是失败，抛出原始错误
                                raise parse_error

                    if parsed_content is None:
                        raise parse_error
                    result["final_output"] = parsed_content
                    content_preview = (
                        str(parsed_content)[:200] + "..."
                        if len(str(parsed_content)) > 200
                        else str(parsed_content)
                    )
                    logger.info(
                        f"[Agent 节点] LLM 返回结构化输出 | "
                        f"parsed_type={type(parsed_content).__name__} | "
                        f"keys={list(parsed_content.keys()) if isinstance(parsed_content, dict) else 'N/A'}"
                    )
                    logger.debug(f"[Agent 节点] 响应内容 | content={content_preview}")
                except (json.JSONDecodeError, TypeError) as e:
                    # JSON 解析失败，回退到原始内容
                    logger.warning(
                        f"[Agent 节点] 结构化输出 JSON 解析失败 | "
                        f"error={e} | content前100={response.content[:100] if response.content else 'None'}"
                    )
                    token_counter = get_token_counter()
                    result["final_output"] = response.content
                    content_preview = (
                        response.content[:200] + "..."
                        if response.content
                        and token_counter.count_tokens(response.content) > 200
                        else response.content
                    )
                    logger.info(
                        f"[Agent 节点] LLM 返回文本响应 | content_tokens={token_counter.count_tokens(response.content) if response.content else 0}"
                    )
                    logger.debug(f"[Agent 节点] 响应内容 | content={content_preview}")
            else:
                # 普通文本响应
                token_counter = get_token_counter()
                result["final_output"] = response.content
                content_preview = (
                    response.content[:200] + "..."
                    if response.content
                    and token_counter.count_tokens(response.content) > 200
                    else response.content
                )
                logger.info(
                    f"[Agent 节点] LLM 返回文本响应 | content_tokens={token_counter.count_tokens(response.content) if response.content else 0}"
                )
                logger.debug(f"[Agent 节点] 响应内容 | content={content_preview}")

        return result

    except Exception as e:
        logger.exception(f"[Agent 节点] LLM 调用失败 | error={str(e)}")
        return {
            "error": f"LLM 调用失败: {str(e)}",
            "should_stop": True,
        }


async def _create_ai_execution_record(
    session_id: str,
    content: str,
    tool_calls: list | None = None,
    thinking_content: str | None = None,
    record_id: str | None = None,
    executor_type: str | None = None,
    executor_id: str | None = None,
    executor_name: str | None = None,
) -> str | None:
    """
    创建 AI 消息执行记录

    由 Agent 循环负责创建 AI 消息的执行记录，确保每条 AI 消息都有独立的记录。

    Args:
        session_id: 会话ID
        content: AI 消息内容
        tool_calls: 工具调用列表（可选）
        thinking_content: 思考内容（可选）
        record_id: 指定的记录ID（可选，用于第二条AI消息）
        executor_type: 执行者类型（可选，如 "agent"）
        executor_id: 执行者ID（可选，用于消息过滤）
        executor_name: 执行者名称（可选，如 "通用任务执行者"）

    Returns:
        记录ID 或 None
    """
    from src.db.connection import get_session_context
    from src.db.repositories.execution_record_repo import ExecutionRecordRepository
    from src.utils.id_encoder import parse_nested_id
    from src.utils.message_id_helper import get_sequence_from_id

    try:
        async with get_session_context() as db:
            repo = ExecutionRecordRepository(db)

            if record_id is None:
                record_id = await repo.save_execution_record(
                    session_id=session_id,
                    message_data={},
                    auto_commit=False,
                )

            sequence = get_sequence_from_id(record_id) or 1

            try:
                parsed = parse_nested_id(record_id)
                depth = parsed.get("depth", 0)
            except Exception:
                depth = 0

            if not content and not tool_calls:
                logger.warning(
                    f"[_create_ai_execution_record] AI 消息数据异常 | "
                    f"session_id={session_id} | record_id={record_id} | "
                    f"content 为空且无 tool_calls，可能是 LLM 响应异常或流式传输中断"
                )

            segments = []
            if tool_calls:
                if content:
                    segments.append({"type": "text", "content": content})

                for i, tool_call in enumerate(tool_calls):
                    if isinstance(tool_call, dict):
                        tool_call_id = tool_call.get("id", tool_call.get("call_id", f"call_{i}"))
                        tool_name = tool_call.get("name", tool_call.get("function", {}).get("name", ""))
                    else:
                        tool_call_id = getattr(tool_call, "id", getattr(tool_call, "call_id", getattr(tool_call, "tool_call_id", f"call_{i}")))
                        tool_name = getattr(tool_call, "name", getattr(tool_call, "function", {}).get("name", ""))

                    segments.append({
                        "type": "tool_call",
                        "tool_call_id": tool_call_id,
                        "tool_name": tool_name,
                    })

                if not content:
                    segments.append({"type": "text", "content": ""})
            else:
                segments.append({"type": "text", "content": content})

            message_data = {
                "type": "ai",
                "record_type": "ai_response",
                "content": content,
                "status": "completed",
                "order": {
                    "sequence": sequence,
                    "depth": depth,
                },
                "version": {
                    "is_current": True,
                },
                "segments": segments,
                "executor": {
                    "type": executor_type or "agent",
                    "id": executor_id or "",
                    "name": executor_name or "",
                },
            }

            if tool_calls:
                message_data["tool_calls"] = tool_calls

            if thinking_content:
                message_data["thinking"] = thinking_content

            final_record_id = await repo.save_execution_record(
                session_id=session_id,
                message_data=message_data,
                record_id=record_id,
            )

            logger.info(
                f"[_create_ai_execution_record] 创建 AI 执行记录 | "
                f"record_id={final_record_id} | session_id={session_id} | "
                f"content_length={len(content) if content else 0} | "
                f"segments_count={len(segments)}"
            )

            return final_record_id

    except Exception as e:
        logger.error(
            f"[_create_ai_execution_record] 创建 AI 执行记录失败 | "
            f"session_id={session_id} | error={e}"
        )
        return None
