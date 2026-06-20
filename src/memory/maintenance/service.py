"""维护主服务 —— 调度、触发器、配置、入口。

暴露接口：
- MaintenanceConfig: 维护配置数据类
- MemoryMaintenanceService: 记忆维护服务（门面类，委托 review_engine / cleanup_engine）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class MaintenanceConfig:
    """复盘驱动的维护配置。

    Attributes:
        enabled: 是否启用自动维护触发器
        review_min_records: 积累多少条新执行记录后触发复盘
        review_max_interval: 最迟多久触发一次复盘（秒）
        skeleton_budget_percent: 骨架占上下文窗口的百分比
        records_per_skeleton_token: 每条执行记录在骨架中约占的 token 数
        max_records_per_review: 单次复盘最大处理记录数
        cleanup_check_interval: 清理巡检间隔（秒）
        cleanup_min_age_days: 至少多少天才考虑清理
        cleanup_capacity_threshold: 容量使用率超过此值时提前清理
        cleanup_early_age_days: 容量紧张时，多少天以上的已复盘数据可清理
    """

    enabled: bool = False
    # 复盘配置
    review_min_records: int = 500
    review_max_interval: int = 604800       # 7 天
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

    负责两个独立的维护周期：
    1. 复盘周期：分析管道执行记录，产出经验/改进建议，存入 Knowledge
    2. 清理周期：根据复盘状态、数据年龄和容量压力，分层清理数据

    Attributes:
        _storage: 执行记录存储（ExecutionRecordStorage）
        _chunk_db: 压缩块服务（ChunkService）
        _knowledge_service: 知识服务（KnowledgeService）
        _pipeline_engine: 管道引擎（PipelineEngine），用于启动复盘管道
        _config: 维护配置
        _stats: 维护操作统计
    """

    def __init__(
        self,
        storage: Any,
        chunk_db: Any,
        knowledge_service: Any,
        pipeline_engine: Any | None = None,
        config: MaintenanceConfig | dict[str, Any] | None = None,
        memory_service: Any = None,
        task_lookup: Any | None = None,
    ) -> None:
        """初始化复盘驱动的记忆维护服务。

        Args:
            storage: 执行记录存储实例（ExecutionRecordStorage）
            chunk_db: 压缩块服务实例（ChunkService）
            knowledge_service: 知识服务实例（KnowledgeService）
            pipeline_engine: 管道引擎实例（PipelineEngine），用于启动复盘管道
            config: 维护配置，支持 MaintenanceConfig 实例、配置字典或 None
            memory_service: 记忆服务门面实例（用于索引重建等操作）
            task_lookup: 可选的任务反查回调，签名 (pipeline_run_id) -> dict | None。
                把 pipeline_run_id 反查到目标 agent 和任务标题，供复盘经验产出带身份。
                由 Application 装配时注入（闭包引用 task_service + root_map），
                不传时经验产出不含 agent 身份。
        """
        self._storage = storage
        self._chunk_db = chunk_db
        self._knowledge_service = knowledge_service
        self._pipeline_engine = pipeline_engine
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
            # 注意：ReviewEngine.__init__ 不接受 config 参数
            self._review_engine = ReviewEngine(
                storage=self._storage,
                chunk_db=self._chunk_db,
                knowledge_service=self._knowledge_service,
                pipeline_engine=self._pipeline_engine,
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
        """向 TriggerManager 注册统一的维护触发器。

        只注册一个定时触发器，周期性检查是否需要复盘或清理。

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

        # 注册统一维护触发器（每 6 小时检查一次）
        check_interval = min(
            self._config.cleanup_check_interval,
            self._config.review_max_interval,
            21600,  # 最长 6 小时检查一次
        )
        trigger_id = "memory_maintenance_check"
        trigger_manager.register(TriggerConfig(
            trigger_id=trigger_id,
            name="记忆维护巡检（复盘+清理）",
            trigger_type=TriggerType.INTERVAL,
            interval_seconds=check_interval,
            action="memory_maintenance.run_maintenance",
            max_fires=0,
            metadata={"maintenance_type": "review_and_cleanup"},
        ))
        registered.append(trigger_id)

        logger.info(
            "[Maintenance] 已注册 %d 个维护触发器: %s (间隔=%ds)",
            len(registered),
            registered,
            check_interval,
        )
        return registered

    # ============================================
    # 触发条件判断
    # ============================================

    def should_trigger_review(self) -> bool:
        """判断是否应该触发复盘。

        满足以下任一条件即触发：
        1. 存在 status=completed 且 review_status=pending 的管道运行
        2. 距上次复盘超过 review_max_interval

        Returns:
            是否应该触发复盘
        """
        # 条件1：存在 pending 的管道运行
        try:
            pending = self._get_review_engine().get_pending_pipelines()
            if pending:
                logger.info(
                    "[Maintenance] 触发复盘：发现 %d 个待复盘管道运行",
                    len(pending),
                )
                return True
        except Exception as exc:
            logger.warning("[Maintenance] 检查 pending pipelines 失败: %s", exc)

        # 条件2：距上次复盘超过最大间隔
        last_review = self._stats.get("last_review_at")
        if last_review:
            try:
                last_time = datetime.fromisoformat(last_review)
                elapsed = (datetime.now(UTC) - last_time).total_seconds()
                if elapsed >= self._config.review_max_interval:
                    logger.info(
                        "[Maintenance] 触发复盘：距上次复盘 %.0f 秒 >= %d 秒",
                        elapsed, self._config.review_max_interval,
                    )
                    return True
            except (ValueError, TypeError):
                pass

        return False

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
    # 统一维护入口
    # ============================================

    async def run_maintenance(self) -> dict[str, Any]:
        """执行维护（复盘和清理独立运行）。

        Returns:
            维护结果字典
        """
        results: dict[str, Any] = {
            "started_at": datetime.now(UTC).isoformat(),
            "status": "running",
            "tasks": {},
        }

        # 独立判断：是否需要复盘
        if self.should_trigger_review():
            review_result = await self.trigger_review_now()
            results["tasks"]["review"] = review_result
            # 同步统计
            now_review = review_result.get("reviewed_at") or datetime.now(UTC).isoformat()
            self._stats["last_review_at"] = now_review
            self._stats["review_count"] += 1
            self._stats["total_pipelines_reviewed"] += review_result.get("pipelines_reviewed", 0)
            self._stats["total_experiences_saved"] += review_result.get("experiences_saved", 0)

        # 独立判断：是否需要清理
        if self.should_trigger_cleanup():
            cleanup_result = await self._get_cleanup_engine().cleanup_by_age_and_capacity(
                review_engine=self._get_review_engine(),
            )
            results["tasks"]["cleanup"] = cleanup_result
            # 同步统计
            now_cleanup = cleanup_result.get("cleaned_at") or datetime.now(UTC).isoformat()
            self._stats["last_cleanup_at"] = now_cleanup
            self._stats["cleanup_count"] += 1
            self._stats["total_pipelines_cleaned"] += cleanup_result.get("l0_deleted", 0)

        results["completed_at"] = datetime.now(UTC).isoformat()
        results["status"] = "completed"

        logger.info("[Maintenance] 维护巡检完成")
        return results

    async def trigger_review_now(self) -> dict[str, Any]:
        """立即触发复盘，处理所有 pending 的管道运行。

        直接调用 ReviewEngine.run_review 处理 ExecutionRecordStorage 中
        status=completed && review_status=pending 的管道，
        把错误经验写入 KnowledgeService（source_type=review_experience）。

        Returns:
            复盘结果汇总
        """
        review_engine = self._get_review_engine()
        pending = review_engine.get_pending_pipelines()

        started_at = datetime.now(UTC).isoformat()
        result: dict[str, Any] = {
            "started_at": started_at,
            "pending_count": len(pending),
            "pipelines_reviewed": 0,
            "experiences_saved": 0,
            "details": [],
        }

        for summary in pending:
            try:
                pr = await review_engine.run_review(summary.run_id)
                ok = pr.get("status") == "success"
                exp_count = pr.get("experience_count", 0) if ok else 0
                result["details"].append({
                    "run_id": summary.run_id,
                    "status": pr.get("status"),
                    "experiences": exp_count,
                    "records_analyzed": pr.get("records_analyzed", 0),
                })
                if ok:
                    result["pipelines_reviewed"] += 1
                    result["experiences_saved"] += exp_count
            except Exception as exc:
                logger.warning(
                    "[Maintenance] 复盘单管道失败 | run_id=%s | err=%s",
                    summary.run_id, exc,
                )
                result["details"].append({
                    "run_id": summary.run_id,
                    "status": "error",
                    "error": str(exc),
                })

        result["completed_at"] = datetime.now(UTC).isoformat()
        logger.info(
            "[Maintenance] 复盘完成 | reviewed=%d | experiences=%d",
            result["pipelines_reviewed"], result["experiences_saved"],
        )
        return result

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
