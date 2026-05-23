"""复盘引擎 —— 筛选、分批、骨架、锚点、LLM 复盘、规则复盘。

负责复盘周期的全部逻辑：
1. 筛选待复盘管道
2. 按骨架 token 预算分批
3. 生成骨架和锚点（纯规则）
4. 调用 pipeline_engine 启动 LLM 复盘（降级为规则复盘）
5. 解析并存储复盘产出到 Knowledge
6. 标记管道已复盘
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


class ReviewEngine:
    """复盘引擎，负责分析管道执行记录并产出经验/改进建议。

    Attributes:
        _storage: 执行记录存储（ExecutionRecordStorage）
        _chunk_db: 压缩块服务（ChunkService）
        _knowledge_service: 知识服务（KnowledgeService）
        _pipeline_engine: 管道引擎（PipelineEngine）
        _config: 维护配置（MaintenanceConfig）
    """

    def __init__(
        self,
        storage: Any,
        chunk_db: Any,
        knowledge_service: Any,
        pipeline_engine: Any | None,
        config: Any,
    ) -> None:
        """初始化复盘引擎。

        Args:
            storage: 执行记录存储实例（ExecutionRecordStorage）
            chunk_db: 压缩块服务实例（ChunkService）
            knowledge_service: 知识服务实例（KnowledgeService）
            pipeline_engine: 管道引擎实例（PipelineEngine），可为 None
            config: 维护配置实例（MaintenanceConfig）
        """
        self._storage = storage
        self._chunk_db = chunk_db
        self._knowledge_service = knowledge_service
        self._pipeline_engine = pipeline_engine
        self._config = config

    # ============================================
    # 复盘主流程
    # ============================================

    async def review_execution_history(self) -> dict[str, Any]:
        """复盘核心流程。

        流程：
        1. 筛选待复盘管道（读 summary 列表，找 review_status="pending"）
        2. 按骨架 token 预算分批
        3. 对每批：
           a. 生成骨架（纯规则）
           b. 生成锚点（纯规则）
           c. 调用 pipeline_engine.run() 启动复盘管道
           d. 解析复盘产出
        4. 存储产出到 Knowledge
        5. 更新 review_status = "reviewed"（L0 和 L1 都标记）
        6. 通知用户

        Returns:
            复盘结果字典
        """
        now = datetime.now(UTC)
        result: dict[str, Any] = {
            "status": "success",
            "pipelines_reviewed": 0,
            "records_analyzed": 0,
            "experiences_saved": 0,
            "batches": 0,
            "errors": [],
        }

        # Phase 1：筛选待复盘管道
        pending_pipelines = self._get_pending_pipelines()
        if not pending_pipelines:
            result["status"] = "skipped"
            result["reason"] = "no pending pipelines"
            return result

        # 限制单次复盘最大记录数
        total_records = sum(p.total_records for p in pending_pipelines)
        if total_records > self._config.max_records_per_review:
            # 按时间排序，优先处理最早的
            pending_pipelines.sort(key=lambda p: p.created_at)
            accumulated = 0
            trimmed = []
            for p in pending_pipelines:
                if accumulated + p.total_records > self._config.max_records_per_review:
                    break
                trimmed.append(p)
                accumulated += p.total_records
            pending_pipelines = trimmed
            total_records = accumulated

        result["records_analyzed"] = total_records

        # Phase 2：按骨架 token 预算分批
        batches = self._plan_review_batches(pending_pipelines)
        result["batches"] = len(batches)

        # Phase 3+4：逐批复盘
        all_experiences: list[dict[str, Any]] = []
        all_agent_reviews: dict[str, Any] = {}
        all_system_reviews: list[dict[str, Any]] = []

        for batch_idx, batch in enumerate(batches):
            try:
                batch_result = await self._review_batch(batch, batch_idx)
                if batch_result:
                    all_experiences.extend(batch_result.get("experiences", []))
                    all_agent_reviews.update(batch_result.get("agent_reviews", {}))
                    all_system_reviews.extend(batch_result.get("system_reviews", []))
            except Exception as e:
                logger.warning(
                    "[Maintenance] 复盘批次 %d 失败: %s", batch_idx, e,
                )
                result["errors"].append(f"batch_{batch_idx}: {e}")

        # Phase 5：存储产出到 Knowledge
        saved_count = await self._save_review_outputs(
            all_experiences, all_agent_reviews, all_system_reviews,
        )
        result["experiences_saved"] = saved_count

        # Phase 5：标记已复盘
        reviewed_count = 0
        for p in pending_pipelines:
            try:
                self._mark_pipeline_reviewed(p.run_id, now)
                reviewed_count += 1
            except Exception as e:
                logger.warning(
                    "[Maintenance] 标记复盘状态失败 | pipeline=%s | error=%s",
                    p.run_id[:12], e,
                )
        result["pipelines_reviewed"] = reviewed_count
        # 附带时间戳，供 service 更新统计
        result["reviewed_at"] = now.isoformat()

        logger.info(
            "[Maintenance] 复盘完成 | pipelines=%d | records=%d | "
            "batches=%d | experiences=%d",
            reviewed_count, total_records, len(batches), saved_count,
        )
        return result

    # ============================================
    # 筛选与分批
    # ============================================

    def _get_pending_pipelines(self) -> list[Any]:
        """筛选所有待复盘的管道。

        只加载 summary，不加载 records。

        Returns:
            review_status="pending" 的 PipelineRunSummary 列表
        """
        summaries = self._storage.list_all_summaries()
        pending = [
            s for s in summaries
            if getattr(s, "review_status", "pending") == "pending"
            and s.total_records > 0
        ]
        # 异常优先：有 error 的排前面
        pending.sort(
            key=lambda s: (0 if s.error else 1, s.created_at),
        )
        return pending

    def _count_pending_records(self) -> int:
        """统计所有待复盘管道的执行记录总数。

        Returns:
            待复盘记录总数
        """
        summaries = self._storage.list_all_summaries()
        return sum(
            s.total_records
            for s in summaries
            if getattr(s, "review_status", "pending") == "pending"
        )

    def _plan_review_batches(self, pending_pipelines: list[Any]) -> list[list[Any]]:
        """按执行记录条数规划复盘批次。

        不是按管道数分批，而是按骨架占上下文的百分比分批。
        骨架预算 = context_size x skeleton_budget_percent / 100
        每批最多 = 预算 / records_per_skeleton_token 条记录

        Args:
            pending_pipelines: 待复盘的管道 summary 列表

        Returns:
            分批后的管道列表的列表
        """
        # 动态计算预算
        context_size = self._get_context_window_size()
        budget = int(context_size * self._config.skeleton_budget_percent / 100)
        max_records = max(1, budget // self._config.records_per_skeleton_token)

        batches: list[list[Any]] = []
        current_batch: list[Any] = []
        current_records = 0

        for pipeline in sorted(pending_pipelines, key=lambda p: p.total_records):
            # 单条管道就超预算？单独成批
            if pipeline.total_records > max_records:
                if current_batch:
                    batches.append(current_batch)
                    current_batch = []
                    current_records = 0
                batches.append([pipeline])
                continue

            # 加入当前批次会超预算？开新批
            if current_records + pipeline.total_records > max_records:
                batches.append(current_batch)
                current_batch = []
                current_records = 0

            current_batch.append(pipeline)
            current_records += pipeline.total_records

        if current_batch:
            batches.append(current_batch)

        return batches

    def _get_context_window_size(self) -> int:
        """获取当前 LLM 的上下文窗口大小。

        Returns:
            上下文窗口 token 数
        """
        try:
            from infrastructure.service_provider import get_service_provider
            provider = get_service_provider()
            context_service = provider.get("context_service")
            if context_service and hasattr(context_service, "_config"):
                return context_service._config.get("context_window", 128000)
        except Exception:
            pass
        return 128000  # 默认 128K

    # ============================================
    # 复盘批次执行
    # ============================================

    async def _review_batch(
        self, batch: list[Any], batch_idx: int,
    ) -> dict[str, Any] | None:
        """对一批管道执行复盘。

        流程：
        1. 加载每条管道的 records
        2. 生成骨架和锚点
        3. 调用复盘 Agent（通过 pipeline_engine）
        4. 解析复盘产出

        Args:
            batch: 一批 PipelineRunSummary
            batch_idx: 批次索引

        Returns:
            复盘产出字典（含 experiences、agent_reviews、system_reviews）
        """
        # 加载 records 并生成骨架和锚点
        skeletons: dict[str, list[str]] = {}
        anchors: dict[str, list[dict]] = {}
        pipeline_records: dict[str, list[Any]] = {}

        for p in batch:
            records = self._storage.list_by_pipeline(p.run_id)
            pipeline_records[p.run_id] = records
            skeletons[p.run_id] = self._generate_skeleton(records, p)
            anchors[p.run_id] = self._generate_anchors(records)

        # 组装复盘上下文
        review_context = self._build_review_context(batch, skeletons, anchors)

        # 尝试通过 pipeline_engine 启动复盘管道
        if self._pipeline_engine is not None:
            try:
                review_output = await self._run_review_pipeline(review_context)
                if review_output:
                    return self._parse_review_output(review_output)
            except Exception as e:
                logger.warning(
                    "[Maintenance] 复盘管道执行失败，降级为规则复盘: %s", e,
                )

        # 降级：纯规则复盘（不调用 LLM）
        return self._rule_based_review(batch, skeletons, anchors, pipeline_records)

    # ============================================
    # 骨架生成
    # ============================================

    def _generate_skeleton(self, records: list[Any], summary: Any) -> list[str]:
        """从 records 生成骨架（纯规则，零 token 消耗）。

        每条记录生成一行骨架文本，格式示例：
        "[10] TOOL task_manage.delete -> OK"

        Args:
            records: 执行记录列表
            summary: 管道运行摘要

        Returns:
            骨架行列表
        """
        skeleton: list[str] = []
        for r in records:
            line = f"[{r.iteration}] {r.type}"
            if r.name:
                line += f" {r.name}"
            if r.error:
                line += f" -> ERR {r.error[:50]}"
            else:
                line += " -> OK"
            # 标记特殊类型
            if r.thinking_content and len(r.thinking_content) > 500:
                line += " [heavy_planning]"
            if r.tool_calls_json:
                try:
                    calls = json.loads(r.tool_calls_json)
                    if len(calls) > 3:
                        line += f" [batch:{len(calls)}]"
                except (json.JSONDecodeError, TypeError):
                    pass
            if r.content and len(r.content) > 2000:
                line += " [long_output]"
            skeleton.append(line)
        return skeleton

    # ============================================
    # 锚点生成
    # ============================================

    def _generate_anchors(self, records: list[Any]) -> list[dict]:
        """从 records 生成锚点标记（纯规则）。

        锚点类型：
        - error: record.error is not None
        - heavy_planning: thinking_content 长度 > 500字
        - batch_operation: 单轮 tool_calls 数量 > 3
        - long_output: content 长度 > 2000字
        - error_recovery: error 后下一轮为成功操作

        Args:
            records: 执行记录列表

        Returns:
            锚点字典列表
        """
        anchors: list[dict] = []
        prev_had_error = False

        for r in records:
            # error 锚点
            if r.error:
                anchors.append({
                    "sequence": r.sequence,
                    "iteration": r.iteration,
                    "anchor_type": "error",
                    "reason": r.error[:100],
                    "preview": (r.content or "")[:50],
                })
                prev_had_error = True
                continue

            # error_recovery 锚点
            if prev_had_error:
                anchors.append({
                    "sequence": r.sequence,
                    "iteration": r.iteration,
                    "anchor_type": "error_recovery",
                    "reason": "error 后成功恢复",
                    "preview": (r.content or "")[:50],
                })
                prev_had_error = False

            # heavy_planning 锚点
            if r.thinking_content and len(r.thinking_content) > 500:
                anchors.append({
                    "sequence": r.sequence,
                    "iteration": r.iteration,
                    "anchor_type": "heavy_planning",
                    "reason": f"深度思考 {len(r.thinking_content)} 字",
                    "preview": r.thinking_content[:50],
                })

            # batch_operation 锚点
            if r.tool_calls_json:
                try:
                    calls = json.loads(r.tool_calls_json)
                    if len(calls) > 3:
                        anchors.append({
                            "sequence": r.sequence,
                            "iteration": r.iteration,
                            "anchor_type": "batch_operation",
                            "reason": f"批量操作 {len(calls)} 次工具调用",
                            "preview": "",
                        })
                except (json.JSONDecodeError, TypeError):
                    pass

            # long_output 锚点
            if r.content and len(r.content) > 2000:
                anchors.append({
                    "sequence": r.sequence,
                    "iteration": r.iteration,
                    "anchor_type": "long_output",
                    "reason": f"长输出 {len(r.content)} 字",
                    "preview": r.content[:50],
                })

        return anchors

    # ============================================
    # 复盘上下文组装
    # ============================================

    def _build_review_context(
        self,
        batch: list[Any],
        skeletons: dict[str, list[str]],
        anchors: dict[str, list[dict]],
    ) -> str:
        """组装复盘上下文文本。

        将骨架和锚点格式化为复盘 Agent 可读的文本。

        Args:
            batch: 一批 PipelineRunSummary
            skeletons: 管道骨架映射
            anchors: 管道锚点映射

        Returns:
            格式化的复盘上下文文本
        """
        parts: list[str] = []
        for p in batch:
            parts.append(f"=== 管道 {p.run_id[:12]} ===")
            parts.append(f"状态: {p.status} | 迭代: {p.total_iterations} | 记录: {p.total_records}")
            if p.error:
                parts.append(f"最终错误: {p.error}")
            parts.append("")

            # 骨架
            skel = skeletons.get(p.run_id, [])
            if skel:
                parts.append("--- 骨架 ---")
                parts.extend(skel)
                parts.append("")

            # 锚点
            anc = anchors.get(p.run_id, [])
            if anc:
                parts.append("--- 锚点 ---")
                for a in anc:
                    parts.append(
                        f"  [{a['iteration']}] {a['anchor_type']}: {a['reason']}"
                    )
                parts.append("")

        return "\n".join(parts)

    # ============================================
    # LLM 复盘管道
    # ============================================

    async def _run_review_pipeline(self, review_context: str) -> str | None:
        """通过 pipeline_engine 启动复盘管道。

        Args:
            review_context: 复盘上下文文本

        Returns:
            复盘 Agent 的输出文本
        """
        if self._pipeline_engine is None:
            return None

        try:
            # 加载复盘 Agent 配置
            agent_config = self._load_review_agent_config()
            if agent_config is None:
                logger.warning("[Maintenance] 复盘 Agent 配置不可用，跳过管道执行")
                return None

            state = await self._pipeline_engine.run(
                user_input=review_context,
                agent_config=agent_config,
                allow_default_fallback=False,
            )
            return state.get("raw_result", "")
        except Exception as e:
            logger.warning("[Maintenance] 复盘管道执行异常: %s", e)
            return None

    def _load_review_agent_config(self) -> Any | None:
        """加载复盘 Agent 配置。

        从 config/agents/review_agent.yaml 加载配置。

        Returns:
            Agent 配置实例，不可用则返回 None
        """
        try:
            from config.agent_loader import load_agent_config
            return load_agent_config("review_agent")
        except Exception:
            return None

    # ============================================
    # 复盘产出解析
    # ============================================

    def _parse_review_output(self, output: str) -> dict[str, Any]:
        """解析复盘 Agent 的 JSON 输出。

        Args:
            output: Agent 输出的 JSON 文本

        Returns:
            解析后的复盘产出字典
        """
        try:
            # 尝试从输出中提取 JSON
            json_start = output.find("{")
            json_end = output.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                json_str = output[json_start:json_end]
                return json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            pass

        # 解析失败，返回空结果
        return {
            "experiences": [],
            "agent_reviews": {},
            "system_reviews": [],
        }

    # ============================================
    # 规则复盘（降级方案）
    # ============================================

    def _rule_based_review(
        self,
        batch: list[Any],
        skeletons: dict[str, list[str]],
        anchors: dict[str, list[dict]],
        pipeline_records: dict[str, list[Any]],
    ) -> dict[str, Any]:
        """纯规则复盘（降级方案，不调用 LLM）。

        基于锚点分析自动产出经验和改进建议。

        Args:
            batch: 一批 PipelineRunSummary
            skeletons: 管道骨架映射
            anchors: 管道锚点映射
            pipeline_records: 管道执行记录映射

        Returns:
            规则复盘产出字典
        """
        experiences: list[dict[str, Any]] = []
        system_reviews: list[dict[str, Any]] = []

        # 统计锚点模式
        error_count = 0
        recovery_count = 0
        heavy_planning_count = 0
        batch_op_count = 0

        for p in batch:
            anc = anchors.get(p.run_id, [])
            for a in anc:
                if a["anchor_type"] == "error":
                    error_count += 1
                elif a["anchor_type"] == "error_recovery":
                    recovery_count += 1
                elif a["anchor_type"] == "heavy_planning":
                    heavy_planning_count += 1
                elif a["anchor_type"] == "batch_operation":
                    batch_op_count += 1

        # 基于锚点模式生成经验
        if error_count > 0 and recovery_count > 0:
            experiences.append({
                "agent_id": "__system__",
                "content": f"在 {len(batch)} 条管道中发现 {error_count} 次错误，"
                           f"其中 {recovery_count} 次成功恢复。"
                           "建议：加强操作前的前置条件检查。",
                "tags": ["error_recovery", "auto_review"],
            })

        if heavy_planning_count > len(batch) * 2:
            experiences.append({
                "agent_id": "__system__",
                "content": f"频繁的深度思考（{heavy_planning_count} 次），"
                           "可能存在任务拆分不合理或决策路径复杂的问题。"
                           "建议：优化任务分解策略。",
                "tags": ["heavy_planning", "auto_review"],
            })

        if batch_op_count > 0:
            experiences.append({
                "agent_id": "__system__",
                "content": f"发现 {batch_op_count} 次批量操作，"
                           "建议：批量操作前增加预检查步骤。",
                "tags": ["batch_operation", "auto_review"],
            })

        return {
            "experiences": experiences,
            "agent_reviews": {},
            "system_reviews": system_reviews,
        }

    # ============================================
    # 复盘产出存储
    # ============================================

    async def _save_review_outputs(
        self,
        experiences: list[dict[str, Any]],
        agent_reviews: dict[str, Any],
        system_reviews: list[dict[str, Any]],
    ) -> int:
        """将复盘产出存储到 Knowledge。

        Args:
            experiences: 经验列表
            agent_reviews: Agent 改进建议
            system_reviews: 系统改进建议

        Returns:
            保存的知识条目数
        """
        saved = 0

        # 存储经验
        for exp in experiences:
            try:
                await self._knowledge_service.create_knowledge(
                    user_id="system",
                    content=exp.get("content", ""),
                    source_type="experience",
                    extra_data={
                        "tags": exp.get("tags", []),
                        "agent_id": exp.get("agent_id", ""),
                    },
                )
                saved += 1
            except Exception as e:
                logger.warning("[Maintenance] 存储经验失败: %s", e)

        # 存储 Agent 改进建议
        for agent_id, review in agent_reviews.items():
            try:
                issues = review.get("issues", [])
                if issues:
                    content = f"Agent {agent_id} 改进建议：\n"
                    for issue in issues:
                        content += f"- {issue.get('pattern', '')}: {issue.get('suggestion', '')}\n"
                    await self._knowledge_service.create_knowledge(
                        user_id="system",
                        content=content,
                        source_type="review",
                        extra_data={"agent_id": agent_id},
                    )
                    saved += 1
            except Exception as e:
                logger.warning(
                    "[Maintenance] 存储 Agent 改进建议失败 | agent=%s | error=%s",
                    agent_id, e,
                )

        # 存储系统改进建议
        for sys_review in system_reviews:
            try:
                await self._knowledge_service.create_knowledge(
                    user_id="system",
                    content=sys_review.get("suggestion", ""),
                    source_type="system_review",
                    extra_data={"issue": sys_review.get("issue", "")},
                )
                saved += 1
            except Exception as e:
                logger.warning("[Maintenance] 存储系统改进建议失败: %s", e)

        # 存储复盘报告摘要
        if experiences or agent_reviews or system_reviews:
            try:
                report = self._build_review_report(
                    experiences, agent_reviews, system_reviews,
                )
                await self._knowledge_service.create_knowledge(
                    user_id="system",
                    content=report,
                    source_type="review_report",
                    extra_data={
                        "experience_count": len(experiences),
                        "agent_count": len(agent_reviews),
                        "system_review_count": len(system_reviews),
                    },
                )
                saved += 1
            except Exception as e:
                logger.warning("[Maintenance] 存储复盘报告失败: %s", e)

        return saved

    def _build_review_report(
        self,
        experiences: list[dict[str, Any]],
        agent_reviews: dict[str, Any],
        system_reviews: list[dict[str, Any]],
    ) -> str:
        """构建复盘报告文本。

        Args:
            experiences: 经验列表
            agent_reviews: Agent 改进建议
            system_reviews: 系统改进建议

        Returns:
            格式化的复盘报告文本
        """
        parts: list[str] = ["执行复盘报告", "=" * 40, ""]

        if experiences:
            parts.append(f"沉淀经验 ({len(experiences)} 条):")
            for exp in experiences:
                parts.append(f"  - {exp.get('content', '')[:100]}")
            parts.append("")

        if agent_reviews:
            parts.append("Agent 改进建议:")
            for agent_id, review in agent_reviews.items():
                parts.append(f"  {agent_id}:")
                for issue in review.get("issues", []):
                    parts.append(f"    - {issue.get('pattern', '')}")
            parts.append("")

        if system_reviews:
            parts.append("系统改进建议:")
            for sr in system_reviews:
                parts.append(f"  - {sr.get('issue', '')}: {sr.get('suggestion', '')}")
            parts.append("")

        return "\n".join(parts)

    # ============================================
    # 复盘标记
    # ============================================

    def _mark_pipeline_reviewed(self, pipeline_id: str, now: datetime) -> None:
        """标记管道为已复盘（L0 summary 和 L1 块都标记）。

        Args:
            pipeline_id: 管道运行 ID
            now: 当前时间
        """
        # 标记 L0 summary
        self._storage.update_summary(pipeline_id, {
            "review_status": "reviewed",
            "reviewed_at": now.isoformat(),
        })

        # 标记 L1 块
        try:
            import asyncio
            chunks = asyncio.get_event_loop().run_until_complete(
                self._chunk_db.find_by_pipeline(pipeline_id, layer="L1"),
            )
            for chunk in chunks:
                chunk.extra_data = getattr(chunk, "extra_data", None) or {}
                if isinstance(chunk.extra_data, dict):
                    chunk.extra_data["review_status"] = "reviewed"
                    chunk.extra_data["reviewed_at"] = now.isoformat()
                # 持久化
                self._chunk_db._save_to_disk(chunk)
        except Exception as e:
            logger.debug(
                "[Maintenance] 标记 L1 块复盘状态失败（非致命）: %s", e,
            )

    # ============================================
    # 单条管道复盘
    # ============================================

    async def _review_single_pipeline(self, pipeline_id: str) -> None:
        """复盘单条管道。

        Args:
            pipeline_id: 管道运行 ID
        """
        summary = self._storage.get_summary(pipeline_id)
        if summary is None:
            return

        records = self._storage.list_by_pipeline(pipeline_id)
        if not records:
            self._mark_pipeline_reviewed(pipeline_id, datetime.now(UTC))
            return

        # 生成骨架和锚点
        skeleton = self._generate_skeleton(records, summary)
        anchors = self._generate_anchors(records)

        # 规则复盘
        review_result = self._rule_based_review(
            [summary],
            {pipeline_id: skeleton},
            {pipeline_id: anchors},
            {pipeline_id: records},
        )

        # 存储
        await self._save_review_outputs(
            review_result.get("experiences", []),
            review_result.get("agent_reviews", {}),
            review_result.get("system_reviews", []),
        )

        # 标记已复盘
        self._mark_pipeline_reviewed(pipeline_id, datetime.now(UTC))
