"""复盘系统增强端到端测试。

验证三处改造在真实组件（不连 LLM）上的集成正确性：

1. 改动1（review_agent.yaml）：真实加载 YAML 配置，断言 system_prompt 含新增的
   4 个执行过程质量维度 + JSON schema 含 process_dimension 字段。

2. 改动2（service.py 体系提示词注入）：真实装配 MemoryMaintenanceService，
   对真实存在的 lingxi agent 调用 _collect_agent_constraints，验证返回内容含
   解析后的完整 system_prompt（{{path:...}} 占位符已替换）+ 硬约束 + 软约束。

3. 改动3（service.py 通知含报告文件名）：_persist_review_result 真实写文件到
   tmp 目录并返回路径；_run_llm_review_task 收集路径后拼进 _notify_parent 通知。

不连真实 LLM，不验证 review_agent 的 LLM 输出质量，只验证三处改动的集成链路。
真实 LLM 全链路见 test_review_b_path_llm_e2e.py。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest

from memory.maintenance.service import MaintenanceConfig, MemoryMaintenanceService


# ============================================================
# 改动1：review_agent.yaml system_prompt 含新增维度
# ============================================================


class TestReviewAgentPromptEnhanced:
    """验证 review_agent.yaml 的 system_prompt 已注入 4 个执行过程质量维度。"""

    def _load_review_agent_config(self) -> dict:
        """真实加载 review_agent.yaml（走 YAML 解析，不 mock）。"""
        import yaml

        cfg_path = _PROJECT_ROOT / "config" / "agents" / "system" / "review_agent.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_yaml_loads_successfully(self):
        """YAML 能正常解析（语法没被改坏）。"""
        cfg = self._load_review_agent_config()
        assert cfg["config_id"] == "review_agent"
        assert cfg["is_active"] is True

    def test_system_prompt_has_process_quality_section(self):
        """system_prompt 含「分析 Agent 执行过程质量」小节。"""
        cfg = self._load_review_agent_config()
        sp = cfg["system_prompt"]
        assert "4. 分析 Agent 执行过程质量" in sp, "应含新增的执行过程质量分析小节"

    @pytest.mark.parametrize(
        "dimension,keyword",
        [
            ("wrong_tool", "工具调用正确性"),
            ("over_call", "多调用"),
            ("under_call", "少调用"),
            ("instruction_compliance", "指令遵循"),
        ],
    )
    def test_system_prompt_has_four_dimensions(self, dimension: str, keyword: str):
        """system_prompt 含全部 4 个执行过程质量维度。"""
        cfg = self._load_review_agent_config()
        sp = cfg["system_prompt"]
        assert dimension in sp, f"应含维度标识 {dimension}"
        assert keyword in sp, f"应含维度关键词「{keyword}」"

    def test_json_schema_has_process_dimension_field(self):
        """JSON 输出格式含 process_dimension 字段。"""
        cfg = self._load_review_agent_config()
        sp = cfg["system_prompt"]
        assert "process_dimension" in sp, "experiences schema 应含 process_dimension 字段"

    def test_existing_dimensions_preserved(self):
        """原有内容（5 Whys / 错误类别 / 复盘输出格式）未被破坏。"""
        cfg = self._load_review_agent_config()
        sp = cfg["system_prompt"]
        assert "5 Whys" in sp, "5 Whys 方法论应保留"
        assert "区分错误类别" in sp, "错误类别划分应保留"
        assert "experiences" in sp, "JSON experiences 结构应保留"
        assert "pipeline_run_id" in sp, "JSON pipeline_run_id 字段应保留"

    def test_hard_constraints_unchanged(self):
        """hard_constraints 仍是原来的 4 条（没被改动破坏）。"""
        cfg = self._load_review_agent_config()
        assert len(cfg["hard_constraints"]) == 4


# ============================================================
# 改动2：_collect_agent_constraints 注入完整体系提示词
# ============================================================


def _make_service(tmp_path) -> MemoryMaintenanceService:
    """装配真实 service（内存 storage，依赖置最小）。"""
    from infrastructure.execution_record_storage import ExecutionRecordStorage

    storage = ExecutionRecordStorage(data_dir=str(tmp_path))
    return MemoryMaintenanceService(
        storage=storage,
        chunk_db=None,
        knowledge_service=None,
        config=MaintenanceConfig(enabled=True),
        review_context_window=128_000,
    )


class TestCollectAgentConstraintsInjectsFullPrompt:
    """验证 _collect_agent_constraints 注入被复盘 agent 的完整体系提示词。"""

    @pytest.mark.asyncio
    async def test_lingxi_full_prompt_resolved(self, tmp_path):
        """对真实 lingxi agent，system_prompt 占位符全部解析。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["lingxi"])

        # lingxi 的 system_prompt 含 {{path:config/agents/main/persona/lingxi_persona.md}}
        # 等占位符，解析后不应再有 {{ 字样
        assert block, "lingxi 应产出非空约束块"
        assert "{{" not in block, "占位符应全部解析，不应残留 {{"

    @pytest.mark.asyncio
    async def test_block_contains_resolved_system_prompt_content(self, tmp_path):
        """约束块含 lingxi system_prompt 解析后的真实内容（角色定义）。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["lingxi"])

        # lingxi system_prompt 解析后含「智能助理」「派发」等角色定义关键词
        assert "完整体系提示词" in block
        assert "派发" in block or "调度" in block, "应含 lingxi 角色定义内容"

    @pytest.mark.asyncio
    async def test_block_contains_hard_constraints(self, tmp_path):
        """约束块含 lingxi 的硬约束原文。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["lingxi"])

        # lingxi 硬约束含「你是接单派单的角色」
        assert "硬约束" in block
        assert "接单派单" in block, "应含 lingxi 硬约束原文"

    @pytest.mark.asyncio
    async def test_block_contains_soft_constraints(self, tmp_path):
        """约束块含 lingxi 的软约束。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["lingxi"])

        assert "软约束" in block

    @pytest.mark.asyncio
    async def test_nonexistent_agent_skipped(self, tmp_path):
        """不存在的 agent_id 被跳过，返回空。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["nonexistent_agent_xyz"])
        assert block == "", "不存在的 agent 应跳过返回空"

    @pytest.mark.asyncio
    async def test_empty_agent_ids_return_empty(self, tmp_path):
        """全空的 agent_id 列表返回空。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["", "", None])  # type: ignore[list-item]
        assert block == ""

    @pytest.mark.asyncio
    async def test_dedup_same_agent(self, tmp_path):
        """同一 agent 多次出现只产出一份约束块。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["lingxi", "lingxi", "lingxi"])
        # 「【lingxi」标记应只出现 1 次
        assert block.count("【lingxi") == 1, "同一 agent 应去重"

    @pytest.mark.asyncio
    async def test_mixed_agents_each_get_block(self, tmp_path):
        """多个不同 agent 各自产出独立的约束块。"""
        svc = _make_service(tmp_path)
        # lingxi 一定存在；code_reviewer_agent 也存在
        block = await svc._collect_agent_constraints(["lingxi", "code_reviewer_agent"])
        assert "【lingxi" in block
        assert "【code_reviewer_agent" in block

    @pytest.mark.asyncio
    async def test_resolved_prompt_longer_than_raw(self, tmp_path):
        """解析后的 prompt 比原文长（占位符替换进了文件内容）。"""
        from agents.global_registry import get_global_agent_registry_sync

        svc = _make_service(tmp_path)
        raw_prompt = get_global_agent_registry_sync().get("lingxi").system_prompt
        block = await svc._collect_agent_constraints(["lingxi"])

        # block 含解析后内容 + 约束，应显著长于裸 system_prompt 原文
        assert len(block) > len(raw_prompt), (
            f"解析后块({len(block)}) 应长于原文({len(raw_prompt)})"
        )


