"""维护主服务 —— 调度、触发器、配置、入口。

复盘执行统一收敛到 B 路径（trigger_llm_review → review_agent LLM 深度分析）。
历史上存在的 A 路径（trigger_review_now 模板化经验提取 / 定时复盘触发器）已删除。

暴露接口：
- MaintenanceConfig: 维护配置数据类
- MemoryMaintenanceService: 记忆维护服务（门面类，委托 review_engine / cleanup_engine）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# 全局持有正在运行的复盘后台任务引用，防止 fire-and-forget task 被 GC 回收
# key 为 task 的 id()，value 为 asyncio.Task
_RUNNING_REVIEW_TASKS: dict[int, asyncio.Task] = {}


@dataclass
class MaintenanceConfig:
    """复盘驱动的维护配置。

    Attributes:
        enabled: 是否启用自动维护触发器
        skeleton_budget_percent: 骨架占上下文窗口的百分比（保留给 review_agent 读记录用）
        records_per_skeleton_token: 每条执行记录在骨架中约占的 token 数
        max_records_per_review: 单次复盘最大处理记录数
        cleanup_check_interval: 清理巡检间隔（秒）
        cleanup_min_age_days: 至少多少天才考虑清理
        cleanup_capacity_threshold: 容量使用率超过此值时提前清理
        cleanup_early_age_days: 容量紧张时，多少天以上的已复盘数据可清理
    """
    enabled: bool = False
    # 复盘配置（B 路径只保留预算相关项，不再有定时触发）
    skeleton_budget_percent: int = 15
    records_per_skeleton_token: int = 15
    max_records_per_review: int = 2000
    # 清理配置
    cleanup_check_interval: int = 86400     # 1 天
    cleanup_min_age_days: int = 30
    cleanup_capacity_threshold: float = 0.8
    cleanup_early_age_days: int = 7

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MaintenanceConfig:
        """从字典创建配置，未提供的字段使用默认值。

        支持嵌套的 review/cleanup 子配置合并到扁平结构。

        Args:
            data: 配置字典

        Returns:
            MaintenanceConfig 实例
        """
        flat: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, dict):
                # 嵌套配置展平（如 review.trigger.min_records）
                for sk, sv in v.items():
                    if isinstance(sv, dict):
                        for ssk, ssv in sv.items():
                            flat[ssk] = ssv
                    else:
                        flat[sk] = sv
            else:
                flat[k] = v
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in flat.items() if k in valid_keys}
        return cls(**filtered)


class MemoryMaintenanceService:
    """复盘驱动的记忆维护服务。

    两个维护职责：
    1. 复盘（B 路径）：trigger_review 工具触发 → 启动 review_agent 做 LLM 深度复盘
       → 产出报告 → 持久化 + 通知父管道
    2. 清理：定时巡检，按复盘状态/数据年龄/容量压力分层清理数据

    Attributes:
        _storage: 执行记录存储（ExecutionRecordStorage）
        _chunk_db: 压缩块服务（ChunkService）
        _knowledge_service: 知识服务（KnowledgeService）
        _config: 维护配置
        _stats: 维护操作统计
    """

    REVIEW_AGENT_ID = "review_agent"

    def __init__(
        self,
        storage: Any,
        chunk_db: Any,
        knowledge_service: Any,
        config: MaintenanceConfig | dict[str, Any] | None = None,
        memory_service: Any = None,
        task_lookup: Any | None = None,
    ) -> None:
        """初始化复盘驱动的记忆维护服务。

        Args:
            storage: 执行记录存储实例（ExecutionRecordStorage）
            chunk_db: 压缩块服务实例（ChunkService）
            knowledge_service: 知识服务实例（KnowledgeService）
            config: 维护配置，支持 MaintenanceConfig 实例、配置字典或 None
            memory_service: 记忆服务门面实例（用于索引重建等操作）
            task_lookup: 可选的任务反查回调，签名 (pipeline_run_id) -> dict | None。
                把 pipeline_run_id 反查到目标 agent 和任务标题，供复盘报告带身份。
                由 Application 装配时注入（闭包引用 task_service + root_map），
                不传时复盘报告不含 agent 身份。
        """
        self._storage = storage
        self._chunk_db = chunk_db
        self._knowledge_service = knowledge_service
        self._memory_service = memory_service
        self._task_lookup = task_lookup

        if config is None:
            self._config = MaintenanceConfig()
        elif isinstance(config, dict):
            self._config = MaintenanceConfig.from_dict(config)
        else:
            self._config = config

        self._stats: dict[str, Any] = {
            "last_review_at": None,
            "last_cleanup_at": None,
            "last_rebuild_at": None,
            "review_count": 0,
            "cleanup_count": 0,
            "rebuild_count": 0,
            "total_pipelines_reviewed": 0,
            "total_experiences_saved": 0,
            "total_pipelines_cleaned": 0,
        }

        # 延迟初始化子引擎（避免循环导入，按需创建）
        self._review_engine: Any | None = None
        self._cleanup_engine: Any | None = None

        # 复盘管道的触发来源（单次复盘周期内有效，由 _run_llm_review_task 设定）。
        # 用于注册复盘管道时打 tags 溯源：parent_pipeline / session_id。
        self._current_parent_pipeline: str = ""
        self._current_trigger_session: str = ""

    # ============================================
    # 子引擎访问（延迟初始化）
    # ============================================

    def _get_review_engine(self) -> Any:
        """获取复盘引擎实例（延迟初始化）。

        Returns:
            ReviewEngine 实例
        """
        if self._review_engine is None:
            from .review_engine import ReviewEngine  # noqa: PLC0415
            self._review_engine = ReviewEngine(
                storage=self._storage,
                chunk_db=self._chunk_db,
                knowledge_service=self._knowledge_service,
                task_lookup=self._task_lookup,
            )
        return self._review_engine

    def _get_cleanup_engine(self) -> Any:
        """获取清理引擎实例（延迟初始化）。

        Returns:
            CleanupEngine 实例
        """
        if self._cleanup_engine is None:
            from .cleanup_engine import CleanupEngine  # noqa: PLC0415
            self._cleanup_engine = CleanupEngine(
                storage=self._storage,
                chunk_db=self._chunk_db,
                memory_service=self._memory_service,
                config=self._config,
            )
        return self._cleanup_engine

    # ============================================
    # 触发器注册
    # ============================================

    def register_triggers(self) -> list[str]:
        """向 TriggerManager 注册清理巡检触发器。

        复盘不再走定时触发（A 路径已删除），统一由 trigger_review 工具按需触发。
        这里只注册一个定时清理触发器。

        Returns:
            注册的触发器 ID 列表
        """
        if not self._config.enabled:
            logger.info("[Maintenance] 自动维护未启用，跳过触发器注册")
            return []

        try:
            from triggers import TriggerConfig, TriggerManager  # noqa: PLC0415
            from triggers.types import TriggerType  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "[Maintenance] TriggerManager 不可用，"
                "无法注册自动维护触发器"
            )
            return []

        trigger_manager: TriggerManager = _get_trigger_manager_safe()
        if trigger_manager is None:
            return []

        registered: list[str] = []

        # 注册清理巡检触发器（按配置间隔）
        trigger_id = "memory_maintenance_check"
        trigger_manager.register(TriggerConfig(
            trigger_id=trigger_id,
            name="记忆维护巡检（清理）",
            trigger_type=TriggerType.INTERVAL,
            interval_seconds=self._config.cleanup_check_interval,
            action="memory_maintenance.run_cleanup",
            max_fires=0,
            metadata={"maintenance_type": "cleanup"},
        ))
        registered.append(trigger_id)

        logger.info(
            "[Maintenance] 已注册 %d 个维护触发器: %s (间隔=%ds)",
            len(registered),
            registered,
            self._config.cleanup_check_interval,
        )
        return registered

    # ============================================
    # 清理巡检入口（供触发器调用）
    # ============================================

    async def run_cleanup(self) -> dict[str, Any]:
        """执行清理巡检（供定时触发器调用）。

        不再触发复盘（A 路径已删除）。仅按复盘状态/年龄/容量清理数据。

        Returns:
            清理结果字典
        """
        results: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "tasks": {},
        }

        if self.should_trigger_cleanup():
            cleanup_result = await self._get_cleanup_engine().cleanup_by_age_and_capacity(
                review_engine=self._get_review_engine(),
            )
            results["tasks"]["cleanup"] = cleanup_result
            now_cleanup = cleanup_result.get("cleaned_at") or datetime.now(UTC).isoformat()
            self._stats["last_cleanup_at"] = now_cleanup
            self._stats["cleanup_count"] += 1
            self._stats["total_pipelines_cleaned"] += cleanup_result.get("l0_deleted", 0)

        results["completed_at"] = datetime.now(UTC).isoformat()
        results["status"] = "completed"

        logger.info("[Maintenance] 清理巡检完成")
        return results

    def should_trigger_cleanup(self) -> bool:
        """判断是否应该触发清理。

        条件：距上次清理超过 cleanup_check_interval。

        Returns:
            是否应该触发清理
        """
        last_cleanup = self._stats.get("last_cleanup_at")
        if last_cleanup:
            try:
                last_time = datetime.fromisoformat(last_cleanup)
                elapsed = (datetime.now(UTC) - last_time).total_seconds()
                if elapsed >= self._config.cleanup_check_interval:
                    return True
            except (ValueError, TypeError):
                return True
        else:
            # 从未清理过，有数据就触发
            return True
        return False

    # ============================================
    # LLM 复盘编排（B 路径，由 trigger_review 工具触发）
    # ============================================

    async def trigger_llm_review(
        self, parent_pipeline_id: str, limit: int = 5,
    ) -> dict[str, Any]:
        """启动 LLM 复盘管道并返回。不阻塞调用方，复盘在后台运行。

        这是 trigger_review 工具的唯一调用入口。工具只负责获取服务并调用此方法，
        复盘的全生命周期（注册管道→注入消息→等待完成→持久化→通知）在此编排。

        复盘是 B 路径唯一的真相源。若 review_agent 启动失败，直接判失败并通知，
        不再降级到模板提取（A 路径已删除）。

        Args:
            parent_pipeline_id: 调用方父管道 ID（用于回写完成通知）
            limit: 待复盘管道数量上限

        Returns:
            提交结果，含 status（submitted / already_running / skipped_nested）
        """
        # 防自循环：复盘管道内不允许二次触发
        if self._is_review_pipeline(parent_pipeline_id):
            logger.info(
                "[Maintenance] 拒绝复盘管道内的二次触发（防自循环）: parent=%s",
                parent_pipeline_id[:12],
            )
            return {"status": "skipped_nested", "message": "复盘管道内不允许再次触发复盘"}

        if getattr(self, "_review_running", False):
            return {"status": "already_running", "message": "复盘正在执行中，请稍后再试"}

        self._review_running = True

        # 创建后台 task 并持有引用防 GC
        _task = asyncio.create_task(self._run_llm_review_task(parent_pipeline_id, limit))
        _RUNNING_REVIEW_TASKS[id(_task)] = _task
        _task.add_done_callback(lambda t: _RUNNING_REVIEW_TASKS.pop(id(t), None))

        return {"status": "submitted", "message": "复盘任务已提交，完成后会通知您结果。"}

    def _is_review_pipeline(self, pipeline_id: str) -> bool:
        """检查给定 pipeline 是否已是复盘链路上的管道（source=tool_review）。"""
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
            entry = get_engine_registry().get(pipeline_id)
            tags = getattr(entry, "tags", {}) or {} if entry else {}
            return tags.get("source") == "tool_review"
        except Exception:
            return False

    async def _run_llm_review_task(self, parent_pipeline_id: str, limit: int) -> None:
        """LLM 复盘后台任务：编排整个复盘流程。

        流程：收集待复盘 → 启动 review_agent → 等报告 → 持久化 → 通知。
        review_agent 启动失败直接判失败通知，不降级（A 路径已删除）。
        """
        child_pipeline_id = ""
        try:
            # 0. 解析触发来源（父管道的 agent/会话），供复盘管道 tags 溯源
            origin = self._resolve_trigger_origin(parent_pipeline_id)
            self._current_parent_pipeline = parent_pipeline_id
            self._current_trigger_session = origin.get("trigger_session", "")

            # 1. 收集待复盘管道列表
            targets = self._collect_review_targets(parent_pipeline_id, limit)
            logger.info(
                "[Maintenance] 收集待复盘管道 parent=%s targets=%d",
                parent_pipeline_id[:12], len(targets),
            )

            # 2. 启动 review_agent 管道做 LLM 深度复盘
            child_pipeline_id, launched = await self._try_launch_review_agent(targets)
            if not launched:
                # A 路径已删除：启动失败直接判失败并通知父管道
                logger.warning(
                    "[Maintenance] review_agent 启动失败，复盘未执行: parent=%s",
                    parent_pipeline_id[:12],
                )
                await self._notify_parent(
                    parent_pipeline_id, "failed",
                    "复盘启动失败：review_agent 管道未能启动（配置缺失或注册失败）。"
                    "请检查 review_agent 配置后重试。",
                )
                return

            # 3. 等待 review_agent 产出报告
            report_text = await self._await_child_report(child_pipeline_id)
            if report_text:
                # 4. 持久化报告
                await self._persist_review_result(child_pipeline_id, report_text)
                # 5. 标记被复盘的 pending 管道为已复盘
                await self._mark_targets_reviewed(targets)
                # 6. 通知父管道
                brief = report_text[:200].replace("\n", " ").strip()
                await self._notify_parent(
                    parent_pipeline_id, "completed",
                    f"复盘管道 {child_pipeline_id[:12]} 已产出报告（已存入知识库和 docs/working/）。"
                    + (f" 报告摘要：{brief}" if brief else ""),
                )
            else:
                # 有执行但未产出报告：标记为 failed，通知父管道
                await self._mark_targets_reviewed(targets, failed=True)
                await self._notify_parent(
                    parent_pipeline_id, "failed",
                    f"复盘管道 {child_pipeline_id[:12]} 已执行完成，但未提取到报告内容。",
                )

        except Exception as exc:
            logger.error("[Maintenance] 复盘执行失败: %s", exc, exc_info=True)
            await self._notify_parent(
                parent_pipeline_id, "failed",
                f"复盘执行失败: {exc}",
            )
        finally:
            self._review_running = False

    def _collect_review_targets(self, parent_pipeline_id: str, limit: int = 5) -> list[dict[str, Any]]:
        """收集待复盘管道列表。"""
        targets: list[dict[str, Any]] = []
        try:
            review_engine = self._get_review_engine()
            pending = review_engine.get_pending_pipelines()
            for summary in pending:
                item: dict[str, Any] = {
                    "run_id": summary.run_id,
                    "status": getattr(summary, "status", "") or "",
                    "total_records": getattr(summary, "total_records", 0),
                    "total_iterations": getattr(summary, "total_iterations", 0),
                    "error": getattr(summary, "error", "") or "",
                    "agent_id": "",
                    "task_title": "",
                }
                if self._task_lookup is not None:
                    try:
                        info = self._task_lookup(summary.run_id) or {}
                        item["agent_id"] = info.get("agent", "") or ""
                        item["task_title"] = (info.get("title", "") or "")[:80]
                    except Exception:
                        pass
                targets.append(item)
            targets.sort(key=lambda t: (
                0 if t.get("status") == "failed" else 1,
                -(t.get("total_records", 0)),
            ))
            targets = targets[:limit]
        except Exception as exc:
            logger.warning("[Maintenance] 收集待复盘管道失败: %s", exc)
        return targets

    async def _mark_targets_reviewed(
        self, targets: list[dict[str, Any]], *, failed: bool = False,
    ) -> None:
        """把本次复盘覆盖的 pending 管道标记为已复盘。

        Args:
            targets: _collect_review_targets 返回的待复盘列表
            failed: True 时标记为 failed（未产出报告），默认 completed
        """
        review_engine = self._get_review_engine()
        for t in targets:
            run_id = t.get("run_id", "")
            if not run_id:
                continue
            try:
                await review_engine.mark_reviewed(run_id, failed=failed)
            except Exception as exc:
                logger.warning(
                    "[Maintenance] 标记管道已复盘失败 | run_id=%s | err=%s",
                    run_id[:12], exc,
                )
        # 更新统计
        self._stats["last_review_at"] = datetime.now(UTC).isoformat()
        self._stats["review_count"] += 1
        self._stats["total_pipelines_reviewed"] += len(targets)

    def _resolve_trigger_origin(self, parent_pipeline_id: str) -> dict[str, str]:
        """从父管道注册表 tags 反查触发来源（复盘是被谁、在哪个会话拉起的）。

        沿用现有 tags 命名约定：agent_id / session_id / parent_pipeline。
        父管道不在注册表（如 API 手动触发 parent=""）时返回空值，不阻断复盘。

        Returns:
            {"trigger_agent": ..., "trigger_session": ..., "trigger_tool": ...}
        """
        origin: dict[str, str] = {
            "trigger_agent": "",
            "trigger_session": "",
            "trigger_tool": "trigger_review",
        }
        if not parent_pipeline_id:
            return origin
        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
            entry = get_engine_registry().get(parent_pipeline_id)
            tags = getattr(entry, "tags", {}) or {} if entry else {}
            origin["trigger_agent"] = tags.get("agent_id", "") or ""
            origin["trigger_session"] = tags.get("session_id", "") or ""
        except Exception:
            pass
        return origin

    async def _try_launch_review_agent(
        self, targets: list[dict[str, Any]],
    ) -> tuple[str, bool]:
        """注册 review_agent 管道并注入消息，启动 LLM 复盘。

        agent 身份与触发来源经 tags 写入注册表（沿用现有约定），
        引擎首次启动时由 _start_idle_engine 从 tags.agent_id 解析 agent，
        不再 load agent_config 后 emit(agent_config=...)。

        Args:
            targets: 待复盘目标列表
            parent_pipeline_id: 调用方父管道（打 source 溯源用）

        Returns:
            (子 pipeline_id, 是否成功启动)
        """
        try:
            from tools.tool_context import MessageType, PipelineMessage, emit, get_engine_registry  # noqa: PLC0415

            # 前置校验：review_agent 配置必须存在，否则 tags.agent_id 反查会失败
            from agents.global_registry import get_global_agent_registry_sync  # noqa: PLC0415
            if get_global_agent_registry_sync().get(self.REVIEW_AGENT_ID) is None:
                logger.warning("[Maintenance] review_agent 配置不存在")
                return "", False

            from infrastructure.service_provider import get_service_provider  # noqa: PLC0415
            registry = get_engine_registry()
            provider = get_service_provider()

            # 触发来源溯源（沿用现有 tags 命名，不自创字段）：
            # - agent_id：复盘管道自身跑哪个 agent（review_agent）
            # - source：来源标记，已有约定值 tool_review
            # - parent_pipeline：调用方父管道（与 task_executor 命名一致）
            # - session_id：触发会话（API 手动触发时为空）
            tags = {
                "agent_id": self.REVIEW_AGENT_ID,
                "source": "tool_review",
                "parent_pipeline": self._current_parent_pipeline,
                "session_id": self._current_trigger_session,
            }
            entry = registry.register_pipeline(
                tags=tags,
                input_route_table=provider.get("input_route_table"),
                output_route_table=provider.get("output_route_table"),
                plugin_registry=provider.get("plugin_registry"),
                services=provider.get_all_services(),
            )
            if entry is None:
                logger.warning("[Maintenance] 管道注册失败")
                return "", False

            pipeline_id = entry.engine.pipeline_id

            # 构造消息内容
            if targets:
                targets_str = "\n".join(
                    f"- pipeline_run_id={t['run_id']} (status={t.get('status','?')}, "
                    f"records={t.get('total_records','?')}, iters={t.get('total_iterations','?')}, "
                    f"agent={t.get('agent_id','?')}, task={t.get('task_title','?')}"
                    + (f", error={t.get('error','')[:60]}" if t.get('error') else "")
                    + ")"
                    for t in targets
                )
                content = (
                    f"[工具触发复盘] 请分析以下管道的执行记录，产出经验和改进建议。\n\n"
                    f"待复盘管道列表（failed 优先，共 {len(targets)} 个）：\n{targets_str}\n\n"
                    f"分析要求：\n"
                    f"1. 先用 read_execution_detail(level=skeleton, pipeline_run_id=...) 逐个查看骨架；\n"
                    f"2. 【必须】分析每个管道时，先看用户最初下达的指令（type=user 的记录）"
                    f"和后续的人类交互/反馈——脱离用户意图的根因分析没有意义；\n"
                    f"3. 对失败/异常的 iteration 做 5 Whys 根因分析（找根因不找症状）；\n"
                    f"4. 产出结构化复盘报告（JSON：summary + experiences + improvements）。"
                )
            else:
                content = (
                    f"[工具触发复盘] 当前无 pending 的执行记录可供复盘。"
                    f"请用 read_execution_detail 查看最近的管道执行记录并产出分析报告。"
                )

            msg = PipelineMessage(
                type=MessageType.CHAT,
                content=content,
                pipeline_id=pipeline_id,
                metadata={"source": "tool_review"},
            )
            # agent 身份已在 tags.agent_id 注册，引擎启动时自动解析，无需传 agent_config
            result = await emit(msg)
            logger.info("[Maintenance] LLM 复盘管道已提交 (pipeline=%s, success=%s)", pipeline_id, result.success)
            return pipeline_id, bool(result.success)

        except Exception as exc:
            logger.warning("[Maintenance] LLM 复盘管道提交失败: %s", exc)
            return "", False

    async def _await_child_report(self, child_pid: str) -> str:
        """轮询等待子复盘管道产出报告（挂起或 engine done 即视为已产出）。

        Args:
            child_pid: 子复盘管道 ID

        Returns:
            复盘报告完整内容，未产出则返回空字符串
        """
        if not child_pid:
            return ""

        try:
            from pipeline.registry import get_engine_registry  # noqa: PLC0415
            registry = get_engine_registry()
            entry = registry.get(child_pid)
            if entry is None:
                return ""

            engine_task = getattr(entry, "engine_task", None)
            if engine_task is None:
                return ""

            # 轮询：每 15s 检查引擎是否挂起，最多 40 次(600s)
            for _ in range(40):
                await asyncio.sleep(15)
                child_engine = getattr(entry, "engine", None)
                if child_engine is not None and getattr(child_engine, "is_suspended", False):
                    break
                if engine_task.done():
                    break

            # 提取报告内容
            return self._collect_child_report(child_pid)

        except Exception as exc:
            logger.warning("[Maintenance] 等待子复盘管道报告失败: %s", exc)
            return ""

    def _collect_child_report(self, child_pid: str) -> str:
        """从子复盘管道的执行记录提取最后一条 AI 文本（完整报告）。"""
        try:
            storage = self._storage
            if storage is None:
                return ""
            records, _ = storage.list_by_pipeline(child_pid)
            for r in reversed(records):
                if getattr(r, "type", "") == "ai" and getattr(r, "content", ""):
                    return r.content.strip()
            return ""
        except Exception:
            return ""

    async def _persist_review_result(self, child_pipeline_id: str, report_text: str) -> None:
        """将复盘报告持久化到 KnowledgeService + Markdown 文件。"""
        if not report_text:
            return

        # 1. 写入 KnowledgeService
        try:
            if self._knowledge_service is not None:
                await self._knowledge_service.create_knowledge(
                    user_id="system",
                    content=(
                        f"## 复盘报告（pipeline={child_pipeline_id}）\n\n"
                        f"{report_text[:5000]}"
                    ),
                    source_type="review_experience",
                    extra_data={"pipeline_run_id": child_pipeline_id},
                )
                logger.info("[Maintenance] 复盘报告已写入 KnowledgeService: pipeline=%s", child_pipeline_id[:12])
            else:
                logger.info("[Maintenance] knowledge_service 不可用，跳过知识库写入")
        except Exception as exc:
            logger.warning("[Maintenance] 写入 KnowledgeService 失败: %s", exc)

        # 2. 写 review_report_{id}.md 文件
        try:
            import os as _os  # noqa: PLC0415
            from datetime import datetime as _dt  # noqa: PLC0415

            _report_dir = _os.path.join(_os.getcwd(), "docs", "working")
            _os.makedirs(_report_dir, exist_ok=True)
            _path = _os.path.join(_report_dir, f"review_report_{child_pipeline_id}.md")
            with open(_path, "w", encoding="utf-8") as _f:
                _f.write(
                    f"# 复盘报告\n\n"
                    f"- **复盘流水 ID**: {child_pipeline_id}\n"
                    f"- **生成时间**: {_dt.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}\n"
                    f"- **状态**: completed\n\n"
                    f"---\n\n{report_text}\n"
                )
            logger.info("[Maintenance] review_report.md 已写入: %s", _path)
        except Exception as exc:
            logger.warning("[Maintenance] 写报告文件失败: %s", exc)

        # 3. 更新经验统计
        self._stats["total_experiences_saved"] += 1

    async def _notify_parent(
        self, parent_pid: str, status: str, summary: str,
    ) -> None:
        """复盘完成后，向父管道回写完成通知。"""
        if not parent_pid:
            return
        try:
            from pipeline.message_bus import send_pipeline_message  # noqa: PLC0415
            from pipeline.message_types import (  # noqa: PLC0415
                MessageType, PipelineMessage,
            )
            msg = PipelineMessage(
                type=MessageType.CHAT,
                content=f"[复盘完成] {summary}",
                pipeline_id=parent_pid,
                metadata={"source": "tool_review"},
            )
            await send_pipeline_message(msg)
            logger.info(
                "[Maintenance] 已通知父管道复盘结果: parent=%s status=%s",
                parent_pid[:12], status,
            )
        except Exception as exc:
            logger.warning("[Maintenance] 通知父管道失败: %s", exc)

    # ============================================
    # 统计
    # ============================================

    def get_stats(self) -> dict[str, Any]:
        """获取维护操作统计。

        Returns:
            维护统计字典
        """
        return self._stats.copy()


def _get_trigger_manager_safe() -> Any:
    """安全获取 TriggerManager 单例。

    Returns:
        TriggerManager 实例，不可用时返回 None
    """
    try:
        from triggers import get_trigger_manager  # noqa: PLC0415
        return get_trigger_manager()
    except ImportError:
        return None
