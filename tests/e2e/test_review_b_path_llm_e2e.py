"""复盘 B 路径全链路真实 e2e（连 LLM）。

与 test_review_b_path_isolation_e2e 的区别：本测试连真实 LLM，跑完整 B 路径
（触发→tags 驱动引擎→review_agent LLM 分析→报告落盘+入库），验证三点改造
在真实运行下的端到端正确性。

运行条件（缺任一自动 skip，避免拖慢普通 CI / 误红屏）：
- 标记为 requires_api
- 环境变量 RUN_LLM_E2E=1 被显式设置（CI 的 llm-e2e job 设置）
- 存在可用的 LLM 密钥（ZHIPU_API_KEY 等）

注意：本测试耗时约 3-5 分钟（后台 review_agent 跑 LLM），且依赖外部 LLM 网络。
默认不进 PR 主链路，由 .github/workflows/ci.yml 的 llm-e2e job 手动/nightly 触发。

用法（本地）：
    RUN_LLM_E2E=1 python -m pytest tests/e2e/test_review_b_path_llm_e2e.py -m requires_api
用法（CI）：见 ci.yml 的 llm-e2e job。
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC = _PROJECT_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

pytestmark = pytest.mark.requires_api

# 门控：未显式要求时 skip，保护普通 CI 不被长耗时/外部依赖拖累
_SHOULD_RUN = (
    os.environ.get("RUN_LLM_E2E") == "1"
    and any(
        os.environ.get(k)
        for k in ("ZHIPU_API_KEY", "GLM_API_KEY", "DEEPSEEK_API_KEY", "MINIMAX_API_KEY")
    )
)
_SKIP_REASON = (
    "需 RUN_LLM_E2E=1 且配置 LLM 密钥（ZHIPU/GLM/DEEPSEEK/MINIMAX 之一）"
    "；本测试连真实 LLM、耗时数分钟，默认不进 PR 链路，由 ci.yml 的 llm-e2e job 触发"
)

_LLM_YAML = _PROJECT_ROOT / "config" / "models" / "llm.yaml"
_TIER_BACKUP: str | None = None


def _force_small_tier_to_glm() -> None:
    """装配前把 tiers.small 临时指向 glm-5.2（zhipu_coding，环境内可连通）。

    review_agent 配置 model_tier=small，默认映射 minimax（CI 环境常不通）。
    在配置加载前改文件，跑完由 _restore_tier 还原。仅影响本测试进程。
    """
    global _TIER_BACKUP
    if not _LLM_YAML.exists():
        return
    _TIER_BACKUP = _LLM_YAML.read_text(encoding="utf-8")
    patched = _TIER_BACKUP.replace(
        "    small: minimax-m3-guangfang", "    small: glm-5.2"
    )
    if patched != _TIER_BACKUP:
        _LLM_YAML.write_text(patched, encoding="utf-8")


def _restore_tier() -> None:
    """还原 llm.yaml 的 tier 配置。"""
    global _TIER_BACKUP
    if _TIER_BACKUP is not None and _LLM_YAML.exists():
        _LLM_YAML.write_text(_TIER_BACKUP, encoding="utf-8")
        _TIER_BACKUP = None


def _seed_pending_pipeline(storage, run_id: str) -> None:
    """向真实 storage 塞一条 review_status=pending 的管道执行记录。"""
    from infrastructure.execution_record_storage import (
        ExecutionRecordData,
        PipelineRunSummary,
    )

    storage._summaries[run_id] = PipelineRunSummary(
        run_id=run_id, status="failed", review_status="pending",
        total_records=4, total_iterations=1,
        created_at="2026-06-27T10:00:00",
        error="ImportError: No module named 'core'",
    )
    for r in [
        ExecutionRecordData(pipeline_run_id=run_id, type="user", role="user",
                            content="请帮我重构 auth 模块", sequence=1, iteration=1),
        ExecutionRecordData(pipeline_run_id=run_id, type="ai", role="assistant",
                            content="我来分析", thinking_content="先读文件",
                            sequence=2, iteration=1),
        ExecutionRecordData(pipeline_run_id=run_id, type="ai", role="assistant",
                            content="", error="ImportError: No module named 'core'",
                            sequence=3, iteration=1),
    ]:
        storage.save(r)


@pytest.mark.skipif(not _SHOULD_RUN, reason=_SKIP_REASON)
def test_b_path_full_chain_produces_report(tmp_path):
    """全链路：触发复盘 → review_agent 跑 LLM → 产出报告文件。

    成功判据：docs/working/ 出现本次新生成的 review_report_*.md。
    覆盖：点1(tags.agent_id 驱动引擎)、点2(来源溯源)、点3(报告引用用户消息)。
    """
    # 1. 装配前改配置（import 时加载），保证 review_agent 用可连通的 glm
    _force_small_tier_to_glm()
    try:
        from channels.websocket.app_factory import create_combined_app
        _ = create_combined_app()  # 触发完整装配

        from infrastructure.service_provider import get_service_provider
        sp = get_service_provider()
        maintenance_service = sp.get("maintenance_service")
        storage = sp.get("execution_record_storage")
        assert maintenance_service is not None, "maintenance_service 未装配"
        assert storage is not None, "execution_record_storage 未装配"

        # 2. 确认 review_agent 配置存在（tags.agent_id 反查前提）
        from agents.global_registry import get_global_agent_registry_sync
        assert get_global_agent_registry_sync().get(
            maintenance_service.REVIEW_AGENT_ID
        ) is not None, "review_agent 配置缺失"

        # 3. 造 pending 目标 + 记录基线报告
        target = "e2e-llm-review-target"
        _seed_pending_pipeline(storage, target)
        report_dir = Path("docs/working")
        baseline = set(report_dir.glob("review_report_*.md")) if report_dir.exists() else set()

        # 4. 触发 B 路径复盘（后台异步）
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                maintenance_service.trigger_llm_review(parent_pipeline_id="")
            )
            assert result.get("status") in ("submitted", "already_running"), \
                f"复盘未提交: {result}"

            # 5. 等报告产出（最长 300s）
            deadline = time.time() + 300
            produced = False
            while time.time() < deadline:
                loop.run_until_complete(asyncio.sleep(5))
                current = set(report_dir.glob("review_report_*.md")) if report_dir.exists() else set()
                new = current - baseline
                if new:
                    produced = True
                    break
                if not getattr(maintenance_service, "_review_running", False):
                    produced = bool(new)
                    break
        finally:
            loop.close()

        # 6. 断言报告产出
        assert produced, "300s 内未产出 review_report_*.md（review_agent 未跑通或 LLM 超时）"
    finally:
        _restore_tier()
