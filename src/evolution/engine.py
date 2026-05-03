"""进化引擎模块。

编排 Agent 自进化的完整闭环流程：
分析缺口 → 四层筛选 → 代码生成 → 契约校验 → 安全审查 → 热加载 → 日志记录
任何步骤失败时自动回滚。

暴露接口：
- evolve(required_capability, context) -> EvolutionResult
- get_status() -> EvolutionStatus
- get_history() -> list[EvolutionRecord]
- EvolutionEngine: 进化引擎类
- create_evolution_engine() -> EvolutionEngine
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from evolution.code_generator import CodeGenerator
from evolution.evolution_log import EvolutionLog
from evolution.gap_analyzer import GapAnalyzer
from evolution.hot_loader import HotLoader
from evolution.rollback_manager import RollbackManager
from evolution.security_reviewer import SecurityReviewer
from evolution.types import (
    CapabilityGap,
    EvolutionRecord,
    EvolutionResult,
    EvolutionStatus,
    FilterLayer,
    FilterResult,
    GeneratedArtifact,
    GenerationType,
    SecurityReport,
)

logger = logging.getLogger(__name__)


class EvolutionEngine:
    """进化引擎。

    编排 Agent 自进化的完整闭环流程，任何步骤失败时自动回滚。

    闭环流程：
    1. 创建 EvolutionRecord
    2. GapAnalyzer.analyze_gap → 识别缺口
    3. GapAnalyzer.four_layer_filter → 四层筛选
    4. CodeGenerator.generate_* → 生成代码
    5. CodeGenerator.validate_contract → 契约校验
    6. SecurityReviewer.review → 安全审查
    7. HotLoader.load_plugin → 热加载
    8. 更新 EvolutionRecord
    9. 失败时 RollbackManager.rollback → 自动回滚

    Attributes:
        _gap_analyzer: 能力缺口分析器
        _code_generator: 代码生成器
        _security_reviewer: 安全审查器
        _hot_loader: 热加载器
        _evolution_log: 进化日志
        _rollback_manager: 回滚管理器
        _status: 当前引擎状态
    """

    def __init__(
        self,
        tool_registry: Any | None = None,
        plugin_registry: Any | None = None,
        config_store: Any | None = None,
        *,
        log_dir: str | None = None,
        storage_dir: str | None = None,
        allowed_imports: set[str] | None = None,
        allowed_permissions: set[str] | None = None,
        base_path: str = ".",
    ) -> None:
        """初始化进化引擎。

        Args:
            tool_registry: 工具注册中心实例
            plugin_registry: 插件注册中心实例
            config_store: 配置存储实例
            log_dir: 进化日志目录
            storage_dir: 回滚检查点存储目录
            allowed_imports: 允许的导入白名单
            allowed_permissions: 允许的权限列表
            base_path: 热加载的基础路径
        """
        self._status = EvolutionStatus.IDLE
        self._lock = threading.Lock()

        # 初始化子模块
        self._gap_analyzer = GapAnalyzer(
            tool_registry=tool_registry,
            config_store=config_store,
            plugin_registry=plugin_registry,
        )

        self._code_generator = CodeGenerator()
        self._security_reviewer = SecurityReviewer(
            allowed_imports=allowed_imports,
            allowed_permissions=allowed_permissions,
        )
        self._hot_loader = HotLoader(
            tool_registry=tool_registry,
            base_path=base_path,
        )
        self._evolution_log = EvolutionLog(log_dir=log_dir)
        self._rollback_manager = RollbackManager(
            hot_loader=self._hot_loader,
            storage_dir=storage_dir,
        )

        # 保存引用
        self._tool_registry = tool_registry
        self._plugin_registry = plugin_registry
        self._config_store = config_store

    def evolve(
        self,
        required_capability: str,
        context: dict[str, Any] | None = None,
    ) -> EvolutionResult:
        """执行完整的进化闭环。

        流程：
        1. 创建 EvolutionRecord
        2. 分析缺口 → 四层筛选
        3. 生成代码 → 契约校验
        4. 安全审查
        5. 热加载
        6. 失败时自动回滚

        Args:
            required_capability: 需要的能力描述
            context: 附加上下文

        Returns:
            进化结果
        """
        context = context or {}

        # 状态守卫 + 并发保护
        with self._lock:
            if self._status not in (EvolutionStatus.IDLE, EvolutionStatus.COMPLETED, EvolutionStatus.FAILED):
                raise RuntimeError(f"引擎正在执行中，当前状态: {self._status}")

        # Step 1: 创建 EvolutionRecord
        record_id = self._evolution_log.create_record_id()
        timestamp = datetime.now(timezone.utc).isoformat()
        record = EvolutionRecord(
            record_id=record_id,
            timestamp=timestamp,
            capability_gap=required_capability,
            status=EvolutionStatus.ANALYZING,
        )
        self._evolution_log.log_record(record)
        self._status = EvolutionStatus.ANALYZING

        try:
            # Step 2: 分析缺口
            gap = self._gap_analyzer.analyze_gap(
                required_capability, context
            )
            # Step 3: 四层筛选
            filter_result = self._gap_analyzer.four_layer_filter(
                gap,
                tool_registry=self._tool_registry,
                config_store=self._config_store,
                plugin_registry=self._plugin_registry,
            )
            record.filter_result = filter_result

            # 如果在 TOOL 层找到匹配，直接返回成功
            if filter_result.recommended_layer == FilterLayer.TOOL:
                record.status = EvolutionStatus.COMPLETED
                self._evolution_log.log_record(record)
                self._status = EvolutionStatus.COMPLETED
                return EvolutionResult(
                    success=True,
                    record=record,
                    message=f"已有工具可满足需求: {filter_result.recommended_action}",
                )

            # 如果在 CONFIG 层找到方案，直接返回成功
            if filter_result.recommended_layer == FilterLayer.CONFIG:
                record.status = EvolutionStatus.COMPLETED
                self._evolution_log.log_record(record)
                self._status = EvolutionStatus.COMPLETED
                return EvolutionResult(
                    success=True,
                    record=record,
                    message=f"通过配置变更满足需求: {filter_result.recommended_action}",
                )

            # Step 4: 创建回滚检查点
            checkpoint_id = self._rollback_manager.create_checkpoint(
                description=f"进化前检查点: {required_capability}",
                hot_loader=self._hot_loader,
            )
            record.rollback_point = checkpoint_id

            # Step 5: 生成代码
            self._status = EvolutionStatus.GENERATING
            record.status = EvolutionStatus.GENERATING
            self._evolution_log.log_record(record)

            artifact = self._generate_code(gap, filter_result, context)

            # Step 6: 契约校验
            artifact = self._code_generator.validate_contract(artifact)
            record.generated_artifact = artifact

            if not artifact.contract_valid:
                record.status = EvolutionStatus.FAILED
                record.error_message = (
                    f"契约校验失败: {'; '.join(artifact.contract_errors)}"
                )
                self._evolution_log.log_record(record)
                self._rollback(record, checkpoint_id)
                return EvolutionResult(
                    success=False,
                    record=record,
                    message=record.error_message,
                )

            # Step 7: 安全审查
            self._status = EvolutionStatus.REVIEWING
            record.status = EvolutionStatus.REVIEWING
            self._evolution_log.log_record(record)

            security_report = self._security_reviewer.review(artifact)
            record.security_report = security_report

            if not security_report.passed:
                record.status = EvolutionStatus.FAILED
                record.error_message = (
                    f"安全审查未通过: risk={security_report.overall_risk}"
                )
                self._evolution_log.log_record(record)
                self._rollback(record, checkpoint_id)
                return EvolutionResult(
                    success=False,
                    record=record,
                    message=record.error_message,
                )

            # Step 8: 热加载
            self._status = EvolutionStatus.LOADING
            record.status = EvolutionStatus.LOADING
            self._evolution_log.log_record(record)

            load_success = self._hot_loader.load_plugin(artifact)

            if not load_success:
                record.status = EvolutionStatus.FAILED
                record.error_message = "热加载失败"
                self._evolution_log.log_record(record)
                self._rollback(record, checkpoint_id)
                return EvolutionResult(
                    success=False,
                    record=record,
                    message=record.error_message,
                )

            # Step 9: 完成
            plugin_name = self._extract_plugin_name(artifact)
            record.status = EvolutionStatus.COMPLETED
            self._evolution_log.log_record(record)
            self._status = EvolutionStatus.COMPLETED

            logger.info(
                "[EvolutionEngine] 进化成功: capability='%s', plugin='%s'",
                required_capability,
                plugin_name,
            )

            return EvolutionResult(
                success=True,
                record=record,
                loaded_plugin_name=plugin_name,
                message=f"能力 '{required_capability}' 进化成功，已加载插件 '{plugin_name}'",
            )

        except Exception as exc:
            logger.error(
                "[EvolutionEngine] 进化异常: capability='%s', error=%s",
                required_capability,
                exc,
            )
            record.status = EvolutionStatus.FAILED
            record.error_message = f"进化过程异常: {exc}"
            self._evolution_log.log_record(record)
            self._rollback(record, record.rollback_point)
            return EvolutionResult(
                success=False,
                record=record,
                message=record.error_message,
            )

    def get_status(self) -> EvolutionStatus:
        """获取当前引擎状态。

        Returns:
            当前进化状态
        """
        return self._status

    def get_history(self) -> list[EvolutionRecord]:
        """获取进化历史记录。

        Returns:
            进化记录列表（按时间倒序）
        """
        return self._evolution_log.query_records()

    def _generate_code(
        self,
        gap: CapabilityGap,
        filter_result: FilterResult,
        context: dict[str, Any],
    ) -> GeneratedArtifact:
        """根据筛选结果生成代码。

        Args:
            gap: 能力缺口
            filter_result: 筛选结果
            context: 上下文

        Returns:
            生成的代码产物
        """
        # 从上下文或默认值确定生成参数
        name = context.get(
            "tool_name",
            gap.missing_capability.lower().replace(" ", "_")[:30],
        )
        description = context.get(
            "description",
            gap.missing_capability,
        )
        parameters = context.get("parameters", {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "输入参数",
                },
            },
            "required": ["input"],
        })
        implementation_hint = context.get("implementation_hint", "")

        # 确定生成类型
        gen_type_str = context.get("generation_type", "builtin_tool")
        if gen_type_str == "mcp_server":
            tools = context.get("tools", [])
            return self._code_generator.generate_mcp_server(
                name=name,
                tools=tools,
                description=description,
            )

        return self._code_generator.generate_builtin_tool(
            name=name,
            description=description,
            parameters=parameters,
            implementation_hint=implementation_hint,
        )

    def _rollback(
        self,
        record: EvolutionRecord,
        checkpoint_id: str | None,
    ) -> None:
        """执行回滚。

        Args:
            record: 进化记录
            checkpoint_id: 回滚目标检查点 ID
        """
        if checkpoint_id is None:
            logger.warning("[EvolutionEngine] 无回滚点，跳过回滚")
            return

        self._status = EvolutionStatus.ROLLING_BACK
        record.status = EvolutionStatus.ROLLING_BACK
        self._evolution_log.log_record(record)

        success = self._rollback_manager.rollback(
            checkpoint_id,
            hot_loader=self._hot_loader,
        )

        if success:
            logger.info("[EvolutionEngine] 回滚成功")
        else:
            logger.warning("[EvolutionEngine] 回滚失败")

        # 无论回滚成功与否，最终状态都是 FAILED
        self._status = EvolutionStatus.FAILED
        record.status = EvolutionStatus.FAILED
        self._evolution_log.log_record(record)

        # 回滚完成后，无论成功与否，最终状态都应为 FAILED
        self._status = EvolutionStatus.FAILED
        record.status = EvolutionStatus.FAILED
        self._evolution_log.log_record(record)

    @staticmethod
    def _extract_plugin_name(artifact: GeneratedArtifact) -> str:
        """从产物中提取插件名称。

        Args:
            artifact: 代码产物

        Returns:
            插件名称
        """
        from pathlib import Path
        return Path(artifact.file_path).stem


def create_evolution_engine(
    tool_registry: Any | None = None,
    plugin_registry: Any | None = None,
    config_store: Any | None = None,
    **kwargs: Any,
) -> EvolutionEngine:
    """创建进化引擎实例（工厂函数）。

    Args:
        tool_registry: 工具注册中心实例
        plugin_registry: 插件注册中心实例
        config_store: 配置存储实例
        **kwargs: 传递给 EvolutionEngine 的额外参数

    Returns:
        配置好的进化引擎实例
    """
    return EvolutionEngine(
        tool_registry=tool_registry,
        plugin_registry=plugin_registry,
        config_store=config_store,
        **kwargs,
    )
