"""进化日志模块测试。

覆盖 EvolutionLog 的核心功能：
- log_record / get_record: 记录和查询日志
- query_records: 按条件过滤查询
- export_log: 导出日志
- 持久化和反序列化
- MF-09 修复验证：模块不导入 os
"""

from __future__ import annotations

import inspect
import json
import os

import pytest

from evolution.evolution_log import EvolutionLog
from evolution.types import (
    CapabilityGap,
    EvolutionRecord,
    EvolutionStatus,
    FilterLayer,
    FilterResult,
    GeneratedArtifact,
    GenerationType,
    SecurityReport,
)


# =========================================================================
# Fixtures
# =========================================================================


@pytest.fixture
def evo_log(tmp_path) -> EvolutionLog:
    """使用临时目录的进化日志实例。"""
    return EvolutionLog(log_dir=str(tmp_path / "evo_logs"))


@pytest.fixture
def sample_record() -> EvolutionRecord:
    """示例进化记录。"""
    return EvolutionRecord(
        record_id="test_001",
        timestamp="2024-01-01T00:00:00",
        capability_gap="test gap",
        status=EvolutionStatus.COMPLETED,
    )


@pytest.fixture
def full_record() -> EvolutionRecord:
    """包含所有字段的完整记录。"""
    gap = CapabilityGap(missing_capability="test", required_by="agent")
    filter_result = FilterResult(
        gap=gap,
        recommended_layer=FilterLayer.PLUGIN,
        recommended_action="generate",
    )
    artifact = GeneratedArtifact(
        generation_type=GenerationType.BUILTIN_TOOL,
        code="test code",
        file_path="test.py",
        contract_valid=True,
    )
    security_report = SecurityReport(
        passed=True,
        overall_risk="low",
    )
    return EvolutionRecord(
        record_id="full_record",
        timestamp="2024-01-01T00:00:00",
        capability_gap="full test",
        filter_result=filter_result,
        generated_artifact=artifact,
        security_report=security_report,
        status=EvolutionStatus.COMPLETED,
    )


# =========================================================================
# log_record / get_record 测试
# =========================================================================


class TestLogAndGetRecord:
    """记录和查询日志测试。"""

    def test_log_and_query_record(
        self, evo_log: EvolutionLog, sample_record: EvolutionRecord,
    ) -> None:
        """记录和查询日志。"""
        evo_log.log_record(sample_record)

        retrieved = evo_log.get_record("test_001")
        assert retrieved is not None
        assert retrieved.record_id == "test_001"
        assert retrieved.capability_gap == "test gap"
        assert retrieved.status == EvolutionStatus.COMPLETED

    def test_get_nonexistent_record(self, evo_log: EvolutionLog) -> None:
        """获取不存在的记录返回 None。"""
        result = evo_log.get_record("nonexistent")
        assert result is None

    def test_log_multiple_records(self, evo_log: EvolutionLog) -> None:
        """记录多条日志。"""
        for i in range(5):
            record = EvolutionRecord(
                record_id=f"rec_{i:03d}",
                timestamp=f"2024-01-0{i+1}T00:00:00",
                capability_gap=f"gap_{i}",
                status=EvolutionStatus.COMPLETED,
            )
            evo_log.log_record(record)

        for i in range(5):
            retrieved = evo_log.get_record(f"rec_{i:03d}")
            assert retrieved is not None

    def test_log_overwrites_existing(
        self, evo_log: EvolutionLog,
    ) -> None:
        """相同 ID 的记录会覆盖。"""
        record1 = EvolutionRecord(
            record_id="overwrite_test",
            timestamp="2024-01-01T00:00:00",
            capability_gap="original",
            status=EvolutionStatus.COMPLETED,
        )
        record2 = EvolutionRecord(
            record_id="overwrite_test",
            timestamp="2024-01-01T00:00:00",
            capability_gap="updated",
            status=EvolutionStatus.FAILED,
        )

        evo_log.log_record(record1)
        evo_log.log_record(record2)

        retrieved = evo_log.get_record("overwrite_test")
        assert retrieved.capability_gap == "updated"
        assert retrieved.status == EvolutionStatus.FAILED