# ============================================================
# 改动3：_persist_review_result 返回路径 + 通知含文件名
# ============================================================


class TestPersistReturnsPath:
    """_persist_review_result 返回写入的文件路径。"""

    @pytest.mark.asyncio
    async def test_returns_path_when_written(self, tmp_path, monkeypatch):
        """有报告内容时返回真实文件路径。"""
        svc = _make_service(tmp_path)
        # 报告写到 tmp，避免污染真实 docs/working
        monkeypatch.chdir(tmp_path)

        path = await svc._persist_review_result("test-pid-abc", '{"summary":"测试报告"}')

        assert path is not None
        assert "review_report_test-pid-abc.md" in path
        assert os.path.exists(path), "文件应真实写入磁盘"

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_report(self, tmp_path):
        """空 report_text 返回 None。"""
        svc = _make_service(tmp_path)
        path = await svc._persist_review_result("x", "")
        assert path is None

    @pytest.mark.asyncio
    async def test_file_contains_report_content(self, tmp_path, monkeypatch):
        """写入的文件含报告内容。"""
        svc = _make_service(tmp_path)
        monkeypatch.chdir(tmp_path)

        report = "# 测试报告\n\n## 经验\n- 经验1"
        path = await svc._persist_review_result("pid-content", report)

        assert path is not None
        content = Path(path).read_text(encoding="utf-8")
        assert "测试报告" in content
        assert "经验1" in content


