# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: none-local
"""复盘报告持久化 TDD 测试（Step 5b + F-REVIEW-2）。

验证内容（与任务规格 4 个用例对齐）：
1. store_report 在 _memory_backend 注入时调用 backend.add，memory_type="review"
2. store_report 仍更新内存 _reports dict（保留给 get_report 立即轮询）
3. _memory_backend=None 时只走内存路径，不崩溃
4. store_report 后 get_report 返回 status=completed 的完整报告

F-REVIEW-2 扩展（review 真实完成事件，轮询语义）：
- get_report 经 pipeline-executor.get_run_status 能力查子管道 run 状态，
  run 真实完成才落 completed（不再"启动即 completed（乐观，空 lessons）"）
- run 失败落 failed；进行中/挂起/查询失败保持 running（不崩）

唯一外部依赖是注入的 IMemoryBackend（用 AsyncMock 替身）与 fake pipeline
能力（CapabilityHandle 注入），不接入真实 hindsight/内核。

[来源: docs/tasks Step 5b 复盘报告落 Hindsight]
[来源: F-REVIEW-2 review 真实完成事件]
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agentos_plugin_sdk.capability import CapabilityHandle

pytestmark = pytest.mark.unit

# 插件目录加入 sys.path（与 hindsight_memory/test_server.py 同款 setup）
_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 review/server.py 模块（每次新建，避免模块级状态跨测试污染）。

    用 module_from_spec + exec_module 直接重建，隔离 _reports/_memory_backend 全局状态。
    """
    mod_name = "review_server_step5b_test"
    plugin_path = _PLUGIN_DIR / "server.py"
    assert plugin_path.exists(), f"server.py missing at {plugin_path}"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod() -> Any:
    """加载 review server 模块，每个测试独立（重置 _reports 与 _memory_backend）。"""
    module = _load_module()
    # 清空模块级状态，避免跨测试污染
    module._reports.clear()
    module._run_ids.clear()
    module._memory_backend = None
    return module


@pytest.fixture
def mock_backend() -> AsyncMock:
    """构造一个 IMemoryBackend 替身（AsyncMock），add 返回一个 memory id。"""
    backend = AsyncMock()
    backend.add.return_value = "mem-review-1"
    return backend