# =========================================================================
# query_records 测试
# =========================================================================


class TestQueryRecords:
    """条件查询测试。"""

    def test_query_by_status(self, evo_log: EvolutionLog) -> None:
        """按状态过滤。"""
        for i, status in enumerate(
            [EvolutionStatus.COMPLETED, EvolutionStatus.FAILED, EvolutionStatus.COMPLETED]
        ):
            record = EvolutionRecord(
                record_id=f"test_{i:03d}",
                timestamp=f"2024-01-0{i+1}T00:00:00",
                capability_gap=f"gap_{i}",
                status=status,
            )
            evo_log.log_record(record)

        results = evo_log.query_records({"status": "completed"})
        assert len(results) == 2

    def test_query_by_capability_gap(self, evo_log: EvolutionLog) -> None:
        """按能力缺口关键词过滤。"""
        record = EvolutionRecord(
            record_id="test_search",
            timestamp="2024-01-01T00:00:00",
            capability_gap="file search capability",
            status=EvolutionStatus.COMPLETED,
        )
        evo_log.log_record(record)

        results = evo_log.query_records({"capability_gap": "search"})
        assert len(results) == 1

        results = evo_log.query_records({"capability_gap": "network"})
        assert len(results) == 0

    def test_query_no_filters(self, evo_log: EvolutionLog) -> None:
        """无过滤条件返回所有记录。"""
        for i in range(3):
            record = EvolutionRecord(
                record_id=f"all_{i}",
                timestamp=f"2024-01-0{i+1}T00:00:00",
                capability_gap=f"gap_{i}",
                status=EvolutionStatus.COMPLETED,
            )
            evo_log.log_record(record)

        results = evo_log.query_records()
        assert len(results) == 3

    def test_query_results_sorted_by_time_desc(self, evo_log: EvolutionLog) -> None:
        """查询结果按时间倒序排列。"""
        for i in range(3):
            record = EvolutionRecord(
                record_id=f"time_{i}",
                timestamp=f"2024-01-0{i+1}T00:00:00",
                capability_gap=f"gap_{i}",
                status=EvolutionStatus.COMPLETED,
            )
            evo_log.log_record(record)

        results = evo_log.query_records()
        timestamps = [r.timestamp for r in results]
        assert timestamps == sorted(timestamps, reverse=True)


# =========================================================================
# export_log 测试
# =========================================================================


class TestExportLog:
    """导出日志测试。"""

    def test_export_log(self, evo_log: EvolutionLog) -> None:
        """导出日志为有效 JSON。"""
        record = EvolutionRecord(
            record_id="test_export",
            timestamp="2024-01-01T00:00:00",
            capability_gap="test gap",
            status=EvolutionStatus.COMPLETED,
        )
        evo_log.log_record(record)

        exported = evo_log.export_log()
        data = json.loads(exported)
        assert isinstance(data, list)
        assert len(data) >= 1
        assert data[0]["record_id"] == "test_export"

    def test_export_empty_log(self, evo_log: EvolutionLog) -> None:
        """空日志导出为空列表。"""
        exported = evo_log.export_log()
        data = json.loads(exported)
        assert data == []


# =========================================================================
# 序列化/反序列化测试
# =========================================================================