class TestNotificationIncludesReportPaths:
    """_run_llm_review_task 通知含产出报告的文件名。

    通过 monkeypatch 拦截 _try_launch_review_agent / _await_child_report /
    _notify_parent，让 _persist_review_result 真实写文件（不 patch 它），
    验证通知 summary 里列出报告文件名。
    """

    @pytest.mark.asyncio
    async def test_notification_lists_report_filenames(self, tmp_path, monkeypatch):
        """通知里列出产出报告的文件名（相对路径）。"""
        svc = _make_service(tmp_path)
        monkeypatch.chdir(tmp_path)

        # 灌 1 个 pending 目标（单批）
        from infrastructure.execution_record_storage import PipelineRunSummary

        svc._storage.save_summary(PipelineRunSummary(
            run_id="target-1", status="success", review_status="pending",
            total_records=5, total_iterations=1,
        ))

        notifications: list[dict] = []

        async def fake_launch(targets):  # noqa: ANN001
            return "child-pid-001", True

        async def fake_await_report(child_pid):  # noqa: ANN001
            return '{"summary":"假复盘报告","experiences":[]}'

        async def fake_notify(parent_pid, status, summary):  # noqa: ANN001
            notifications.append({"status": status, "summary": summary})

        svc._try_launch_review_agent = fake_launch  # type: ignore[method-assign]
        svc._await_child_report = fake_await_report  # type: ignore[method-assign]
        svc._notify_parent = fake_notify  # type: ignore[method-assign]

        await svc._run_llm_review_task("")

        assert len(notifications) == 1
        notif = notifications[0]
        assert notif["status"] == "completed"
        # 通知应含「详细报告」段和文件名
        assert "详细报告" in notif["summary"], "通知应含详细报告段"
        assert "review_report_child-pid-001.md" in notif["summary"], (
            "通知应含报告文件名"
        )

    @pytest.mark.asyncio
    async def test_notification_omits_report_section_when_no_report(self, tmp_path, monkeypatch):
        """无报告产出（_await_child_report 返回空）时通知不含详细报告段。"""
        svc = _make_service(tmp_path)
        monkeypatch.chdir(tmp_path)

        from infrastructure.execution_record_storage import PipelineRunSummary

        svc._storage.save_summary(PipelineRunSummary(
            run_id="target-2", status="success", review_status="pending",
            total_records=5, total_iterations=1,
        ))

        notifications: list[dict] = []

        async def fake_launch(targets):  # noqa: ANN001
            return "child-pid-002", True

        async def fake_await_report(child_pid):  # noqa: ANN001
            return ""  # 无报告产出

        async def fake_notify(parent_pid, status, summary):  # noqa: ANN001
            notifications.append({"status": status, "summary": summary})

        svc._try_launch_review_agent = fake_launch  # type: ignore[method-assign]
        svc._await_child_report = fake_await_report  # type: ignore[method-assign]
        svc._notify_parent = fake_notify  # type: ignore[method-assign]

        await svc._run_llm_review_task("")

        notif = notifications[0]
        assert "详细报告" not in notif["summary"], "无报告时不应有详细报告段"

    @pytest.mark.asyncio
    async def test_multiple_reports_all_listed(self, tmp_path, monkeypatch):
        """多批复盘产出多个报告，通知里全部列出。"""
        svc = _make_service(tmp_path)
        monkeypatch.chdir(tmp_path)

        from infrastructure.execution_record_storage import PipelineRunSummary

        # 灌 2 个目标，配小窗口强制切 2 批（每目标 100 记录 × 15 = 1500 token，
        # 单批预算 10000×15%=1500 → 每批只塞 1 个）
        svc._config = MaintenanceConfig(
            enabled=True, skeleton_budget_percent=15,
            records_per_skeleton_token=15, review_batch_limit=3,
        )
        svc._review_context_window = 10_000  # type: ignore[assignment]
        for i in range(2):
            svc._storage.save_summary(PipelineRunSummary(
                run_id=f"multi-{i}", status="success", review_status="pending",
                total_records=100, total_iterations=10,
            ))

        notifications: list[dict] = []
        pid_counter = [0]

        async def fake_launch(targets):  # noqa: ANN001
            pid_counter[0] += 1
            return f"multi-child-{pid_counter[0]}", True

        async def fake_await_report(child_pid):  # noqa: ANN001
            return '{"summary":"报告"}'

        async def fake_notify(parent_pid, status, summary):  # noqa: ANN001
            notifications.append({"status": status, "summary": summary})

        svc._try_launch_review_agent = fake_launch  # type: ignore[method-assign]
        svc._await_child_report = fake_await_report  # type: ignore[method-assign]
        svc._notify_parent = fake_notify  # type: ignore[method-assign]

        await svc._run_llm_review_task("")

        notif = notifications[0]
        assert "multi-child-1.md" in notif["summary"], "应列出第 1 个报告"
        assert "multi-child-2.md" in notif["summary"], "应列出第 2 个报告"


# ============================================================
# 集成：三处改动联动（_collect_agent_constraints 产出 → 触发消息）
# ============================================================


class TestConstraintsBlockUsableInTriggerMessage:
    """验证 _collect_agent_constraints 的产出能直接拼进触发消息（格式可用）。"""

    @pytest.mark.asyncio
    async def test_block_is_plain_string(self, tmp_path):
        """产出是纯字符串，可直接 f-string 拼接（不会类型错误）。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["lingxi"])
        assert isinstance(block, str)

        # 模拟 _try_launch_review_agent 里的拼接（验证不会因类型/格式崩）
        content = f"[触发复盘]\n\n{block}\n\n分析要求：..."
        assert "完整体系提示词" in content

    @pytest.mark.asyncio
    async def test_block_has_clear_delimiters(self, tmp_path):
        """产出块有清晰的标题分隔，review_agent 能识别边界。"""
        svc = _make_service(tmp_path)
        block = await svc._collect_agent_constraints(["lingxi"])
        # 开头应是说明性标题
        assert block.startswith("被复盘 Agent 的完整体系提示词")
        # 每个 agent 块用 【xxx】 标记
        assert "【lingxi" in block


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
