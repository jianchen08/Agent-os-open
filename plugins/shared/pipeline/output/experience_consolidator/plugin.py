"""经验沉淀输出插件（Step 5c 重建版）。

在任务完成时，自动从 ChunkData（压缩块）提炼可检索的知识。
触发条件：state 中 task_complete=True 或 execution_status="completed"。

数据流：
  context_window_guard 产出 ChunkData → 本插件读取 → 提炼 Knowledge → 存储

本插件 Step 5c 重建要点（相对 0.1 的变化）：
- 存储/检索由 ctx.get_service("chunk_service") / ctx.get_service("knowledge_service")
  改为模块级 `_memory_backend: IMemoryBackend`（Hindsight/Kernel 后端），通过
  `set_memory_backend()` 注入——0.2 的 PluginContext 无服务注册表，
  get_service 恒抛 KeyError，老实现因此永远静默 no-op。
- 读取压缩块：`backend.search(query="", user_id, top_k=50, memory_type="chunk")`，
  并按 metadata.tags 中的 `pipeline:{pipeline_run_id}` 标签客户端过滤
  （chunks 由 context_window_guard 以该标签写入）。
- 写入知识：`backend.add(user_id, content, memory_type="experience", tags, source="consolidation")`。

State 命名空间：
    - experience_consolidated : 是否沉淀成功
    - knowledge_id : 生成的知识 ID（最后一条）
    - knowledge_ids : 生成的知识 ID 列表
"""

from __future__ import annotations

import logging
from typing import Any

from pipeline.plugin import IOutputPlugin, OutputResult, PluginContext
from pipeline.types import StateKeys

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════
# 模块级依赖注入（由 server.py 的 on_load 注入，测试直接赋值）
# ═══════════════════════════════════════════════════════════

# 长期记忆后端（IMemoryBackend，Hindsight/capability）；None 时无法检索/沉淀，早退
_memory_backend: Any | None = None


def set_memory_backend(backend: Any | None) -> None:
    """注入 IMemoryBackend 实例（由 server.py 在 on_load 时注入）。

    Args:
        backend: 实现 add/search/delete/import_document 的后端实例
            （HindsightBackend / KernelMemoryBackend 或 duck-type）；传 None 清空
    """
    global _memory_backend
    _memory_backend = backend


