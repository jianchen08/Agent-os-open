"""进化日志模块。

记录 Agent 自进化过程的每一步操作，提供完整的审计追踪。
日志存储在 JSON 文件中，路径为 data/evolution_logs/。

暴露接口：
- log_record(record) -> None
- query_records(filters) -> list[EvolutionRecord]
- get_record(record_id) -> EvolutionRecord | None
- export_log() -> str
- EvolutionLog: 进化日志管理器类
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evolution.types import EvolutionRecord

logger = logging.getLogger(__name__)

# 默认日志目录
DEFAULT_LOG_DIR = "data/evolution_logs"


class EvolutionLog:
    """进化日志管理器。

    管理进化操作的审计日志，支持：
    - 记录进化操作
    - 按条件查询历史记录
    - 导出完整日志
    - JSON 文件持久化

    Attributes:
        _log_dir: 日志文件目录
        _records: 内存中的记录缓存 {record_id: EvolutionRecord}
    """

    def __init__(self, log_dir: str | None = None) -> None:
        """初始化进化日志管理器。

        Args:
            log_dir: 日志目录路径，默认为 data/evolution_logs/
        """
        self._log_dir = Path(log_dir or DEFAULT_LOG_DIR)
        self._records: dict[str, EvolutionRecord] = {}
        self._ensure_log_dir()

    def log_record(self, record: EvolutionRecord) -> None:
        """记录进化操作。

        将记录保存到内存缓存和持久化存储。

        Args:
            record: 进化日志记录
        """
        # 保存到内存
        self._records[record.record_id] = record

        # 持久化到文件
        self._persist_record(record)

        logger.info(
            "[EvolutionLog] 记录进化操作: id='%s', status=%s, gap='%s'",
            record.record_id,
            record.status.value,
            record.capability_gap,
        )

    def query_records(self, filters: dict[str, Any] | None = None) -> list[EvolutionRecord]:
        """查询历史记录。

        支持按以下字段过滤：
        - status: 按状态过滤
        - capability_gap: 按能力缺口关键词过滤
        - start_time / end_time: 按时间范围过滤

        Args:
            filters: 过滤条件字典

        Returns:
            匹配的进化记录列表（按时间倒序）
        """
        filters = filters or {}
        results: list[EvolutionRecord] = []

        # 加载所有记录（包括持久化的）
        all_records = self._load_all_records()

        for record in all_records.values():
            if self._match_filters(record, filters):
                results.append(record)

        # 按时间倒序排列
        results.sort(key=lambda r: r.timestamp, reverse=True)
        return results

    def get_record(self, record_id: str) -> EvolutionRecord | None:
        """获取单条记录。

        先查内存缓存，再查持久化存储。

        Args:
            record_id: 记录 ID

        Returns:
            进化记录，不存在返回 None
        """
        # 先查内存
        if record_id in self._records:
            return self._records[record_id]

        # 再查持久化
        record = self._load_record(record_id)
        if record is not None:
            self._records[record_id] = record
        return record

    def export_log(self) -> str:
        """导出完整日志为 JSON 字符串。

        Returns:
            JSON 格式的完整日志
        """
        all_records = self._load_all_records()
        records_list = [
            self._record_to_dict(r) for r in all_records.values()
        ]
        return json.dumps(records_list, indent=2, ensure_ascii=False, default=str)

    def create_record_id(self) -> str:
        """生成唯一的记录 ID。

        Returns:
            格式为 evo_{timestamp}_{random_suffix} 的唯一 ID
        """
        import uuid
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        suffix = str(uuid.uuid4())[:8]
        return f"evo_{timestamp}_{suffix}"

    # -- 内部方法 --------------------------------------------------------

    def _ensure_log_dir(self) -> None:
        """确保日志目录存在。"""
        self._log_dir.mkdir(parents=True, exist_ok=True)

    def _persist_record(self, record: EvolutionRecord) -> None:
        """将记录持久化到 JSON 文件。

        每条记录一个文件，文件名为 {record_id}.json。

        Args:
            record: 进化记录
        """
        try:
            file_path = self._log_dir / f"{record.record_id}.json"
            data = self._record_to_dict(record)
            file_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.error(
                "[EvolutionLog] 持久化记录失败: id='%s', error=%s",
                record.record_id,
                exc,
            )

    def _load_record(self, record_id: str) -> EvolutionRecord | None:
        """从文件加载单条记录。

        Args:
            record_id: 记录 ID

        Returns:
            进化记录，不存在返回 None
        """
        try:
            file_path = self._log_dir / f"{record_id}.json"
            if not file_path.exists():
                return None

            data = json.loads(file_path.read_text(encoding="utf-8"))
            return self._dict_to_record(data)

        except Exception as exc:
            logger.warning(
                "[EvolutionLog] 加载记录失败: id='%s', error=%s",
                record_id,
                exc,
            )
            return None

    def _load_all_records(self) -> dict[str, EvolutionRecord]:
        """加载所有记录。

        合并内存缓存和持久化存储中的记录。

        Returns:
            {record_id: EvolutionRecord} 映射
        """
        all_records = dict(self._records)

        try:
            for file_path in self._log_dir.glob("*.json"):
                record_id = file_path.stem
                if record_id not in all_records:
                    record = self._load_record(record_id)
                    if record is not None:
                        all_records[record_id] = record
        except Exception as exc:
            logger.warning("[EvolutionLog] 加载记录失败: %s", exc)

        return all_records

    @staticmethod
    def _match_filters(
        record: EvolutionRecord,
        filters: dict[str, Any],
    ) -> bool:
        """检查记录是否匹配过滤条件。

        Args:
            record: 进化记录
            filters: 过滤条件

        Returns:
            是否匹配
        """
        # 按状态过滤
        if "status" in filters:
            if record.status.value != filters["status"]:
                return False

        # 按能力缺口关键词过滤
        if "capability_gap" in filters:
            keyword = filters["capability_gap"].lower()
            if keyword not in record.capability_gap.lower():
                return False

        # 按时间范围过滤
        if "start_time" in filters:
            if record.timestamp < filters["start_time"]:
                return False

        if "end_time" in filters:
            if record.timestamp > filters["end_time"]:
                return False

        return True

    @staticmethod
    def _record_to_dict(record: EvolutionRecord) -> dict[str, Any]:
        """将 EvolutionRecord 转换为可序列化的字典。

        Args:
            record: 进化记录

        Returns:
            可序列化的字典
        """
        data: dict[str, Any] = {
            "record_id": record.record_id,
            "timestamp": record.timestamp,
            "capability_gap": record.capability_gap,
            "status": record.status.value,
            "error_message": record.error_message,
            "rollback_point": record.rollback_point,
        }

        if record.filter_result is not None:
            fr = record.filter_result
            data["filter_result"] = {
                "recommended_layer": fr.recommended_layer.value,
                "recommended_action": fr.recommended_action,
                "tool_layer_result": fr.tool_layer_result,
                "config_layer_result": fr.config_layer_result,
                "plugin_layer_result": fr.plugin_layer_result,
                "core_layer_result": fr.core_layer_result,
            }

        if record.generated_artifact is not None:
            ga = record.generated_artifact
            # 设计决策：序列化时排除 code 字段，避免将大量代码文本写入日志文件。
            # code 字段可通过 generated_artifact.file_path 从文件系统获取。
            data["generated_artifact"] = {
                "generation_type": ga.generation_type.value,
                "file_path": ga.file_path,
                "contract_valid": ga.contract_valid,
                "contract_errors": ga.contract_errors,
            }

        if record.security_report is not None:
            sr = record.security_report
            data["security_report"] = {
                "passed": sr.passed,
                "overall_risk": sr.overall_risk,
                "static_analysis_issues": sr.static_analysis_issues,
                "permission_issues": sr.permission_issues,
                "resource_violations": sr.resource_violations,
            }

        return data

    @staticmethod
    def _dict_to_record(data: dict[str, Any]) -> EvolutionRecord:
        """将字典转换为 EvolutionRecord。

        Args:
            data: 序列化的字典

        Returns:
            进化记录
        """
        from evolution.types import (
            CapabilityGap,
            EvolutionStatus,
            FilterLayer,
            FilterResult,
            GeneratedArtifact,
            GenerationType,
            SecurityReport,
        )

        record = EvolutionRecord(
            record_id=data["record_id"],
            timestamp=data["timestamp"],
            capability_gap=data["capability_gap"],
            status=EvolutionStatus(data.get("status", "idle")),
            error_message=data.get("error_message", ""),
            rollback_point=data.get("rollback_point"),
        )

        # 还原 filter_result
        if "filter_result" in data and data["filter_result"]:
            fr_data = data["filter_result"]
            record.filter_result = FilterResult(
                gap=CapabilityGap(
                    missing_capability=record.capability_gap,
                    required_by="restored",
                ),
                recommended_layer=FilterLayer(
                    fr_data.get("recommended_layer", "plugin")
                ),
                recommended_action=fr_data.get("recommended_action", ""),
                tool_layer_result=fr_data.get("tool_layer_result"),
                config_layer_result=fr_data.get("config_layer_result"),
                plugin_layer_result=fr_data.get("plugin_layer_result"),
                core_layer_result=fr_data.get("core_layer_result"),
            )

        # 还原 generated_artifact
        if "generated_artifact" in data and data["generated_artifact"]:
            ga_data = data["generated_artifact"]
            record.generated_artifact = GeneratedArtifact(
                generation_type=GenerationType(
                    ga_data.get("generation_type", "builtin_tool")
                ),
                code="",
                file_path=ga_data.get("file_path", ""),
                contract_valid=ga_data.get("contract_valid", False),
                contract_errors=ga_data.get("contract_errors", []),
            )

        # 还原 security_report
        if "security_report" in data and data["security_report"]:
            sr_data = data["security_report"]
            record.security_report = SecurityReport(
                passed=sr_data.get("passed", False),
                overall_risk=sr_data.get("overall_risk", "unknown"),
                static_analysis_issues=sr_data.get("static_analysis_issues", []),
                permission_issues=sr_data.get("permission_issues", []),
                resource_violations=sr_data.get("resource_violations", []),
            )

        return record
