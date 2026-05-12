"""
上下文构建器模块

负责按 layer_order 顺序拼接各层内容，构建完整的 LLM 上下文

四层结构：
- 第1层（系统静态层）：system_prompt + tools_description + static_vars
- 第2层（压缩层）：L3关键词 + L2三元组 + L1八段压缩
- 第3层（消息层）：recent_messages（按executor_id隔离）
- 第4层（尾部动态层）：dynamic_vars（实时生成）

简化设计：
- 3种注入方式：full(全量)、summary(摘要)、retrieval(检索)
- 第1层 static_vars：长期保存的静态内容
- 第4层 dynamic_vars：每轮实时生成的动态内容（普通变量 + 记忆注入）
"""

import json
import logging
import os
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from src.memory.loaders import LOADER_MAP, load_layer

logger = logging.getLogger(__name__)

# 调试日志文件路径
_DEBUG_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))), "logs")
_DEBUG_LOG_FILE = os.path.join(_DEBUG_LOG_DIR, "context_builder_debug.log")


class ContextBuilder:
    """
    上下文构建器

    负责按照配置的 layer_order 顺序，依次构建各层内容，
    最终生成完整的 LangChain 消息列表供 LLM 使用。
    """

    def __init__(self):
        """初始化上下文构建器"""
        self._agent_config = None

    async def build(
        self,
        store: Any,
        user_message: str | None = None,
        agent_config: Any | None = None
    ) -> tuple[list[Any], list[dict]]:
        """
        构建发送给 LLM 的完整上下文

        Args:
            store: LayeredContextStore 实例
            user_message: 用户新消息（用于日志记录）
            agent_config: Agent 配置，用于决定是否添加动态变量等

        Returns:
            元组 (messages, context_parts)
        """
        messages: list[Any] = []
        context_parts: list[dict] = []

        # 保存 agent_config 供后续使用
        self._agent_config = agent_config

        # 读取配置的 layer_order
        layer_order = self._get_layer_order(store)

        # 按照 layer_order 顺序构建各层
        for layer_name in layer_order:
            await self._build_layer(
                store=store,
                layer_name=layer_name,
                messages=messages,
                context_parts=context_parts,
            )

        # 输出上下文构建摘要
        self._log_build_summary(messages, context_parts)

        # 输出内存状态到日志文件（用于调试）
        self._log_memory_state(store, messages)

        return messages, context_parts

    def _get_layer_order(self, store: Any) -> list[str]:
        """获取配置的 layer_order"""
        from src.config.system_config import get_system_config_manager

        manager = get_system_config_manager()

        try:
            config = manager.load_context_window_config()
        except FileNotFoundError as e:
            raise KeyError(
                f"[ContextBuilder] 上下文窗口配置文件未找到: {e}\n"
                f"请检查配置文件路径是否正确，或在 config/ 目录下创建对应的配置文件。"
            ) from e

        layer_order = config.get("layer_order", [])

        if not layer_order:
            raise KeyError(
                f"[ContextBuilder] 配置文件中缺少 'layer_order' 配置项\n"
                f"配置文件路径: {getattr(manager, 'config_path', 'unknown')}\n"
                f"请在配置文件中添加 layer_order 配置，例如:\n"
                f"  layer_order:\n"
                f"    - system_prompt\n"
                f"    - tools_description\n"
                f"    - static_vars\n"
                f"    - l3_memory\n"
                f"    - l2_memory\n"
                f"    - l1_memory\n"
                f"    - recent_messages\n"
                f"    - dynamic_variables"
            )

        return layer_order

    async def _build_layer(
        self,
        store: Any,
        layer_name: str,
        messages: list[Any],
        context_parts: list[dict],
    ) -> None:
        """构建单层内容"""
        logger.info(f"[ContextBuilder] 开始构建层: {layer_name}")

        # 检查工具描述开关
        if layer_name == "tools_description":
            from src.config.system_config import get_system_config_manager
            manager = get_system_config_manager()
            config = manager.load_context_window_config()
            include_in_prompt = config.get("include_tools_description_in_prompt", True)
            if not include_in_prompt:
                logger.info("[ContextBuilder] 工具描述开关为 false，跳过构建工具描述层")
                return

        # 第1-3层：使用加载器从 store 读取
        if layer_name in LOADER_MAP:
            # 特殊处理 static_vars，需要传递 agent_config
            if layer_name == "static_vars":
                from src.memory.loaders import load_static_vars
                content = await load_static_vars(store, self._agent_config)
            else:
                content = await load_layer(store, layer_name)
        # 第4层：实时生成动态上下文（不保存在 store 中）
        elif layer_name == "dynamic_vars":
            content = await self._generate_dynamic_layer(store)
        else:
            logger.warning(f"[ContextBuilder] 未知的层名称: {layer_name}")
            return

        # 记录层内容信息
        if content:
            if isinstance(content, list):
                logger.info(f"[ContextBuilder] 层 {layer_name} | 内容类型=list | 长度={len(content)}")
                if content and len(content) > 0:
                    logger.info(f"[ContextBuilder] 层 {layer_name} | 第一项类型={type(content[0]).__name__}")
                    if isinstance(content[0], dict) and 'role' in content[0]:
                        logger.info(f"[ContextBuilder] 层 {layer_name} | 消息角色={[m.get('role') for m in content[:3]]}")
            elif isinstance(content, str):
                logger.info(f"[ContextBuilder] 层 {layer_name} | 内容类型=str | 长度={len(content)}")
            else:
                logger.info(f"[ContextBuilder] 层 {layer_name} | 内容类型={type(content).__name__}")
        else:
            logger.info(f"[ContextBuilder] 层 {layer_name} | 内容为空")

        # 根据层类型构建消息
        # 特殊处理 dynamic_vars -> _build_dynamic_variables
        if layer_name == "dynamic_vars":
            builder_method = self._build_dynamic_variables
        else:
            builder_method = getattr(self, f"_build_{layer_name}", None)

        if builder_method:
            before_count = len(messages)
            builder_method(content, messages, context_parts)
            after_count = len(messages)
            logger.info(f"[ContextBuilder] 层 {layer_name} | 构建完成 | 新增消息数={after_count - before_count}")
        else:
            logger.warning(f"[ContextBuilder] 层 {layer_name} | 未找到构建方法")

    def _build_system_prompt(
        self, content: Any, messages: list[Any], context_parts: list[dict]
    ) -> None:
        """构建系统提示层"""
        if content:
            messages.append(SystemMessage(content=content))
            context_parts.append({
                "section": "固定 Prompt", "type": "SystemMessage",
                "length": len(content), "preview": content[:100],
            })
            logger.info(f"[ContextBuilder] system_prompt | 长度={len(content)}")

    def _build_tools_description(
        self, content: Any, messages: list[Any], context_parts: list[dict]
    ) -> None:
        """构建工具描述层"""
        if content:
            msg = f"## 可用工具\n\n{content}"
            messages.append(SystemMessage(content=msg))
            context_parts.append({
                "section": "工具描述", "type": "SystemMessage",
                "length": len(msg), "preview": content[:100],
            })
            logger.info(f"[ContextBuilder] tools_description | 长度={len(msg)}")

    def _build_static_vars(
        self, content: Any, messages: list[Any], context_parts: list[dict]
    ) -> None:
        """构建静态变量层（第1层）"""
        if content:
            msg = f"## 静态资源\n\n{content}"
            messages.append(SystemMessage(content=msg))
            context_parts.append({
                "section": "静态资源", "type": "SystemMessage",
                "length": len(msg), "preview": content[:100],
            })
            logger.info(f"[ContextBuilder] static_vars | 长度={len(msg)}")

    def _build_l3_memory(
        self, content: Any, messages: list[Any], context_parts: list[dict]
    ) -> None:
        """构建 L3 关键词层"""
        if content:
            msg = f"## 历史关键词\n\n{content}"
            messages.append(SystemMessage(content=msg))
            context_parts.append({
                "section": "L3 关键词", "type": "SystemMessage",
                "length": len(msg), "preview": content[:100],
            })
            logger.info(f"[ContextBuilder] l3_memory | 长度={len(msg)}")

    def _build_l2_memory(
        self, content: Any, messages: list[Any], context_parts: list[dict]
    ) -> None:
        """构建 L2 摘要层"""
        if content:
            msg = f"## 历史摘要\n\n{content}"
            messages.append(SystemMessage(content=msg))
            context_parts.append({
                "section": "L2 摘要", "type": "SystemMessage",
                "length": len(msg), "preview": content[:100],
            })
            logger.info(f"[ContextBuilder] l2_memory | 长度={len(msg)}")

    def _build_l1_memory(
        self, content: Any, messages: list[Any], context_parts: list[dict]
    ) -> None:
        """构建 L1 详细历史层"""
        if content:
            msg = f"## 详细历史\n\n{content}"
            messages.append(SystemMessage(content=msg))
            context_parts.append({
                "section": "L1 摘要", "type": "SystemMessage",
                "length": len(msg), "preview": content[:100],
            })
            logger.info(f"[ContextBuilder] l1_memory | 长度={len(msg)}")

    def _build_recent_messages(
        self, content: Any, messages: list[Any], context_parts: list[dict]
    ) -> None:
        """构建最近消息层"""
        logger.info(f"[_build_recent_messages] 开始构建 | content类型={type(content).__name__} | content长度={len(content) if content else 0}")

        if not content:
            logger.warning("[_build_recent_messages] content为空，直接返回")
            return

        history_messages = []
        for idx, msg in enumerate(content):
            role = msg.get("role")
            msg_content = msg.get("content", "")
            tool_calls = msg.get("tool_calls")

            logger.info(f"[_build_recent_messages] 处理消息 #{idx} | role={role} | content长度={len(msg_content) if msg_content else 0} | content预览={msg_content[:50] if msg_content else 'None'}")

            # 跳过空内容的消息（除了 tool 消息和带 tool_calls 的 assistant 消息）
            # assistant 消息可能内容为空但包含 tool_calls
            is_empty_content = not msg_content
            has_tool_calls = tool_calls and len(tool_calls) > 0
            should_skip = is_empty_content and role != "tool" and not has_tool_calls

            if should_skip:
                logger.info(f"[_build_recent_messages] 跳过空内容的 {role} 消息")
                continue

            if role == "user":
                history_messages.append(HumanMessage(content=msg_content))
                logger.info(f"[_build_recent_messages] 添加 HumanMessage | content={msg_content[:50]}...")
            elif role == "assistant":
                if has_tool_calls:
                    normalized = self._normalize_tool_calls(tool_calls)
                    history_messages.append(AIMessage(content=msg_content, tool_calls=normalized))
                    logger.info(f"[_build_recent_messages] 添加 AIMessage(带tool_calls) | tool_calls数量={len(tool_calls)}")
                else:
                    history_messages.append(AIMessage(content=msg_content))
                    logger.info(f"[_build_recent_messages] 添加 AIMessage | content={msg_content[:50] if msg_content else 'None'}...")
            elif role == "tool":
                history_messages.append(ToolMessage(
                    content=msg_content,
                    tool_call_id=msg.get("tool_call_id", ""),
                    name=msg.get("name", "unknown")
                ))
                logger.info(f"[_build_recent_messages] 添加 ToolMessage | name={msg.get('name', 'unknown')}")
            else:
                logger.warning(f"[_build_recent_messages] 未知角色类型: {role}")

        if history_messages:
            messages.extend(history_messages)
            context_parts.append({
                "section": "消息历史",
                "type": f"{len(history_messages)} 条消息",
                "length": sum(len(getattr(m, "content", "")) for m in history_messages),
                "preview": "从内存加载",
            })
            logger.info(f"[_build_recent_messages] 构建完成 | 总消息数={len(history_messages)}")
        else:
            logger.warning("[_build_recent_messages] 没有构建任何消息")

    async def _generate_dynamic_layer(self, store: Any) -> dict[str, Any]:
        """
        生成第四层：尾部动态层

        第4层包含：
        1. 普通动态变量：时间、Session、Agent、Model
        2. 记忆注入：通过 Agent 配置指定名称和注入方式

        简化设计：3种注入方式（full/summary/retrieval）
        """
        layer_data = {}

        try:
            from datetime import datetime

            # 1. 普通动态变量
            layer_data["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            session_vars = {}
            if getattr(store, 'session_id', None):
                session_vars["session_id"] = store.session_id
            if getattr(store, 'user_id', None):
                session_vars["user_id"] = store.user_id
            if session_vars:
                layer_data["session"] = session_vars

            agent_vars = {}
            if getattr(store, 'executor_name', None):
                agent_vars["name"] = store.executor_name
            if getattr(store, 'executor_type', None):
                agent_vars["type"] = store.executor_type
            if agent_vars:
                layer_data["agent"] = agent_vars

            model_vars = {}
            if getattr(store, 'model_alias', None):
                model_vars["name"] = store.model_alias
            if model_vars:
                layer_data["model"] = model_vars

            # 2. 记忆注入（从 Agent 配置读取）
            if self._agent_config:
                # 获取查询文本（用于 retrieval）
                # 从 context_repository 获取最近的用户消息
                query = ""
                try:
                    if hasattr(store, 'context_repository') and store.context_repository:
                        recent_messages = await store.get_recent_messages(limit=10)
                        for msg in recent_messages:
                            if msg.get("role") == "user":
                                query = msg.get("content", "")
                                break
                except Exception as e:
                    logger.debug(f"[ContextBuilder] 获取查询文本失败: {e}")

                # 处理动态变量中的记忆注入
                memory_injections = []

                # 从 dynamic_vars.items 获取简化格式的配置
                dynamic_vars_config = getattr(self._agent_config, 'dynamic_vars', None)
                if dynamic_vars_config and hasattr(dynamic_vars_config, 'items'):
                    for item in dynamic_vars_config.items:
                        if item.type == 'memory':
                            # 记忆注入
                            name = item.name
                            inject_type = item.inject_type
                            top_k = item.top_k

                            if name and hasattr(store, 'inject_memory'):
                                try:
                                    content = await store.inject_memory(
                                        name=name,
                                        inject_type=inject_type,
                                        query=query if inject_type == 'retrieval' else '',
                                        top_k=top_k
                                    )
                                    if content:
                                        memory_injections.append({
                                            'name': name,
                                            'type': inject_type,
                                            'content': content
                                        })
                                except Exception as e:
                                    logger.debug(f"[ContextBuilder] 记忆注入失败 {name}: {e}")

                if memory_injections:
                    layer_data['memory_injections'] = memory_injections

        except Exception as e:
            logger.warning(f"[ContextBuilder] 生成第四层失败: {e}")

        return layer_data

    def _build_dynamic_variables(
        self, content: Any, messages: list[Any], context_parts: list[dict]
    ) -> None:
        """
        构建第四层：尾部动态层

        包含：
        1. 普通动态变量（时间、Session、Agent、Model）
        2. 记忆注入（通过 inject_memory 获取）
        """
        if not content or not isinstance(content, dict):
            logger.debug("[ContextBuilder] 动态层内容为空，跳过")
            return

        layer_parts = []

        # 1. 普通动态变量（简化格式，避免嵌套标题）
        var_lines = []
        if 'timestamp' in content:
            var_lines.append(f"- 时间: {content['timestamp']}")
        if 'session' in content:
            session = content['session']
            if session.get('session_id'):
                var_lines.append(f"- 会话: {session['session_id']}")
        if 'agent' in content:
            agent = content['agent']
            if agent.get('name'):
                var_lines.append(f"- Agent: {agent['name']}")
        if 'model' in content:
            model = content['model']
            if model.get('name'):
                var_lines.append(f"- 模型: {model['name']}")

        if var_lines:
            layer_parts.append("\n".join(var_lines))

        # 2. 记忆注入
        memory_injections = content.get('memory_injections', [])
        if memory_injections:
            injection_lines = []
            for injection in memory_injections:
                name = injection.get('name', 'unknown')
                inj_type = injection.get('type', 'full')
                inj_content = injection.get('content', '')
                injection_lines.append(f"\n[{name}] ({inj_type}):\n{inj_content}")
            if injection_lines:
                layer_parts.append("## 相关记忆" + "".join(injection_lines))

        # 构建完整第四层（简化标题结构）
        if layer_parts:
            full_content = "\n\n".join(layer_parts)
            # 使用单一标题，避免嵌套
            msg = f"## 动态上下文\n\n{full_content}"
            messages.append(SystemMessage(content=msg))
            context_parts.append({
                "section": "动态上下文", "type": "SystemMessage",
                "length": len(msg), "preview": full_content[:100],
            })
            logger.info(f"[ContextBuilder] 第四层动态上下文 | 长度={len(msg)}")
        else:
            logger.debug("[ContextBuilder] 第四层内容为空，跳过")

    def _normalize_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """标准化工具调用格式"""
        normalized = []
        for tc in tool_calls:
            if "function" in tc:
                # OpenAI 格式
                tool_args = {}
                arguments = tc.get("function", {}).get("arguments", "")
                if isinstance(arguments, str):
                    try:
                        tool_args = json.loads(arguments)
                    except json.JSONDecodeError:
                        tool_args = {}
                elif isinstance(arguments, dict):
                    tool_args = arguments
                normalized_tc = {
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "args": tool_args,
                }
            else:
                normalized_tc = {
                    "id": tc.get("id", tc.get("call_id", "")),
                    "name": tc.get("name", tc.get("tool_name", "")),
                    "args": tc.get("args", tc.get("tool_args", {})),
                }
            normalized.append(normalized_tc)
        return normalized

    def _log_build_summary(self, messages: list[Any], context_parts: list[dict]) -> None:
        """输出上下文构建摘要日志"""
        total_chars = sum(len(getattr(msg, "content", "")) for msg in messages)
        logger.info(
            f"[ContextBuilder] 上下文构建完成 | "
            f"总消息数={len(messages)} | "
            f"总字符数={total_chars} | "
            f"分段数={len(context_parts)}"
        )
        for i, part in enumerate(context_parts, 1):
            logger.info(
                f"[ContextBuilder]   {i}. {part['section']} | "
                f"类型={part['type']} | "
                f"长度={part['length']}"
            )

    def _log_memory_state(self, store: Any, messages: list[Any]) -> None:
        """
        将内存变量状态输出到日志文件

        用于调试四层上下文构建过程，输出完整的内存状态到独立日志文件。
        """
        try:
            import datetime

            # 准备内存状态数据
            memory_state = {
                "timestamp": datetime.datetime.now().isoformat(),
                "session_id": getattr(store, 'session_id', 'N/A'),
                "executor_id": getattr(store, 'executor_id', 'N/A'),
                "executor_type": getattr(store, 'executor_type', 'N/A'),
                "layers": {
                    "L1": {
                        "exists": bool(getattr(store, '_layers', {}).get('L1')),
                        "length": len(getattr(store, '_layers', {}).get('L1', '')),
                        "preview": getattr(store, '_layers', {}).get('L1', '')[:200] + "..." if len(getattr(store, '_layers', {}).get('L1', '')) > 200 else getattr(store, '_layers', {}).get('L1', '')
                    },
                    "L2": {
                        "exists": bool(getattr(store, '_layers', {}).get('L2')),
                        "length": len(getattr(store, '_layers', {}).get('L2', '')),
                        "preview": getattr(store, '_layers', {}).get('L2', '')[:200] + "..." if len(getattr(store, '_layers', {}).get('L2', '')) > 200 else getattr(store, '_layers', {}).get('L2', '')
                    },
                    "L3": {
                        "exists": bool(getattr(store, '_layers', {}).get('L3')),
                        "length": len(getattr(store, '_layers', {}).get('L3', '')),
                        "preview": getattr(store, '_layers', {}).get('L3', '')[:200] + "..." if len(getattr(store, '_layers', {}).get('L3', '')) > 200 else getattr(store, '_layers', {}).get('L3', '')
                    }
                },
                "messages": {
                    "total_count": len(getattr(store, '_messages', [])),
                    "executor_filtered_count": len([m for m in getattr(store, '_messages', []) if m.get('executor_id') == getattr(store, 'executor_id', None)]),
                    "recent_preview": [
                        {"role": m.get("role"), "content": m.get("content", "")[:100] + "..." if len(m.get("content", "")) > 100 else m.get("content", "")}
                        for m in list(getattr(store, '_messages', []))[-3:]
                    ]
                },
                "fixed_prompt": {
                    "exists": bool(getattr(store, '_system_prompt', '')),
                    "length": len(getattr(store, '_system_prompt', '')),
                    "preview": getattr(store, '_system_prompt', '')[:200] + "..." if len(getattr(store, '_system_prompt', '')) > 200 else getattr(store, '_system_prompt', '')
                },
                "built_messages": [
                    {"type": type(m).__name__, "content": getattr(m, "content", "")[:200] + "..." if len(getattr(m, "content", "")) > 200 else getattr(m, "content", "")}
                    for m in messages
                ]
            }

            # 写入日志文件
            os.makedirs(_DEBUG_LOG_DIR, exist_ok=True)
            with open(_DEBUG_LOG_FILE, 'a', encoding='utf-8') as f:
                f.write("=" * 80 + "\n")
                f.write(f"ContextBuilder Memory State - {memory_state['timestamp']}\n")
                f.write("=" * 80 + "\n")
                f.write(json.dumps(memory_state, ensure_ascii=False, indent=2))
                f.write("\n\n")

            logger.debug(f"[ContextBuilder] 内存状态已输出到: {_DEBUG_LOG_FILE}")

        except Exception as e:
            logger.warning(f"[ContextBuilder] 输出内存状态失败: {e}")