class ExperienceConsolidatorPlugin(IOutputPlugin):
    """经验沉淀输出插件。

    在任务完成时，从 ChunkData（压缩块）提炼知识并存储。
    触发条件：state 中 task_complete=True 或 execution_status="completed"。

    压缩块经模块级 `_memory_backend`（IMemoryBackend）检索（memory_type="chunk"），
    知识以 memory_type="experience" 写入同一后端（source="consolidation"）。

    优先级：28（在 context_compress 之后）
    沉淀失败不影响当轮结果。
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        """初始化经验沉淀插件。

        Args:
            config: 插件配置，当前无特殊配置项
        """
        self._config = config or {}

    @property
    def name(self) -> str:
        """插件唯一标识名称。"""
        return "experience_consolidator"

    @property
    def priority(self) -> int:
        """插件执行优先级。"""
        return 28

    @property
    def route_signals(self) -> list[str]:
        """本插件关注所有路由信号。"""
        return []

    async def execute(self, ctx: PluginContext) -> OutputResult:  # noqa: PLR0911
        """执行经验沉淀逻辑。

        检查任务是否完成，如果完成则从 ChunkData 提炼知识并存储。

        Args:
            ctx: 插件执行上下文

        Returns:
            包含沉淀结果的输出结果
        """
        # 1. 检查任务完成状态
        task_complete = ctx.state.get(StateKeys.TASK_COMPLETE, False)
        execution_status = ctx.state.get(StateKeys.EXECUTION_STATUS, "")
        if not task_complete and execution_status != "completed":
            return OutputResult()

        # 2. 获取 pipeline_run_id
        pipeline_run_id = ctx.state.get(StateKeys.PIPELINE_ID, "")
        if not pipeline_run_id:
            logger.debug(
                "[%s] 无 pipeline_run_id，跳过经验沉淀",
                self.name,
            )
            return OutputResult()

        # 3. 检查 memory backend 注入
        if _memory_backend is None:
            logger.debug(
                "[%s] memory backend 未注入，跳过经验沉淀",
                self.name,
            )
            return OutputResult(state_updates={"experience_consolidated": False})

        # 4. 从 backend 检索当前管道的 chunk 类记忆
        #    （chunks 由 context_window_guard 以 memory_type="chunk"、
        #    tags 含 "pipeline:{pipeline_run_id}" 写入，这里按标签客户端过滤）
        user_id = ctx.state.get("user_id", "") or pipeline_run_id
        try:
            results = await _memory_backend.search(
                query="",
                user_id=user_id,
                top_k=50,
                memory_type="chunk",
            )
        except Exception as e:
            logger.warning(
                "[%s] 获取压缩块失败 | pipeline_run_id=%s | error=%s",
                self.name,
                pipeline_run_id,
                e,
            )
            return OutputResult(state_updates={"experience_consolidated": False})

        chunks = [
            item
            for item in (results or [])
            if self._has_pipeline_tag(item, pipeline_run_id)
        ]
        if not chunks:
            logger.debug(
                "[%s] 无压缩块可沉淀 | pipeline_run_id=%s",
                self.name,
                pipeline_run_id,
            )
            return OutputResult(state_updates={"experience_consolidated": False})

        # 5. 从压缩块提炼知识
        knowledge_ids: list[str] = []
        errors: list[str] = []

        for chunk in chunks:
            content = self._extract_knowledge_content(chunk)
            if not content.strip():
                continue

            keywords = self._extract_keywords(chunk)
            layer = self._extract_layer(chunk)
            chunk_id = chunk.get("id", "") if isinstance(chunk, dict) else ""
            tags = [f"pipeline:{pipeline_run_id}", f"source_chunk:{chunk_id}"]
            if layer:
                tags.append(f"layer:{layer}")
            tags.extend(keywords[:10])

            try:
                kid = await _memory_backend.add(
                    user_id=user_id,
                    content=content,
                    memory_type="experience",
                    tags=tags,
                    source="consolidation",
                )
                if kid:
                    knowledge_ids.append(kid)
            except Exception as e:
                errors.append(str(e))
                logger.warning(
                    "[%s] 知识存储失败 | chunk_id=%s | error=%s",
                    self.name,
                    chunk_id,
                    e,
                )

        # 6. 更新 state
        if knowledge_ids:
            logger.info(
                "[%s] 经验沉淀成功 | pipeline_run_id=%s | chunks=%d | knowledge_ids=%s",
                self.name,
                pipeline_run_id,
                len(chunks),
                knowledge_ids,
            )
            return OutputResult(
                state_updates={
                    "experience_consolidated": True,
                    "knowledge_id": knowledge_ids[-1],
                    "knowledge_ids": knowledge_ids,
                },
            )
        logger.debug(
            "[%s] 经验沉淀失败 | pipeline_run_id=%s | chunks=%d | errors=%d",
            self.name,
            pipeline_run_id,
            len(chunks),
            len(errors),
        )
        return OutputResult(
            state_updates={
                "experience_consolidated": False,
            },
        )

    # ------------------------------------------------------------------
    # 辅助方法（统一形态条目：{id, content, score, memory_type, metadata}）
    # ------------------------------------------------------------------

    @staticmethod
    def _metadata_tags(item: Any) -> list[Any]:
        """从 chunk 条目提取 metadata.tags（兼容 dict 与对象形态）。

        Args:
            item: backend.search 返回的条目

        Returns:
            tags 列表；无 metadata/tags 时返回空列表
        """
        if isinstance(item, dict):
            meta = item.get("metadata")
        else:
            meta = getattr(item, "metadata", None)
        if not isinstance(meta, dict):
            return []
        tags = meta.get("tags")
        return tags if isinstance(tags, list) else []

    def _has_pipeline_tag(self, item: Any, pipeline_run_id: str) -> bool:
        """判定条目是否属于指定管道（metadata.tags 含 pipeline:{pipeline_run_id}）。

        Args:
            item: backend.search 返回的条目
            pipeline_run_id: 管道运行 ID

        Returns:
            属于该管道返回 True
        """
        expected = f"pipeline:{pipeline_run_id}"
        return any(
            isinstance(t, str) and t == expected
            for t in self._metadata_tags(item)
        )

    @staticmethod
    def _extract_keywords(item: Any) -> list[str]:
        """从 chunk 条目提取关键词列表。

        优先取条目顶层 keywords 字段，其次取 metadata.keywords；
        兼容 0.1 的 ChunkData.keywords 属性形态。

        Args:
            item: backend.search 返回的条目

        Returns:
            去空白后的关键词字符串列表
        """
        if isinstance(item, dict):
            keywords = item.get("keywords") or []
            if not keywords:
                meta = item.get("metadata")
                if isinstance(meta, dict):
                    keywords = meta.get("keywords") or []
        else:
            keywords = getattr(item, "keywords", []) or []
        return [str(k) for k in keywords if str(k).strip()]

    @staticmethod
    def _extract_layer(item: Any) -> str:
        """从 chunk 条目提取层级（L1/L2/STATE_SNAPSHOT）。

        新形态下层级记录在 metadata.tags 中；兼容 0.1 的 ChunkData.layer 属性。

        Args:
            item: backend.search 返回的条目

        Returns:
            层级名称；无则返回空串
        """
        for t in ExperienceConsolidatorPlugin._metadata_tags(item):
            if isinstance(t, str) and t in ("L1", "L2", "STATE_SNAPSHOT"):
                return t
        if isinstance(item, dict):
            return str(item.get("layer", "") or "")
        return str(getattr(item, "layer", "") or "")

    @staticmethod
    def _extract_knowledge_content(chunk: Any) -> str:
        """从 ChunkData 提炼知识内容。

        将压缩块的 content 和 keywords 组合为知识文本。

        Args:
            chunk: backend.search 返回的 chunk 条目
                （统一形态 dict，兼容 0.1 ChunkData 对象形态）

        Returns:
            拼接后的知识内容字符串
        """
        parts: list[str] = []

        if isinstance(chunk, dict):
            content = chunk.get("content", "")
        else:
            content = getattr(chunk, "content", "")
        if content:
            parts.append(str(content))

        keywords = ExperienceConsolidatorPlugin._extract_keywords(chunk)
        if keywords:
            parts.append(f"关键词: {', '.join(keywords)}")

        layer = ExperienceConsolidatorPlugin._extract_layer(chunk)
        if layer:
            parts.append(f"层级: {layer}")

        return "\n".join(parts)
