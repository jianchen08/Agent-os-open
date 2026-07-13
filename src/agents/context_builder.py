"""Agent 上下文构建器。

根据 Agent 配置中的 static_vars 和 dynamic_vars 构建上下文字典，
用于注入到 LLM 的状态中。

静态上下文（第1层）在会话级别不变，可缓存；
动态上下文（第4层）每轮变化，实时生成。

支持的变量类型：
- rules: 从配置文件读取规则内容
- path: 从指定路径读取文件内容
- folder: 从指定文件夹路径自动加载所有文件内容（可通过 extensions 过滤）
- timestamp: 动态生成当前时间戳
- session: 动态生成会话信息
- agent: 动态生成当前 Agent 信息
- model: 动态生成当前模型信息
- retrieval: 基于标签的知识库检索
- routed: 根据运行时 state 中的 key 路由到不同内容

典型用法::

    from agents.context_builder import ContextBuilder

    builder = ContextBuilder()
    static = builder.build_static_context(config)
    dynamic = builder.build_dynamic_context(config)
    full = builder.build_full_context(config)
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from pathlib import Path
from typing import Any

from .types import AgentConfig, ContextConfig, ContextVarItem

logger = logging.getLogger(__name__)


class ContextBuilder:
    """Agent 上下文构建器，从 static_vars/dynamic_vars 配置生成上下文字典。

    对于 rules 和 path 类型的变量，会尝试读取文件内容注入；
    对于 timestamp/session/agent/model 类型，在运行时动态生成。
    """

    def __init__(self, base_path: str | Path | None = None) -> None:
        """初始化上下文构建器。

        Args:
            base_path: 文件搜索的基础路径，用于解析 path 类型的变量。
                       默认为当前工作目录。
        """
        self._base_path = Path(base_path) if base_path else Path.cwd()

    # 上下文文件大小上限（10MB），超过则跳过
    _MAX_CONTEXT_FILE_SIZE = 10 * 1024 * 1024

    def _resolve_path_content(self, file_path: str) -> str:
        """尝试读取文件内容。

        Args:
            file_path: 相对于 base_path 的文件路径。

        Returns:
            文件内容字符串，读取失败返回空字符串。
        """
        full_path = self._base_path / file_path
        try:
            if full_path.exists() and full_path.is_file():
                if full_path.stat().st_size > self._MAX_CONTEXT_FILE_SIZE:
                    logger.warning(
                        "上下文文件过大，跳过: %s (%d bytes)",
                        full_path,
                        full_path.stat().st_size,
                    )
                    return ""
                return full_path.read_text(encoding="utf-8")
        except OSError as e:
            logger.warning("读取文件失败 %s: %s", full_path, e)
        return ""

    def _resolve_folder_content(
        self,
        folder_path: str,
        extensions: list[str] | None = None,
    ) -> str:
        """读取文件夹中所有文件的内容，合并为一个字符串。

        遍历文件夹下的所有文件（非递归），按文件名排序，
        将每个文件的内容以 ``--- filename ---`` 分隔拼接。
        超过大小限制的文件会被跳过。

        Args:
            folder_path: 相对于 base_path 的文件夹路径。
            extensions: 文件扩展名过滤列表，如 [".py", ".md"]。
                        为空或 None 则不过滤。

        Returns:
            合并后的内容字符串，读取失败返回空字符串。
        """
        full_path = self._base_path / folder_path
        if not folder_path or not folder_path.strip():
            # 空 path 不允许回退到 base_path，直接跳过
            return ""
        if not full_path.exists() or not full_path.is_dir():
            logger.warning("文件夹不存在或不是目录: %s", full_path)
            return ""

        ext_set = set(extensions) if extensions else None
        parts: list[str] = []

        try:
            entries = sorted(full_path.iterdir(), key=lambda p: p.name)
        except OSError as e:
            logger.warning("遍历文件夹失败 %s: %s", full_path, e)
            return ""

        for entry in entries:
            if not entry.is_file():
                continue
            if ext_set and entry.suffix.lower() not in ext_set:
                continue
            try:
                if entry.stat().st_size > self._MAX_CONTEXT_FILE_SIZE:
                    logger.warning(
                        "上下文文件过大，跳过: %s (%d bytes)",
                        entry,
                        entry.stat().st_size,
                    )
                    continue
                content = entry.read_text(encoding="utf-8")
                parts.append(f"--- {entry.name} ---\n{content}")
            except OSError as e:
                logger.warning("读取文件失败 %s: %s", entry, e)

        return "\n\n".join(parts)

    def _build_item_value(self, item: ContextVarItem) -> dict[str, Any]:  # noqa: PLR0912,PLR0915
        """构建单个上下文变量项的值。

        Args:
            item: 上下文变量项。

        Returns:
            包含变量名和值的字典。
        """
        result: dict[str, Any] = {"name": item.name or "unnamed"}

        if item.content:
            # 直接内容注入
            result["content"] = item.content
            result["type"] = "inline"
            return result

        var_type = item.type

        if var_type == "rules":
            # 行为约束规则 — 从 hard_constraints 和 soft_constraints 获取
            result["type"] = "rules"
            result["content"] = ""  # 实际内容需要从 AgentConfig 中提取
        elif var_type == "path":
            # 文件路径读取
            content = self._resolve_path_content(item.path)
            result["type"] = "path"
            result["path"] = item.path
            result["content"] = content
        elif var_type == "folder":
            # 文件夹内容读取 — 自动加载目录下所有文件
            content = self._resolve_folder_content(item.path, item.extensions or None)
            result["type"] = "folder"
            result["path"] = item.path
            result["content"] = content
            if item.extensions:
                result["extensions"] = item.extensions
        elif var_type == "timestamp":
            # 动态时间戳
            now = datetime.datetime.now(datetime.timezone.utc)
            result["type"] = "timestamp"
            result["content"] = now.isoformat()
        elif var_type == "session":
            # 会话信息（运行时动态生成）
            result["type"] = "session"
            result["content"] = {"session_id": "", "round": 0}
        elif var_type == "agent":
            # 当前 Agent 信息
            result["type"] = "agent"
            result["content"] = {"agent_id": "", "agent_name": ""}
        elif var_type == "model":
            # 当前模型信息
            result["type"] = "model"
            result["content"] = {"model_id": "", "provider": ""}
        elif var_type == "routed":
            # 路由变量 — 根据运行时 state 中的 key 值选择注入内容
            result["type"] = "routed"
            result["route_key"] = item.route_key
            result["routes"] = item.routes
            result["content"] = ""
        elif item.tags:
            # 基于标签的知识库检索
            result["type"] = "retrieval"
            result["tags"] = item.tags
            result["inject_type"] = item.inject_type
            result["top_k"] = item.top_k
            if item.memory_type:
                result["memory_type"] = item.memory_type
            if item.memory_layer:
                result["memory_layer"] = item.memory_layer
            result["content"] = ""  # 检索结果运行时填充
        else:
            result["type"] = var_type or "unknown"
            result["content"] = ""

        return result

    def _build_context(self, config: ContextConfig, config_obj: AgentConfig) -> dict[str, Any]:
        """从 ContextConfig 构建上下文字典。

        Args:
            config: 上下文配置。
            config_obj: Agent 配置（用于提取约束等额外信息）。

        Returns:
            上下文字典。
        """
        if not config.enabled:
            return {"enabled": False, "items": []}

        items: list[dict[str, Any]] = []
        for item in config.items:
            item_value = self._build_item_value(item)
            # 对于 rules 类型，补充约束内容
            if item.type == "rules":
                rules_content = "\n".join(config_obj.hard_constraints + config_obj.soft_constraints)
                item_value["content"] = rules_content
            items.append(item_value)

        return {"enabled": True, "items": items}

    def build_static_context(self, config: AgentConfig) -> dict[str, Any]:
        """构建静态上下文（第1层，会话级不变，可缓存）。

        Args:
            config: Agent 配置。

        Returns:
            静态上下文字典。
        """
        return self._build_context(config.static_vars, config)

    def build_dynamic_context(self, config: AgentConfig) -> dict[str, Any]:
        """构建动态上下文（第4层，每轮变化，实时生成）。

        Args:
            config: Agent 配置。

        Returns:
            动态上下文字典。
        """
        return self._build_context(config.dynamic_vars, config)

    def build_full_context(self, config: AgentConfig) -> dict[str, Any]:
        """构建完整上下文（静态 + 动态合并）。

        Args:
            config: Agent 配置。

        Returns:
            合并后的完整上下文字典。
        """
        static = self.build_static_context(config)
        dynamic = self.build_dynamic_context(config)
        return {
            "static": static,
            "dynamic": dynamic,
        }

    # ---- 异步方法 ----

    async def _resolve_path_content_async(self, file_path: str) -> str:
        """异步版本的文件内容读取。"""
        return await asyncio.to_thread(self._resolve_path_content, file_path)

    async def _resolve_folder_content_async(self, folder_path: str, extensions: list[str] | None = None) -> str:
        """异步版本的文件夹内容读取。"""
        return await asyncio.to_thread(self._resolve_folder_content, folder_path, extensions)

    async def build_static_context_async(self, config: AgentConfig) -> dict[str, Any]:
        """异步构建静态上下文，文件读取卸载到线程池。"""
        return await asyncio.to_thread(self.build_static_context, config)

    async def build_dynamic_context_async(self, config: AgentConfig) -> dict[str, Any]:
        """异步构建动态上下文。"""
        return await asyncio.to_thread(self.build_dynamic_context, config)

    async def build_full_context_async(self, config: AgentConfig) -> dict[str, Any]:
        """异步构建完整上下文。"""
        static = await self.build_static_context_async(config)
        dynamic = await self.build_dynamic_context_async(config)
        return {"static": static, "dynamic": dynamic}