class TestSerializationRoundtrip:
    """序列化反序列化正确性测试。"""

    def test_serialization_roundtrip(
        self, evo_log: EvolutionLog, full_record: EvolutionRecord,
    ) -> None:
        """完整记录的序列化反序列化正确。"""
        evo_log.log_record(full_record)

        retrieved = evo_log.get_record("full_record")
        assert retrieved is not None
        assert retrieved.filter_result is not None
        assert retrieved.filter_result.recommended_layer == FilterLayer.PLUGIN
        assert retrieved.generated_artifact is not None
        assert retrieved.generated_artifact.contract_valid is True
        assert retrieved.security_report is not None
        assert retrieved.security_report.passed is True

    def test_persistence_across_instances(self, tmp_path) -> None:
        """持久化后新实例能读取记录。"""
        log_dir = str(tmp_path / "persist_logs")

        # 写入
        log1 = EvolutionLog(log_dir=log_dir)
        record = EvolutionRecord(
            record_id="persist_test",
            timestamp="2024-01-01T00:00:00",
            capability_gap="persistence test",
            status=EvolutionStatus.COMPLETED,
        )
        log1.log_record(record)

        # 新实例读取
        log2 = EvolutionLog(log_dir=log_dir)
        retrieved = log2.get_record("persist_test")

        assert retrieved is not None
        assert retrieved.capability_gap == "persistence test"
        assert retrieved.status == EvolutionStatus.COMPLETED

    def test_record_to_dict_structure(
        self, evo_log: EvolutionLog, full_record: EvolutionRecord,
    ) -> None:
        """序列化字典结构正确。"""
        data = evo_log._record_to_dict(full_record)

        assert data["record_id"] == "full_record"
        assert data["timestamp"] == "2024-01-01T00:00:00"
        assert data["status"] == "completed"
        assert "filter_result" in data
        assert data["filter_result"]["recommended_layer"] == "plugin"
        assert "generated_artifact" in data
        assert data["generated_artifact"]["contract_valid"] is True
        assert "security_report" in data
        assert data["security_report"]["passed"] is True

    def test_dict_to_record_minimal(self, evo_log: EvolutionLog) -> None:
        """最小字典反序列化。"""
        data = {
            "record_id": "minimal",
            "timestamp": "2024-01-01T00:00:00",
            "capability_gap": "test",
            "status": "idle",
        }
        record = evo_log._dict_to_record(data)
        assert record.record_id == "minimal"
        assert record.status == EvolutionStatus.IDLE

    def test_generated_artifact_code_excluded_in_serialization(
        self, evo_log: EvolutionLog,
    ) -> None:
        """序列化时 generated_artifact 的 code 字段被排除（设计决策）。"""
        artifact = GeneratedArtifact(
            generation_type=GenerationType.BUILTIN_TOOL,
            code="large code content here",
            file_path="test.py",
        )
        record = EvolutionRecord(
            record_id="no_code_test",
            timestamp="2024-01-01T00:00:00",
            capability_gap="test",
            generated_artifact=artifact,
        )

        data = evo_log._record_to_dict(record)
        # code 字段应被排除
        assert "code" not in data.get("generated_artifact", {})
        # file_path 保留
        assert data["generated_artifact"]["file_path"] == "test.py"


# =========================================================================
# create_record_id 测试
# =========================================================================


class TestCreateRecordId:
    """记录 ID 生成测试。"""

    def test_create_record_id_format(self, evo_log: EvolutionLog) -> None:
        """ID 格式以 evo_ 开头。"""
        record_id = evo_log.create_record_id()
        assert record_id.startswith("evo_")

    def test_create_record_id_unique(self, evo_log: EvolutionLog) -> None:
        """生成的 ID 唯一。"""
        ids = {evo_log.create_record_id() for _ in range(10)}
        assert len(ids) == 10


# =========================================================================
# MF-09 修复验证
# =========================================================================


class TestNoOsImport:
    """MF-09 修复验证：模块不导入 os。"""

    def test_no_os_import(self) -> None:
        """模块不导入 os（MF-09修复验证）。"""
        import evolution.evolution_log as mod

        # 不应有顶级 os 属性
        assert not hasattr(mod, "os"), "evolution_log 不应有顶级 os 导入"

    def test_source_no_top_level_os(self) -> None:
        """源代码中无顶级 os 导入。"""
        import evolution.evolution_log as mod

        source = inspect.getsource(mod)
        lines = source.split("\n")
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("import os") or stripped.startswith("from os"):
                indent = len(line) - len(line.lstrip())
                assert indent > 0, f"发现顶级 os 导入在第 {i+1} 行"
