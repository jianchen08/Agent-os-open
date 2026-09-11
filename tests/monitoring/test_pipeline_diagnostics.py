"""pipeline_diagnostics 管道诊断读面测试（trace 时间线投影 + state 全字段）。

覆盖三条行为线：
- project_trace 投影：summary 取值优先级（router 回复 > raw_result > 错误）、
  patch_data 字符串形态解析、异常形态不崩、摘要长度上界（性质断言）；
- list_pipeline_traces：seq 升序、limit 尾部截取但 total 不变、能力不可用降级空；
- get_pipeline_state_full：fields 透传（JSON 字符串不解析）、runs/summary 组装、
  缺参与能力不可用降级。
"""

from __future__ import annotations

import pytest

import kernel_reads
import pipeline_diagnostics as pd

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试


@pytest.fixture(autouse=True)
def _clean_providers():
    kernel_reads.reset_providers()
    yield
    kernel_reads.reset_providers()


def _set_provider(name: str, rows):
    async def _fn(*args, **kwargs):
        return rows

    kernel_reads.set_provider(name, _fn)


def _trace(seq: int, patch: dict | str, plugin_id: str = "llm_core") -> dict:
    return {
        "trace_id": f"tr-{seq}",
        "run_id": "run-1",
        "branch_id": "main",
        "seq_in_branch": seq,
        "plugin_id": plugin_id,
        "patch_type": "state_update",
        "patch_data": patch,
        "created_at": f"2026-09-08T00:00:{seq:02d}Z",
    }


class TestProjectTrace:
    """project_trace 单条投影行为。"""

    def test_full_patch_summary_prefers_router_text(self):
        """summary 优先取 router 回复文本并截断到 160；error/usage/tool 计数随行。"""
        entry = _trace(1, {
            "iteration": 3,
            "router": {"last_response_text": "r" * 300},
            "llm_usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            "_executed_tool_calls": [{"name": "file_read"}, {"name": "bash_execute"}],
            "raw_error": None,
        })
        row = pd.project_trace(entry)
        assert row["summary"] is not None
        assert len(row["summary"]) == 160  # 性质：摘要恒被截到上界
        assert row["iteration"] == 3
        assert row["llm_usage"]["total_tokens"] == 120
        assert row["tool_call_count"] == 2
        assert row["error"] is None

    def test_string_patch_data_parsed_and_raw_result_fallback(self):
        """patch_data 为 JSON 字符串时解析；无 router 时 summary 回退 raw_result 文本。"""
        entry = _trace(2, '{"raw_result": "任务已写入 /tmp/a.md", "raw_error": {"code": 7}}')
        row = pd.project_trace(entry)
        assert row["summary"] == "任务已写入 /tmp/a.md"
        # error 为结构化对象 → 序列化为字符串（不丢信息）
        assert row["error"] is not None and '"code"' in row["error"]

    def test_degenerate_patch_does_not_crash(self):
        """空 patch / 非 dict patch_data → 空投影而非异常（读面防御）。"""
        for patch in ({}, "not-json", None, 42):
            row = pd.project_trace(_trace(3, patch))
            assert row["summary"] is None
            assert row["tool_call_count"] == 0
            assert row["llm_usage"] is None

    def test_error_summary_when_no_content(self):
        """只有错误时 summary 以「错误:」前缀呈现（时间线上肉眼可辨失败步）。"""
        row = pd.project_trace(_trace(4, {"raw_error": "tool timeout after 5000ms"}))
        assert row["summary"].startswith("错误:")
        assert "5000ms" in row["summary"]


class TestListPipelineTraces:
    """list_pipeline_traces 列表行为。"""

    def test_ascending_and_limit_keeps_total(self):
        """seq 升序输出；limit 尾部截取，total 反映全量（分页语义：尾部最近）。"""
        _set_provider("traces", [_trace(i, {"iteration": i}) for i in range(1, 6)])
        import asyncio

        result = asyncio.run(pd.list_pipeline_traces("pipe-1", limit=2))
        assert result["total"] == 5
        assert [t["seq"] for t in result["traces"]] == [4, 5]

    def test_missing_pipeline_id_empty(self):
        import asyncio

        result = asyncio.run(pd.list_pipeline_traces(""))
        assert result == {"traces": [], "total": 0, "pipeline_id": ""}

    def test_provider_missing_degrades_empty(self):
        """traces provider 未注入 → 空载荷（前端契约不破坏）。"""
        import asyncio

        result = asyncio.run(pd.list_pipeline_traces("pipe-x"))
        assert result["traces"] == [] and result["total"] == 0


class TestGetPipelineStateFull:
    """get_pipeline_state_full 组装行为。"""

    def test_fields_runs_summary_assembled(self):
        """state 全字段按 field_key 透传原始字符串；runs 与摘要行并归一响应。"""
        _set_provider("db-admin-query", {
            "table": "pipeline_state", "total": 2, "limit": 500, "offset": 0,
            "rows": [
                {"field_key": "iteration", "field_value": "7", "updated_at": "t1"},
                {"field_key": "track.llm_usage", "field_value": '{"total_tokens": 900}', "updated_at": "t2"},
            ],
        })
        _set_provider("runs-by-pipeline", [{"run_id": "r1", "status": "completed"}])
        _set_provider("pipeline-state", [{"pipeline_id": "pipe-1", "message_count": 4}])

        import asyncio

        result = asyncio.run(pd.get_pipeline_state_full("pipe-1"))
        assert [f["field_key"] for f in result["fields"]] == ["iteration", "track.llm_usage"]
        # 全字段语义：field_value 保持 DB 原始字符串（不解析不裁剪）
        assert result["fields"][1]["field_value"] == '{"total_tokens": 900}'
        assert result["runs"][0]["run_id"] == "r1"
        assert result["summary"]["message_count"] == 4

    def test_summary_missing_is_none_not_crash(self):
        """state 摘要行缺失（冷管道）→ summary=None，fields/runs 照常返回。"""
        _set_provider("db-admin-query", {"rows": [{"field_key": "k", "field_value": "v", "updated_at": "t"}]})
        _set_provider("runs-by-pipeline", [])
        _set_provider("pipeline-state", [{"pipeline_id": "other"}])

        import asyncio

        result = asyncio.run(pd.get_pipeline_state_full("pipe-1"))
        assert result["summary"] is None
        assert len(result["fields"]) == 1 and result["runs"] == []

    def test_missing_pipeline_id_empty(self):
        import asyncio

        result = asyncio.run(pd.get_pipeline_state_full(""))
        assert result["fields"] == [] and result["runs"] == [] and result["summary"] is None

    def test_dbadmin_failure_degrades_empty_fields(self):
        """db-admin 能力不可用（provider 未注入）→ fields 空但不崩。"""
        import asyncio

        result = asyncio.run(pd.get_pipeline_state_full("pipe-1"))
        assert result["fields"] == []
