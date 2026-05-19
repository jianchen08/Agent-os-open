"""
加载器模块

提供简化的层加载器函数，用于从 LayeredContextStore 加载上下文层内容

四层结构：
- 第1层（系统静态层）：system_prompt + tools_description + static_vars
- 第2层（压缩层）：L3关键词 + L2三元组 + L1八段压缩
- 第3层（消息层）：recent_messages（按executor_id隔离，按需从数据库加载）
- 第4层（尾部动态层）：dynamic_vars（实时生成，不保存）

加载器只读取，不保存中间数据。构建完成后，临时变量自动销毁。

分层读取策略（块级读取，不可分割）：
- L0（原文）：读取所有未压缩的原文消息
- L1：读取所有L1压缩块（每个块对应多个执行记录，不可分割）
- L2：读取所有L2压缩块（每个块对应多个L1块，不可分割）
- L3：读取所有L3压缩块（最顶层）

压缩策略（块级压缩）：
- 压缩时根据预算决定压缩哪些块
- 如果n个块比预算大，n-1个比预算小，则压缩第n个块
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ==================== 第1层：系统静态层 ====================

async def load_system_prompt(store: Any) -> str:
    """
    加载系统提示（第1层）

    从 store._system_prompt 读取，长期保存在内存中
    """
    return getattr(store, '_system_prompt', "")


async def load_tools_description(store: Any) -> str:
    """
    加载工具描述（第1层）

    从 store._tools_description 读取，长期保存在内存中
    """
    return getattr(store, '_tools_description', "")


async def load_static_vars(store: Any, agent_config: Any = None) -> str:
    """
    加载静态变量（第1层）

    支持两种配置格式：
    1. 传统格式：从 store._static_knowledge 读取
    2. 简化格式：从 agent_config.static_vars.items 读取，支持注入方式

    Args:
        store: LayeredContextStore 实例
        agent_config: Agent 配置，用于获取静态变量配置

    Returns:
        静态变量内容字符串
    """
    parts = []

    # 1. 处理简化格式：从 agent_config.static_vars.items 读取
    if agent_config:
        static_vars_config = getattr(agent_config, 'static_vars', None)
        if static_vars_config and hasattr(static_vars_config, 'items'):
            for item in static_vars_config.items:
                # 检查 enabled 属性，如果存在且为 False 则跳过
                if hasattr(item, 'enabled') and not item.enabled:
                    continue

                item_type = getattr(item, 'type', '')
                inject_type = getattr(item, 'inject_type', 'full')
                name = getattr(item, 'name', '')
                top_k = getattr(item, 'top_k', 3)

                # 获取查询文本（用于 retrieval）
                query = ""
                if hasattr(store, '_messages') and store._messages:
                    for msg in reversed(store._messages):
                        if msg.get("role") == "user":
                            query = msg.get("content", "")
                            break

                content = ""
                try:
                    if item_type == 'memory':
                        # 记忆注入
                        if name and hasattr(store, 'inject_static_var'):
                            content = await store.inject_static_var(
                                name=name,
                                inject_type=inject_type,
                                query=query if inject_type == 'retrieval' else '',
                                top_k=top_k
                            )
                        elif name and hasattr(store, 'inject_memory'):
                            # 回退到 inject_memory
                            content = await store.inject_memory(
                                name=name,
                                inject_type=inject_type,
                                query=query if inject_type == 'retrieval' else '',
                                top_k=top_k
                            )
                    elif item_type == 'file':
                        # 文件读取
                        path = getattr(item, 'path', '')
                        if path:
                            content = await _load_file_content(path)
                    elif item_type == 'text':
                        # 直接文本内容
                        content = getattr(item, 'content', '')

                    if content:
                        parts.append(f"### {name or item_type}\n{content}")

                except Exception as e:
                    logger.debug(f"[load_static_vars] 加载静态变量项失败 {name}: {e}")

    # 2. 处理传统格式：从 store._static_knowledge 读取（向后兼容）
    static_knowledge = getattr(store, '_static_knowledge', {})
    if static_knowledge:
        for name, content in static_knowledge.items():
            parts.append(f"### {name}\n{content}")

    return "\n\n".join(parts)


async def _load_file_content(path: str) -> str:
    """
    异步加载文件内容

    Args:
        path: 文件路径

    Returns:
        文件内容字符串
    """
    try:
        import aiofiles
        async with aiofiles.open(path, encoding='utf-8') as f:
            return await f.read()
    except Exception as e:
        logger.warning(f"[_load_file_content] 读取文件失败 {path}: {e}")
        return f"[文件读取失败: {path}]"


# ==================== 第2层：压缩层（块级读取） ====================

async def load_l1_memory(store: Any) -> str:
    """
    加载 L1 层内容（第2层 - 八段摘要）

    读取策略：
    - 读取所有 L1 压缩块（不可分割）
    - 每个块对应多个原文执行记录

    优先使用 store._reader 读取（有 ChunkMetadataStore 元数据索引），
    如果不可用则回退到 context_repository
    """
    session_id = getattr(store, 'session_id', None)
    executor_id = getattr(store, 'executor_id', None)
    executor_type = getattr(store, 'executor_type', None)

    if not session_id:
        return ""

    try:
        # 优先使用 _reader 读取（有元数据索引）
        reader = getattr(store, '_reader', None)
        if reader is not None and hasattr(reader, 'read_compressed_layer'):
            logger.debug(f"[load_l1_memory] 使用 _reader 读取 L1 层 | executor_id={executor_id}")
            contents = await reader.read_compressed_layer("L1")
            if contents:
                return "\n\n---\n\n".join(contents)
            return ""

        # 回退到 context_repository
        if not hasattr(store, 'context_repository') or not store.context_repository:
            return ""

        logger.debug(f"[load_l1_memory] 使用 context_repository 读取 L1 层 | executor_id={executor_id}")
        chunks = await store.context_repository.get_layer_chunks(
            session_id=session_id,
            layer="L1",
            executor_type=executor_type,
            executor_id=executor_id
        )

        if not chunks:
            return ""

        # 合并所有块内容（块不可分割，全部读取）
        contents = [chunk["content"] for chunk in chunks if chunk.get("content")]
        return "\n\n---\n\n".join(contents) if contents else ""

    except Exception as e:
        logger.warning(f"[load_l1_memory] 加载失败: {e}")
        return ""


async def load_l2_memory(store: Any) -> str:
    """
    加载 L2 层内容（第2层 - 三元组摘要）

    读取策略：
    - 读取所有 L2 压缩块（不可分割）
    - 每个块对应多个 L1 块

    优先使用 store._reader 读取（有 ChunkMetadataStore 元数据索引），
    如果不可用则回退到 context_repository
    """
    session_id = getattr(store, 'session_id', None)
    executor_id = getattr(store, 'executor_id', None)
    executor_type = getattr(store, 'executor_type', None)

    if not session_id:
        return ""

    try:
        # 优先使用 _reader 读取（有元数据索引）
        reader = getattr(store, '_reader', None)
        if reader is not None and hasattr(reader, 'read_compressed_layer'):
            logger.debug(f"[load_l2_memory] 使用 _reader 读取 L2 层 | executor_id={executor_id}")
            contents = await reader.read_compressed_layer("L2")
            if contents:
                return "\n\n---\n\n".join(contents)
            return ""

        # 回退到 context_repository
        if not hasattr(store, 'context_repository') or not store.context_repository:
            return ""

        logger.debug(f"[load_l2_memory] 使用 context_repository 读取 L2 层 | executor_id={executor_id}")
        chunks = await store.context_repository.get_layer_chunks(
            session_id=session_id,
            layer="L2",
            executor_type=executor_type,
            executor_id=executor_id
        )

        if not chunks:
            return ""

        # 合并所有块内容（块不可分割，全部读取）
        contents = [chunk["content"] for chunk in chunks if chunk.get("content")]
        return "\n\n---\n\n".join(contents) if contents else ""

    except Exception as e:
        logger.warning(f"[load_l2_memory] 加载失败: {e}")
        return ""


async def load_l3_memory(store: Any) -> str:
    """
    加载 L3 层内容（第2层 - 关键词索引）

    读取策略：
    - 读取所有 L3 压缩块（不可分割）
    - L3 是最顶层，不会被进一步压缩

    优先使用 store._reader 读取（有 ChunkMetadataStore 元数据索引），
    如果不可用则回退到 context_repository
    """
    session_id = getattr(store, 'session_id', None)
    executor_id = getattr(store, 'executor_id', None)
    executor_type = getattr(store, 'executor_type', None)

    if not session_id:
        return ""

    try:
        # 优先使用 _reader 读取（有元数据索引）
        reader = getattr(store, '_reader', None)
        if reader is not None and hasattr(reader, 'read_compressed_layer'):
            logger.debug(f"[load_l3_memory] 使用 _reader 读取 L3 层 | executor_id={executor_id}")
            contents = await reader.read_compressed_layer("L3")
            if contents:
                return "\n\n---\n\n".join(contents)
            return ""

        # 回退到 context_repository
        if not hasattr(store, 'context_repository') or not store.context_repository:
            return ""

        logger.debug(f"[load_l3_memory] 使用 context_repository 读取 L3 层 | executor_id={executor_id}")
        chunks = await store.context_repository.get_layer_chunks(
            session_id=session_id,
            layer="L3",
            executor_type=executor_type,
            executor_id=executor_id
        )

        if not chunks:
            return ""

        # 合并所有块内容
        contents = [chunk["content"] for chunk in chunks if chunk.get("content")]
        return "\n\n---\n\n".join(contents) if contents else ""

    except Exception as e:
        logger.warning(f"[load_l3_memory] 加载失败: {e}")
        return ""


# ==================== 第3层：消息层（原文层） ====================

async def load_recent_messages(store: Any) -> list[dict]:
    """
    加载最近消息列表（第3层 - L0 原文层）

    读取策略：
    1. 始终从数据库加载历史消息（确保重新连接后能获取历史消息）
    2. 与内存缓存 _messages 合并（确保刚添加的消息能被立即获取）
    3. 使用 store._reader 或 context_repository 从数据库读取

    设计原则：
    1. 数据库优先：重新连接后必须从数据库加载历史消息
    2. 内存合并：将内存中的新消息与数据库历史消息合并
    3. 压缩和读取分离，读取时不考虑预算
    4. 只读取未压缩的原文（即没有对应 L1 块的执行记录）
    """
    session_id = getattr(store, 'session_id', None)
    executor_id = getattr(store, 'executor_id', None)
    getattr(store, 'executor_type', None)

    logger.info(f"[load_recent_messages] 开始加载 | session_id={session_id} | executor_id={executor_id}")

    # 从数据库加载历史消息
    db_messages = []
    try:
        # 首先尝试使用 _reader 从数据库读取（有元数据索引）
        reader = getattr(store, '_reader', None)
        logger.info(f"[load_recent_messages] 检查 _reader | reader存在={reader is not None}")

        if reader is not None and hasattr(reader, 'read_message_layer'):
            logger.info(f"[load_recent_messages] 使用 _reader 读取消息层 | session_id={session_id}")
            db_messages = await reader.read_message_layer()
            logger.info(f"[load_recent_messages] _reader 返回 | 消息数={len(db_messages)}")
            for idx, msg in enumerate(db_messages):
                logger.info(f"[load_recent_messages] _reader 消息 #{idx} | role={msg.get('role')} | content={msg.get('content', '')[:50]}...")
        elif hasattr(store, 'context_repository') and store.context_repository:
            # 回退到 context_repository
            logger.info(f"[load_recent_messages] 使用 context_repository 读取消息层 | session_id={session_id}")
            db_messages = await store.context_repository.get_uncompressed_messages(
                session_id=session_id,
                executor_type=None,  # 不应用 executor_type 过滤
                executor_id=None     # 不应用 executor_id 过滤，获取会话所有消息
            )
            logger.info(f"[load_recent_messages] 从数据库读取原始消息 | 数量={len(db_messages)}")
            for idx, msg in enumerate(db_messages):
                logger.info(f"[load_recent_messages] 数据库消息 #{idx} | role={msg.get('role')} | executor_id={msg.get('executor_id')} | content={msg.get('content', '')[:50]}...")
        else:
            logger.warning("[LayerLoader] context_repository 未设置，无法加载消息")
    except Exception as e:
        logger.warning(f"[load_recent_messages] 从数据库加载失败: {e}")
        db_messages = []

    # BUG-FIX-fix_20260226_tool_callback: 合并内存缓存和数据库消息
    # 问题根因: load_recent_messages 只从数据库加载消息，忽略了内存缓存中的工具消息
    # 修复方案: 将内存缓存中的消息与数据库历史消息合并，确保工具执行结果能传递给 LLM
    memory_messages = getattr(store, '_messages', [])
    logger.info(f"[load_recent_messages] 内存缓存消息数={len(memory_messages)}")

    # 合并策略：
    # 1. 数据库消息是历史消息（已持久化）
    # 2. 内存缓存消息是最新消息（可能还未持久化，如工具执行结果）
    # 3. 使用 content + role + tool_call_id 作为去重键
    merged_messages = list(db_messages)  # 复制数据库消息

    # 构建已存在消息的唯一标识集合
    existing_keys = set()
    for msg in db_messages:
        key = f"{msg.get('role')}:{msg.get('content', '')[:100]}:{msg.get('tool_call_id', '')}"
        existing_keys.add(key)

    # 添加内存缓存中不存在的新消息
    for msg in memory_messages:
        key = f"{msg.get('role')}:{msg.get('content', '')[:100]}:{msg.get('tool_call_id', '')}"
        if key not in existing_keys:
            merged_messages.append(msg)
            existing_keys.add(key)
            logger.info(f"[load_recent_messages] 从内存缓存添加消息 | role={msg.get('role')} | content={msg.get('content', '')[:50]}...")

    logger.info(f"[load_recent_messages] 合并后消息数 | 数据库={len(db_messages)} | 内存={len(memory_messages)} | 合并后={len(merged_messages)}")

    # 按执行者过滤消息
    if executor_id and merged_messages:
        filtered_messages = []
        for msg in merged_messages:
            msg_executor_id = msg.get('executor_id')
            # 如果消息没有 executor_id，或者匹配当前 executor_id，则保留
            if not msg_executor_id or msg_executor_id == executor_id:
                filtered_messages.append(msg)
            else:
                logger.info(f"[load_recent_messages] 过滤消息 | 消息executor_id={msg_executor_id} != 当前executor_id={executor_id}")
        merged_messages = filtered_messages
        logger.info(f"[load_recent_messages] 过滤后消息数 | 原始={len(merged_messages) + len([m for m in merged_messages if m.get('executor_id') and m.get('executor_id') != executor_id])} | 过滤后={len(merged_messages)}")

    # 按时间排序（假设消息中有 created_at 或按添加顺序）
    # 如果没有时间戳，保持当前顺序
    try:
        merged_messages.sort(key=lambda m: m.get('created_at', '') or '')
    except Exception:
        pass  # 如果排序失败，保持原顺序

    return merged_messages


# ==================== 加载器映射表 ====================

LOADER_MAP: dict[str, Any] = {
    # 第1层：系统静态层
    "system_prompt": load_system_prompt,
    "tools_description": load_tools_description,
    "static_vars": load_static_vars,
    # 第2层：压缩层
    "l1_memory": load_l1_memory,
    "l2_memory": load_l2_memory,
    "l3_memory": load_l3_memory,
    # 第3层：消息层
    "recent_messages": load_recent_messages,
}


async def load_layer(store: Any, layer_name: str) -> Any:
    """
    加载指定层内容

    Args:
        store: LayeredContextStore 实例
        layer_name: 层名称

    Returns:
        层内容（临时变量，函数结束后销毁）

    Raises:
        KeyError: 如果层名称未注册
    """
    loader = LOADER_MAP.get(layer_name)
    if loader is None:
        raise KeyError(f"未找到层 '{layer_name}' 的加载器")
    return await loader(store)


__all__ = [
    # 第1层
    "load_system_prompt",
    "load_tools_description",
    "load_static_vars",
    # 第2层
    "load_l1_memory",
    "load_l2_memory",
    "load_l3_memory",
    # 第3层
    "load_recent_messages",
    # 通用
    "load_layer",
    "LOADER_MAP",
]