def _inject_pipeline_capability(
    mod: Any, status: str = "running", run_id: str = "run-abc"
) -> None:
    """注入 fake pipeline-executor 能力（F-REVIEW-2 轮询链路）。

    0.2 收尾：start_run 占位已随旧引擎移除（trigger_review 走本地降级），
    此处仅 mock get_run_status（get_report 轮询链路）。
    """

    async def fake_call(
        method: str, params: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        if method == "get_run_status":
            return {"run_id": run_id, "status": status}
        raise AssertionError(f"unexpected capability method: {method}")

    mod.plugin._capabilities["pipeline-executor"] = CapabilityHandle(
        "pipeline-executor", call_fn=fake_call
    )


def _seed_running_report(mod: Any, review_id: str, run_id: str = "run-abc") -> None:
    """按 trigger_review 成功路径登记 running 报告 + run_id。"""
    mod._run_ids[review_id] = run_id
    mod._reports[review_id] = {
        "review_id": review_id,
        "task_id": "task-x",
        "summary": "s",
        "artifacts": [],
        "metrics": {},
        "status": "running",
        "run_id": run_id,
        "created_at": 1.0,
    }


# ═══════════════════════════════════════════════════════════
# 1. store_report 落到 backend
# ═══════════════════════════════════════════════════════════


class TestStoreReportPersists:
    async def test_store_report_persists_to_backend(
        self, mod: Any, mock_backend: AsyncMock
    ) -> None:
        """store_report 在 _memory_backend 注入时调用 backend.add，
        memory_type="review"，且 tags 含 review_id 与 review_report。"""
        mod.set_memory_backend(mock_backend)

        report = {
            "task_id": "task-1",
            "lessons": ["lesson-a"],
            "recommendations": ["rec-a"],
        }
        await mod.store_report("review-1", report)

        mock_backend.add.assert_awaited_once()
        kwargs = mock_backend.add.call_args.kwargs
        assert kwargs["memory_type"] == "review"
        tags = kwargs.get("tags") or []
        assert any("review-1" in t for t in tags), f"tags 应含 review_id，实际: {tags}"
        assert "review_report" in tags
        # source 标注复盘来源
        assert kwargs.get("source") == "review_agent"


# ═══════════════════════════════════════════════════════════
# 2. store_report 仍更新内存 _reports
# ═══════════════════════════════════════════════════════════


class TestStoreReportInMemory:
    async def test_store_report_keeps_inmemory(
        self, mod: Any, mock_backend: AsyncMock
    ) -> None:
        """store_report 仍把报告写入内存 _reports dict（供 get_report 立即轮询）。"""
        mod.set_memory_backend(mock_backend)

        await mod.store_report(
            "review-2", {"task_id": "task-2", "lessons": ["l1"]}
        )

        assert "review-2" in mod._reports
        entry = mod._reports["review-2"]
        assert entry["status"] == "completed"
        assert entry.get("lessons") == ["l1"]


# ═══════════════════════════════════════════════════════════
# 3. _memory_backend=None 时降级
# ═══════════════════════════════════════════════════════════


class TestStoreReportWithoutBackend:
    async def test_store_report_without_backend_degrades(self, mod: Any) -> None:
        """_memory_backend=None 时只走内存路径，不调用任何 backend，不崩溃。"""
        # 默认 mod fixture 已置 _memory_backend=None
        await mod.store_report(
            "review-3", {"task_id": "task-3", "lessons": ["l-degrade"]}
        )

        # 内存仍更新
        assert "review-3" in mod._reports
        assert mod._reports["review-3"]["status"] == "completed"


# ═══════════════════════════════════════════════════════════
# 4. store_report 后 get_report 返回完整报告
# ═══════════════════════════════════════════════════════════


class TestGetReportAfterStore:
    async def test_get_report_returns_persisted(
        self, mod: Any, mock_backend: AsyncMock
    ) -> None:
        """store_report 后 get_report 返回 status=completed 的完整报告。"""
        mod.set_memory_backend(mock_backend)

        report = {
            "task_id": "task-4",
            "summary": "复盘摘要",
            "lessons": ["lesson-x"],
            "recommendations": ["rec-x"],
        }
        await mod.store_report("review-4", report)

        got = await mod.get_report("review-4")
        assert got["status"] == "completed"
        assert got["task_id"] == "task-4"
        assert got["lessons"] == ["lesson-x"]


# ═══════════════════════════════════════════════════════════
# F-REVIEW-2: review 真实完成事件（轮询子管道 run 状态）
# 语义：completed 只由子管道真实完成触发，不再"启动即 completed（乐观，空 lessons）"
# ═══════════════════════════════════════════════════════════


class TestGetReportRunStatusPolling:
    """get_report 经 pipeline-executor.get_run_status 轮询子管道真实状态。"""

    async def test_run_completed_finalizes_report(self, mod: Any) -> None:
        """run 状态 completed → get_report 把 report 落为 completed。"""
        _inject_pipeline_capability(mod, status="completed")
        _seed_running_report(mod, "review-5")

        got = await mod.get_report("review-5")
        assert got["status"] == "completed"
        assert got["run_status"] == "completed"
        assert "completed_at" in got
        assert got["run_id"] == "run-abc"

    async def test_run_still_running_keeps_running(self, mod: Any) -> None:
        """run 仍 running → report 保持 running，不提前 completed。"""
        _inject_pipeline_capability(mod, status="running")
        _seed_running_report(mod, "review-6")

        got = await mod.get_report("review-6")
        assert got["status"] == "running"
        assert got.get("run_status") == "running"

    async def test_run_suspended_keeps_running(self, mod: Any) -> None:
        """run 挂起 → report 保持 running（记录 run_status 供调用方）。"""
        _inject_pipeline_capability(mod, status="suspended")
        _seed_running_report(mod, "review-6b")

        got = await mod.get_report("review-6b")
        assert got["status"] == "running"
        assert got.get("run_status") == "suspended"

    async def test_run_failed_marks_report_failed(self, mod: Any) -> None:
        """run 失败 → report 落 failed（不再无限 running）。"""
        _inject_pipeline_capability(mod, status="failed")
        _seed_running_report(mod, "review-7")

        got = await mod.get_report("review-7")
        assert got["status"] == "failed"
        assert got["run_status"] == "failed"
        assert "failed_at" in got

    async def test_no_capability_keeps_running_degrades(self, mod: Any) -> None:
        """能力未注入（独立进程/降级）→ 查询失败，保持 running，不崩。"""
        _seed_running_report(mod, "review-8")

        got = await mod.get_report("review-8")
        assert got["status"] == "running"
        assert got.get("run_status") is None

    async def test_run_status_call_failure_keeps_running(self, mod: Any) -> None:
        """内核 get_run_status 报错 → 降级保持 running，不崩。"""

        async def failing_call(
            method: str, params: dict[str, Any], timeout: float | None = None
        ) -> dict[str, Any]:
            raise RuntimeError("kernel unreachable")

        mod.plugin._capabilities["pipeline-executor"] = CapabilityHandle(
            "pipeline-executor", call_fn=failing_call
        )
        _seed_running_report(mod, "review-8b")

        got = await mod.get_report("review-8b")
        assert got["status"] == "running"


class TestTriggerReviewDegrade:
    """trigger_review 行为（0.2 收尾：start_run 占位已移除，固定本地降级）。

    review_agent 深度复盘待接入 chat.send_message → PipelineExecutor 路径
    （见 server.py 模块头注释），届时恢复 running/轮询链路测试。
    """

    async def test_trigger_degrades_locally(self, mod: Any) -> None:
        """trigger 直接本地降级报告（status=completed, mode=local_degrade）。"""
        triggered = await mod.trigger_review(
            task_id="task-11",
            summary="无能力环境",
            metrics={"accuracy": 0.3},
        )
        assert triggered["status"] == "completed"
        assert triggered["mode"] == "local_degrade"
        got = await mod.get_report(triggered["review_id"])
        assert got["mode"] == "local_degrade"
        assert got["status"] == "completed"


# ═══════════════════════════════════════════════════════════
# GAP-1：深度复盘经 chat.send_message 起 review_agent 管道
# ═══════════════════════════════════════════════════════════


def _inject_chat_capability(mod: Any, result: Any = None, error: Exception | None = None) -> list[dict]:
    """注入 fake chat 能力，记录 send_message 入参。"""
    calls: list[dict] = []

    async def fake_call(
        method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        assert method == "send_message"
        calls.append(params)
        if error:
            raise error
        return result if result is not None else {
            "status": "created",
            "pipeline_id": "pipe_review_gen_1",
        }

    mod.plugin._capabilities["chat"] = CapabilityHandle("chat", call_fn=fake_call)
    return calls


def _inject_pipeline_state_capability(mod: Any, rows: list[dict]) -> None:
    """注入 fake pipeline-state 能力（get_report 轮询复盘管道状态）。"""

    async def fake_call(
        method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        assert method == "list"
        return rows

    mod.plugin._capabilities["pipeline-state"] = CapabilityHandle(
        "pipeline-state", call_fn=fake_call
    )


class TestTriggerReviewDispatch:
    async def test_trigger_review_starts_review_pipeline(self, mod: Any) -> None:
        """chat 可用 → 经 send_message 起 review 管道（不再 local_degrade）。"""
        calls = _inject_chat_capability(mod)
        r = await mod.trigger_review(
            task_id="task-9", summary="复盘周报任务", artifacts=["a.md"], metrics={"quality": 0.8}
        )
        assert r["status"] == "running"
        assert r["mode"] == "pipeline"
        assert r["pipeline_id"] == "pipe_review_gen_1"

        # 两次调用：创建复盘管道 + 登记复盘管道到任务管道 state（task.owned.*）
        assert len(calls) == 2
        p = calls[0]
        assert p["create"] is True
        assert p["background"] is True
        # 血缘：根形式（系统组件，诚实声明复盘来源——不伪造父）
        assert p["lineage"] == {"root": True, "origin": {"kind": "plugin", "source": "review"}}
        # 登记调用：复盘管道 id 写回被复盘任务管道（管道树数据链）
        reg = calls[1]
        assert reg["pipeline_id"] == "task-9"
        assert reg["no_dispatch"] is True
        assert reg["state"]["task.owned.pipe_review_gen_1.title"] == "复盘 task-9"
        assert reg["state"]["task.owned.pipe_review_gen_1.scope"] == "non_container"
        # state：复盘对象 + 复盘输入出生即入
        assert p["state"]["task.id"] == "task-9"
        assert p["state"]["review.summary"] == "复盘周报任务"
        assert p["state"]["review.artifacts"] == ["a.md"]
        assert p["state"]["review.metrics"] == {"quality": 0.8}
        assert "复盘" in p["message"] and "task-9" in p["message"]

        # 报告登记为 running（get_report 轮询入口）
        report = mod._reports[r["review_id"]]
        assert report["status"] == "running"
        assert report["pipeline_id"] == "pipe_review_gen_1"

    async def test_trigger_review_degrades_without_chat(self, mod: Any) -> None:
        """chat capability 缺席 → 维持 local_degrade 兜底（不崩）。"""
        mod.plugin._capabilities.pop("chat", None)
        r = await mod.trigger_review(task_id="task-1", summary="s")
        assert r["status"] == "completed"
        assert r["mode"] == "local_degrade"

    async def test_trigger_review_skips_registration_without_task(self, mod: Any) -> None:
        """无 task_id（系统级复盘）只创建管道不登记（无归属任务可写回）。"""
        calls = _inject_chat_capability(mod)
        r = await mod.trigger_review(task_id="", summary="系统复盘")
        assert r["status"] == "running"
        assert len(calls) == 1  # 仅创建调用，无登记调用

    async def test_trigger_review_degrades_on_dispatch_error(self, mod: Any) -> None:
        """派发失败（内核错误）→ local_degrade 兜底（不崩）。"""
        _inject_chat_capability(mod, error=RuntimeError("kernel down"))
        r = await mod.trigger_review(task_id="task-1", summary="s")
        assert r["mode"] == "local_degrade"


# ═══════════════════════════════════════════════════════════
# G3 冷读兜底：sidecar 重启后 get_report 从 Hindsight 取回已持久化报告
# （0.2 收尾 §3.1；IMemoryBackend 只有相似度 search，无按 id/tags 精确检索，
#  精确校验 review_id == 入参由读侧自行完成，防相似度误召回）
# ═══════════════════════════════════════════════════════════


class _StubMemoryBackend:
    """IMemoryBackend 替身：记录 add/search/get_documents 调用，返回统一形态条目。

    条目形态对齐 HindsightBackend._map_hindsight_results 输出：
    {id, content, score, memory_type, metadata}——content 为报告 JSON 串。
    get_documents 模拟 HindsightBackend 文档面（缺陷②冷读新路径）。
    """

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.search_calls: list[dict[str, Any]] = []
        self.search_error: Exception | None = None
        self.add_calls: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.get_documents_calls: list[dict[str, Any]] = []
        self.get_documents_error: Exception | None = None

    async def add(
        self,
        user_id: str,
        content: str,
        memory_type: str = "semantic",
        tags: list[str] | None = None,
        source: str = "",
        metadata: dict[str, str] | None = None,
    ) -> str:
        self.add_calls.append(
            {
                "user_id": user_id,
                "content": content,
                "memory_type": memory_type,
                "tags": tags,
                "source": source,
                "metadata": metadata,
            }
        )
        self.entries.append(
            {
                "id": f"mem-{len(self.entries) + 1}",
                "content": content,
                "memory_type": memory_type,
                "metadata": {"tags": tags or [], "source": source, "bank": user_id},
            }
        )
        return f"mem-{len(self.entries)}"

    async def get_documents(
        self,
        user_id: str,
        document_id: str = "",
        tags: list[str] | None = None,
        tags_match: str = "any_strict",
        q: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        self.get_documents_calls.append(
            {
                "user_id": user_id,
                "document_id": document_id,
                "tags": tags,
                "tags_match": tags_match,
                "q": q,
                "limit": limit,
            }
        )
        if self.get_documents_error is not None:
            raise self.get_documents_error
        return list(self.documents[:limit])

    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 5,
        memory_type: str | None = None,
    ) -> list[dict[str, Any]]:
        self.search_calls.append(
            {
                "query": query,
                "user_id": user_id,
                "top_k": top_k,
                "memory_type": memory_type,
            }
        )
        if self.search_error is not None:
            raise self.search_error
        return [dict(entry, score=0.9) for entry in self.entries[:top_k]]


class TestStoreReportFixedBank:
    async def test_store_report_writes_fixed_review_bank(
        self, mod: Any, mock_backend: AsyncMock
    ) -> None:
        """写侧 user_id 固定 "review" bank（冷读定向检索前提），不随 task_id 漂移。

        task_id 信息保留在 content JSON 内不丢（报告体本身含 task_id 字段）。
        """
        mod.set_memory_backend(mock_backend)
        await mod.store_report(
            "review-b1", {"task_id": "task-b1", "lessons": ["l"]}
        )
        kwargs = mock_backend.add.call_args.kwargs
        assert kwargs["user_id"] == "review"


class TestGetReportColdRead:
    async def test_cold_read_recovers_report_after_restart(self, mod: Any) -> None:
        """红1：_reports 清空（模拟 sidecar 重启）→ 经 Hindsight 取回已持久化报告。"""
        stub = _StubMemoryBackend()
        mod.set_memory_backend(stub)
        await mod.store_report(
            "review-c1",
            {"task_id": "task-c1", "summary": "冷读报告", "lessons": ["l-c1"]},
        )
        mod._reports.clear()  # 模拟重启丢内存

        got = await mod.get_report("review-c1")
        assert got.get("error") is None
        assert got["review_id"] == "review-c1"
        assert got["status"] == "completed"
        assert got["lessons"] == ["l-c1"]
        # 回填内存：后续 get_report 直接走内存路径
        assert mod._reports["review-c1"]["status"] == "completed"
        # 检索参数：定向 review bank + memory_type=review 过滤
        assert stub.search_calls, "冷读应调用 backend.search"
        last = stub.search_calls[-1]
        assert last["user_id"] == "review"
        assert last["memory_type"] == "review"
        assert last["query"] == "review-c1"

    async def test_cold_read_rejects_mismatched_review_id(self, mod: Any) -> None:
        """红2：search 相似召回但 review_id 不匹配（含非 JSON 条目）→ 精确校验
        拒绝采纳，维持 not found，不污染 _reports。"""
        stub = _StubMemoryBackend()
        stub.entries.append(
            {
                "id": "mem-1",
                "content": json.dumps(
                    {"review_id": "review-OTHER", "status": "completed", "lessons": ["x"]}
                ),
                "memory_type": "review",
                "metadata": {},
            }
        )
        stub.entries.append(
            {"id": "mem-2", "content": "not-a-json-content", "memory_type": "review", "metadata": {}}
        )
        mod.set_memory_backend(stub)

        got = await mod.get_report("review-missing")
        assert got == {"error": "review not found", "review_id": "review-missing"}
        assert "review-missing" not in mod._reports

    async def test_cold_read_without_backend_still_not_found(self, mod: Any) -> None:
        """红3：_memory_backend=None（降级）→ not found 且不抛异常。"""
        got = await mod.get_report("review-none")
        assert got == {"error": "review not found", "review_id": "review-none"}

    async def test_cold_read_search_failure_degrades_to_not_found(self, mod: Any) -> None:
        """检索异常（HindsightBackend.search 上抛 RuntimeError 形态）→ 退回
        not found 并告警，不崩 get_report。"""
        stub = _StubMemoryBackend()
        stub.search_error = RuntimeError("hindsight.recall 调用失败: sidecar down")
        mod.set_memory_backend(stub)

        got = await mod.get_report("review-err")
        assert got == {"error": "review not found", "review_id": "review-err"}
        assert "review-err" not in mod._reports


class TestGetReportPipelinePoll:
    async def test_get_report_finalizes_on_pipeline_completed(self, mod: Any) -> None:
        """复盘管道 completed → 报告落 completed（mode=pipeline，内容取 raw_result）。"""
        _inject_chat_capability(mod)
        r = await mod.trigger_review(task_id="task-9", summary="s")
        rid = r["review_id"]

        _inject_pipeline_state_capability(
            mod,
            rows=[{
                "pipeline_id": "pipe_review_gen_1",
                "status": "completed",
                "raw_result": "复盘结论：测试覆盖不足，建议补集成测试",
            }],
        )
        report = await mod.get_report(rid)
        assert report["status"] == "completed"
        assert report["mode"] == "pipeline"
        assert "复盘结论" in report["summary"]

    async def test_get_report_marks_failed_on_pipeline_failure(self, mod: Any) -> None:
        """复盘管道 failed → 报告落 failed（诚实状态，不伪造完成）。"""
        _inject_chat_capability(mod)
        r = await mod.trigger_review(task_id="task-9", summary="s")

        _inject_pipeline_state_capability(
            mod, rows=[{"pipeline_id": "pipe_review_gen_1", "status": "failed"}]
        )
        report = await mod.get_report(r["review_id"])
        assert report["status"] == "failed"

    async def test_get_report_keeps_running_while_pipeline_running(self, mod: Any) -> None:
        """复盘管道仍在跑 → 报告保持 running（get_report 可重复轮询）。"""
        _inject_chat_capability(mod)
        r = await mod.trigger_review(task_id="task-9", summary="s")

        _inject_pipeline_state_capability(
            mod, rows=[{"pipeline_id": "pipe_review_gen_1", "status": "running"}]
        )
        report = await mod.get_report(r["review_id"])
        assert report["status"] == "running"


# ═══════════════════════════════════════════════════════════
# 缺陷①（写入必炸）+ 缺陷②（冷读形态错）修复回归
# ── hindsight-client 0.9.x aretain metadata 是 dict[str,str] pydantic 校验，
#    tags list 塞 metadata → ValidationError → 报告从未真正持久化；
#    recall 返回抽取后事实（type=world/observation/experience），原文 JSON
#    永不命中，types=['memory'] 422（2026-08-19 批 C 真实 API 取证）。
# ═══════════════════════════════════════════════════════════


class TestStoreReportMetadataKey:
    async def test_store_report_passes_review_id_metadata(
        self, mod: Any, mock_backend: AsyncMock
    ) -> None:
        """红：store_report 把 review_id 传进 add 的 metadata（冷读定向键），
        与序列化后的 tags（add 装配处负责）共同构成可检索 wire metadata。"""
        mod.set_memory_backend(mock_backend)
        await mod.store_report("review-m1", {"task_id": "t", "lessons": ["l"]})

        kwargs = mock_backend.add.call_args.kwargs
        assert kwargs.get("metadata") == {"review_id": "review-m1"}


class TestColdReadViaDocuments:
    async def test_cold_read_uses_documents_original_text(self, mod: Any) -> None:
        """红：冷读走 get_documents 按 review_id tag 精确取回原文 original_text
        （不再依赖 recall 抽取事实形态——原文 JSON 永不命中）。"""
        stub = _StubMemoryBackend()
        stub.documents = [
            {
                "id": "doc-1",
                "original_text": json.dumps(
                    {
                        "review_id": "review-d1",
                        "status": "completed",
                        "lessons": ["l-d1"],
                        "task_id": "task-d1",
                    },
                    ensure_ascii=False,
                ),
                "document_metadata": {"review_id": "review-d1"},
                "tags": ["type:review", "review_id:review-d1"],
            }
        ]
        mod.set_memory_backend(stub)
        mod._reports.clear()  # 模拟重启丢内存

        got = await mod.get_report("review-d1")

        assert got.get("error") is None
        assert got["review_id"] == "review-d1"
        assert got["status"] == "completed"
        assert got["lessons"] == ["l-d1"]
        # 定向检索：review bank + review_id tag 精确匹配
        assert stub.get_documents_calls, "冷读应调用 backend.get_documents"
        last = stub.get_documents_calls[-1]
        assert last["user_id"] == "review"
        assert last["tags"] == ["review_id:review-d1"]
        assert last["tags_match"] == "any_strict"
        # 回填内存
        assert mod._reports["review-d1"]["status"] == "completed"

    async def test_cold_read_documents_rejects_mismatched_review_id(
        self, mod: Any
    ) -> None:
        """原文 review_id 不匹配（tag 误中/脏数据）→ 精确校验拒绝，不采纳。"""
        stub = _StubMemoryBackend()
        stub.documents = [
            {
                "id": "doc-x",
                "original_text": json.dumps(
                    {"review_id": "review-OTHER", "status": "completed"}
                ),
                "document_metadata": {"review_id": "review-OTHER"},
                "tags": ["review_id:review-OTHER"],
            }
        ]
        mod.set_memory_backend(stub)

        got = await mod.get_report("review-d2")
        assert got == {"error": "review not found", "review_id": "review-d2"}
        assert "review-d2" not in mod._reports

    async def test_cold_read_documents_error_falls_back_gracefully(
        self, mod: Any
    ) -> None:
        """get_documents 抛错（后端不可用）→ 告警降级 not found，不崩。"""
        stub = _StubMemoryBackend()
        stub.get_documents_error = RuntimeError("hindsight.get_documents 调用失败: down")
        mod.set_memory_backend(stub)

        got = await mod.get_report("review-d3")
        assert got == {"error": "review not found", "review_id": "review-d3"}

    async def test_cold_read_legacy_search_still_works_without_documents(
        self, mod: Any
    ) -> None:
        """后端无 get_documents（旧形态/仅 search）→ 回落既有 search 冷读路径
        （语义不变，兼容非 hindsight 后端）。"""

        class _SearchOnlyBackend(_StubMemoryBackend):
            get_documents = None  # type: ignore[assignment]

        stub = _SearchOnlyBackend()
        mod.set_memory_backend(stub)
        await mod.store_report("review-legacy", {"task_id": "t", "lessons": ["l-l"]})
        mod._reports.clear()

        got = await mod.get_report("review-legacy")
        assert got["review_id"] == "review-legacy"
        assert got["lessons"] == ["l-l"]
