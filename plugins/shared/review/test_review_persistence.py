# @feature: FP-0.2.六 记忆检索 | @vision: V1 可进化 | @ci: python-coverage
"""复盘报告 F9 单通道存储契约测试：文件面本体 + state 索引 + 抽取事实。

收敛后形态（用户裁定 2026-10-08：大对象走文件面，state 只存索引）：

- 报告本体 = 被复盘任务 ws 内 ``docs/working/reviews/{review_id}.json``
  （真实文件系统落盘，禁 mock 文件面）；
- state 索引 = 被复盘任务行 ``task.review.{rid}.{status,pipeline_id,summary,
  created_at,path}``（经 pipeline-state.update，测试以 fake 内核 state 注册表
  承接——内核能力是外部依赖）；
- get_report = 索引定位 → 文件面直读，无内存副本、无记忆库回退；
- Hindsight 只留面向召回的抽取事实（非报告全文 JSON）。

覆盖：
1. 派发出生即写索引（running 一次写全）；
2. 管道完成 → 终结联动（本体落盘 + 索引补 path + raw_result 成 summary），
   重启后（索引仍在、管道行可逝）读面照常——单通道不依赖内存；
3. 管道失败 → failed 诚实态，无本体文件；
4. 错误语义三分：state 读面不可用 / review 不存在 / 产物缺失；
5. 降级报告同样落位（文件 + 索引）；
6. 抽取事实写入与失败降级。

[来源: docs/working/state单一真相源审查_20261008.md F9]
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agentos_plugin_sdk.capability import CapabilityHandle

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 review/server.py 模块（每次新建，隔离模块级全局态）。"""
    mod_name = "review_server_f9_test"
    plugin_path = _PLUGIN_DIR / "server.py"
    assert plugin_path.exists(), f"server.py missing at {plugin_path}"
    spec = importlib.util.spec_from_file_location(mod_name, plugin_path)
    assert spec is not None and spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


class _FakeKernelState:
    """pipeline-state 能力替身：内存 state 注册表（list/update 真实语义）。

    update 只收 task.* 键（与内核协议一致）；list 组装扁平行（pipeline_id +
    字段）。行可显式摘除（模拟管道行逝去，验证读面单通道不依赖管道行）。
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def set_task_row(self, task_id: str, ws_root: str) -> None:
        self.rows[task_id] = {
            "task.status": "running",
            "task.ws_meta": {"mode": "isolated", "path": ws_root},
        }

    def drop_row(self, pipeline_id: str) -> None:
        self.rows.pop(pipeline_id, None)

    async def call(
        self, method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        if method == "list":
            return [
                dict({"pipeline_id": pid}, **fields)
                for pid, fields in self.rows.items()
            ]
        if method == "update":
            for k in params["fields"]:
                assert k.startswith("task."), f"非 task.* 键: {k}"
            self.rows.setdefault(params["pipeline_id"], {}).update(params["fields"])
            return {"status": "updated"}
        raise AssertionError(f"unexpected method: {method}")


class _StubMemoryBackend:
    """IMemoryBackend 替身：记录 add 调用，可注错。"""

    def __init__(self) -> None:
        self.add_calls: list[dict[str, Any]] = []
        self.add_error: Exception | None = None

    async def add(
        self,
        user_id: str,
        content: str,
        memory_type: str = "semantic",
        tags: list[str] | None = None,
        source: str = "",
        metadata: dict[str, str] | None = None,
    ) -> str:
        if self.add_error is not None:
            raise self.add_error
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
        return f"mem-{len(self.add_calls)}"


@pytest.fixture
def state(tmp_path: Path) -> _FakeKernelState:
    return _FakeKernelState()


@pytest.fixture
def mod(tmp_path: Path, state: _FakeKernelState) -> Any:
    """加载 server 模块并注入 fake pipeline-state 能力。"""
    module = _load_module()
    module._memory_backend = None

    handle = CapabilityHandle("pipeline-state", call_fn=state.call)
    module.plugin._capabilities["pipeline-state"] = handle
    return module


def _inject_chat(
    mod: Any, *, pipeline_id: str = "pipe_review_gen_1", absent: bool = False
) -> list[dict[str, Any]]:
    """注入 fake chat 能力（absent=True 时摘除，模拟降级）。"""
    calls: list[dict[str, Any]] = []

    async def call_fn(
        method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        calls.append(params)
        return {"pipeline_id": pipeline_id}

    if absent:
        mod.plugin._capabilities.pop("chat", None)
    else:
        mod.plugin._capabilities["chat"] = CapabilityHandle("chat", call_fn=call_fn)
    return calls


def _complete_pipeline(state: _FakeKernelState, pipeline_id: str, raw_result: str) -> None:
    """把复盘管道行置为已完成（引擎出口形态：status + raw_result）。"""
    state.rows[pipeline_id] = {"status": "completed", "raw_result": raw_result}


def _report_file(tmp_path: Path, ws_name: str, review_id: str) -> Path:
    return (
        tmp_path / ws_name / "docs" / "working" / "reviews" / f"{review_id}.json"
    )


class TestDispatchWritesBirthIndex:
    async def test_trigger_running_writes_full_birth_index(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """派发成功 → 锚行（任务行）出生索引一次写全（running/pipeline_id/summary/created_at）。"""
        ws = tmp_path / "ws_task-9"
        state.set_task_row("task-9", str(ws))
        _inject_chat(mod)

        r = await mod.review_ops(action="trigger", task_id="task-9", summary="复盘周报任务")

        assert r["status"] == "running"
        rid = r["review_id"]
        anchor = state.rows["task-9"]
        assert anchor[f"task.review.{rid}.status"] == "running"
        assert anchor[f"task.review.{rid}.pipeline_id"] == "pipe_review_gen_1"
        assert anchor[f"task.review.{rid}.summary"] == "复盘周报任务"
        assert isinstance(anchor[f"task.review.{rid}.created_at"], float)

    async def test_trigger_without_task_leaves_no_index(self, mod: Any, state: _FakeKernelState) -> None:
        """无锚行（task_id 空）→ 索引无处可写（诚实降级：该复盘读面不可见）。"""
        _inject_chat(mod)
        r = await mod.review_ops(action="trigger", task_id="", summary="系统复盘")
        assert r["status"] == "running"
        assert not any(k.startswith("task.review.") for row in state.rows.values() for k in row)


class TestRoundTripRealFilePlane:
    """store（产出落位）→ get_report 往返：索引 + 文件面真实落盘。"""

    async def _run_to_completion(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path,
        task_id: str, ws_name: str, pipeline_id: str, raw_result: str,
    ) -> tuple[str, Path]:
        state.set_task_row(task_id, str(tmp_path / ws_name))
        _inject_chat(mod, pipeline_id=pipeline_id)
        r = await mod.review_ops(action="trigger", task_id=task_id, summary=f"{task_id} 复盘")
        rid = r["review_id"]
        _complete_pipeline(state, pipeline_id, raw_result)
        return rid, _report_file(tmp_path, ws_name, rid)

    async def test_completed_pipeline_finalizes_to_file_and_index(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        rid, file_path = await self._run_to_completion(
            mod, state, tmp_path,
            task_id="task-a", ws_name="ws_a", pipeline_id="pipe_a",
            raw_result="复盘结论：测试覆盖不足，建议补集成测试",
        )
        got = await mod.review_ops(action="get_report", review_id=rid)

        assert got.get("error") is None, got
        assert got["status"] == "completed"
        assert got["mode"] == "pipeline"
        assert got["task_id"] == "task-a"
        assert got["summary"] == "复盘结论：测试覆盖不足，建议补集成测试"
        # 文件面真实落盘：本体 JSON 在 ws 内（坐标 = 索引 path）
        assert file_path.is_file(), f"报告本体未落盘: {file_path}"
        body = json.loads(file_path.read_text(encoding="utf-8"))
        assert body["review_id"] == rid and body["status"] == "completed"
        # 索引补 path（ws 相对坐标）
        anchor = state.rows["task-a"]
        assert anchor[f"task.review.{rid}.path"] == f"docs/working/reviews/{rid}.json"

    async def test_second_review_round_trips_independently(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """≥2 组输入：另一任务/管道同一契约独立成立（坐标互不串扰）。"""
        rid_b, file_b = await self._run_to_completion(
            mod, state, tmp_path,
            task_id="task-b", ws_name="ws_b", pipeline_id="pipe_b",
            raw_result="B 任务复盘结论",
        )
        got = await mod.review_ops(action="get_report", review_id=rid_b)
        assert got["task_id"] == "task-b"
        assert got["summary"] == "B 任务复盘结论"
        assert json.loads(file_b.read_text(encoding="utf-8"))["task_id"] == "task-b"

    async def test_read_survives_pipeline_row_loss(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """终态化后再读：管道行逝去（冷清理模拟）→ 索引+文件面单通道照常取回。"""
        rid, file_path = await self._run_to_completion(
            mod, state, tmp_path,
            task_id="task-c", ws_name="ws_c", pipeline_id="pipe_c",
            raw_result="C 结论",
        )
        assert (await mod.review_ops(action="get_report", review_id=rid))["status"] == "completed"

        state.drop_row("pipe_c")
        got = await mod.review_ops(action="get_report", review_id=rid)
        assert got.get("error") is None, got
        assert got["status"] == "completed"
        assert got["summary"] == "C 结论"
        assert file_path.is_file()


class TestFinalizeLinkage:
    async def test_failed_pipeline_marks_failed_without_file(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """管道失败 → 索引落 failed（诚实态，不伪造完成），无本体文件。"""
        ws = tmp_path / "ws_task-f"
        state.set_task_row("task-f", str(ws))
        _inject_chat(mod, pipeline_id="pipe_f")
        r = await mod.review_ops(action="trigger", task_id="task-f", summary="s")
        rid = r["review_id"]
        state.rows["pipe_f"] = {"status": "failed"}

        got = await mod.review_ops(action="get_report", review_id=rid)

        assert got["status"] == "failed"
        assert got["task_id"] == "task-f"
        assert state.rows["task-f"][f"task.review.{rid}.status"] == "failed"
        assert not list(ws.rglob("*.json")), "失败复盘不产报告文件"

    async def test_running_pipeline_stays_running_and_repeatable(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """管道进行中 → 保持 running（可重复轮询）；行缺失同样保持 running。"""
        ws = tmp_path / "ws_task-r"
        state.set_task_row("task-r", str(ws))
        _inject_chat(mod, pipeline_id="pipe_r")
        rid = (await mod.review_ops(action="trigger", task_id="task-r", summary="s"))["review_id"]

        state.rows["pipe_r"] = {"status": "running"}
        assert (await mod.review_ops(action="get_report", review_id=rid))["status"] == "running"
        state.drop_row("pipe_r")
        assert (await mod.review_ops(action="get_report", review_id=rid))["status"] == "running"


class TestErrorSemantics:
    async def test_unknown_review_not_found(self, mod: Any) -> None:
        got = await mod.review_ops(action="get_report", review_id="review_absent")
        assert got == {
            "success": False, "error": "review not found", "review_id": "review_absent",
        }

    async def test_state_plane_unavailable_distinct_from_not_found(
        self, mod: Any
    ) -> None:
        """state 读面不可用 ≠ 复盘不存在（索引在 state，读面失明单独表态）。"""
        mod.plugin._capabilities.pop("pipeline-state", None)
        got = await mod.review_ops(action="get_report", review_id="review_x1")
        assert got["success"] is False
        assert got["error"] == "pipeline state unavailable"

    async def test_completed_but_artifact_missing_errors(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """索引 completed 而本体缺失（文件被清/无 ws 坐标）→ 显式产物缺失错误。"""
        ws = tmp_path / "ws_task-m"
        state.set_task_row("task-m", str(ws))
        _inject_chat(mod, pipeline_id="pipe_m")
        rid = (await mod.review_ops(action="trigger", task_id="task-m", summary="s"))["review_id"]
        _complete_pipeline(state, "pipe_m", "结论")
        assert (await mod.review_ops(action="get_report", review_id=rid))["status"] == "completed"

        # 本体被清（文件面是本体唯一真相——索引仍在也不能凭空复原）
        _report_file(tmp_path, "ws_task-m", rid).unlink()
        got = await mod.review_ops(action="get_report", review_id=rid)
        assert got["success"] is False
        assert got["error"] == "review report artifact missing"
        assert got["report_path"] == f"docs/working/reviews/{rid}.json"

    async def test_traversal_review_id_rejected(self, mod: Any) -> None:
        """路径越界 review_id → not found（不触文件面，fail-closed）。"""
        got = await mod.review_ops(action="get_report", review_id="../../etc/passwd")
        assert got["success"] is False
        assert got["error"] == "review not found"


class TestDegradedPersistsArtifact:
    async def test_degraded_report_persists_file_and_index(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """chat 缺席降级 → 报告在触发当轮落位（文件 + 索引），get_report 可读。"""
        ws = tmp_path / "ws_task-d"
        state.set_task_row("task-d", str(ws))
        _inject_chat(mod, absent=True)

        r = await mod.review_ops(action="trigger", task_id="task-d", summary="降级复盘")

        assert r["status"] == "degraded"
        rid = r["review_id"]
        files = list(ws.rglob("*.json"))
        assert len(files) == 1, f"降级报告应落盘一份: {files}"
        got = await mod.review_ops(action="get_report", review_id=rid)
        assert got.get("error") is None, got
        assert got["status"] == "degraded"
        assert got["mode"] == "local_degrade"
        assert got["review_id"] == rid

    async def test_degraded_without_task_warns_and_skips_persistence(
        self, mod: Any, state: _FakeKernelState
    ) -> None:
        """无锚行降级 → 响应照常 degraded，持久化跳过（日志留痕）。"""
        _inject_chat(mod, absent=True)
        r = await mod.review_ops(action="trigger", task_id="", summary="s")
        assert r["status"] == "degraded"
        assert not any(
            k.startswith("task.review.") for row in state.rows.values() for k in row
        )

    async def test_dispatch_failure_degrades(self, mod: Any) -> None:
        """派发调用本身失败（内核错误）→ degraded 兜底（不产 running 假象）。"""

        async def boom(
            method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            raise RuntimeError("kernel down")

        mod.plugin._capabilities["chat"] = CapabilityHandle("chat", call_fn=boom)
        r = await mod.review_ops(action="trigger", task_id="task-g9", summary="s")
        assert r["status"] == "degraded"
        assert r["mode"] == "local_degrade"

    async def test_degraded_with_unwritable_ws_still_responds(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """降级路径落盘失败（ws 根是文件）→ 响应照常 degraded，索引 path 空。"""
        blocker = tmp_path / "blocked_ws"
        blocker.write_text("x", encoding="utf-8")
        state.set_task_row("task-blk2", str(blocker))
        _inject_chat(mod, absent=True)

        r = await mod.review_ops(action="trigger", task_id="task-blk2", summary="s")

        assert r["status"] == "degraded"
        anchor = state.rows["task-blk2"]
        rid = r["review_id"]
        assert anchor[f"task.review.{rid}.status"] == "completed"
        assert anchor[f"task.review.{rid}.path"] == ""


class TestArtifactPlacementDegradation:
    """落位受阻（无 ws 坐标 / 落盘失败）→ 终态诚实、读面显式产物缺失。"""

    async def test_anchor_without_ws_finalizes_with_empty_path(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """锚行无 task.ws_meta → completed 落位但 path 为空；复读报产物缺失。"""
        state.rows["task-nw"] = {"task.status": "running"}  # 无 ws 坐标
        _inject_chat(mod, pipeline_id="pipe_nw")
        rid = (await mod.review_ops(action="trigger", task_id="task-nw", summary="s"))["review_id"]
        _complete_pipeline(state, "pipe_nw", "无锚点结论")

        got = await mod.review_ops(action="get_report", review_id=rid)

        assert got["status"] == "completed"
        assert got["summary"] == "无锚点结论"
        assert state.rows["task-nw"][f"task.review.{rid}.path"] == ""
        # 复读：completed 而无本体 → 显式产物缺失（不伪造内容）
        again = await mod.review_ops(action="get_report", review_id=rid)
        assert again["success"] is False
        assert again["error"] == "review report artifact missing"

    async def test_unwritable_ws_finalizes_with_artifact_missing(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """ws 根是文件（落盘必败）→ 终态 completed + path 空，读面产物缺失。"""
        blocker = tmp_path / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        state.set_task_row("task-blk", str(blocker))
        _inject_chat(mod, pipeline_id="pipe_blk")
        rid = (await mod.review_ops(action="trigger", task_id="task-blk", summary="s"))["review_id"]
        _complete_pipeline(state, "pipe_blk", "结论")

        got = await mod.review_ops(action="get_report", review_id=rid)

        assert got["status"] == "completed"
        assert state.rows["task-blk"][f"task.review.{rid}.path"] == ""


class TestStateRowsShapeGuard:
    async def test_non_list_rows_treated_as_unavailable(self, mod: Any) -> None:
        """能力返回非 list → 读面不可用（与空列表区分，不误判 not found）。"""

        async def call_fn(
            method: str, params: dict[str, Any], timeout: float | None = None
        ) -> Any:
            return "not-a-list"

        mod.plugin._capabilities["pipeline-state"] = CapabilityHandle(
            "pipeline-state", call_fn=call_fn
        )
        got = await mod.review_ops(action="get_report", review_id="review_any")
        assert got["error"] == "pipeline state unavailable"


class TestReportStorePlane:
    """文件面单元契约：坐标白名单防穿越 + 坏本体读面 None 语义。"""

    def test_rel_path_whitelist(self) -> None:
        from report_store import report_rel_path

        assert report_rel_path("review_ab-12:3") == "docs/working/reviews/review_ab-12:3.json"
        with pytest.raises(ValueError):
            report_rel_path("../../etc/passwd")
        with pytest.raises(ValueError):
            report_rel_path("")

    def test_read_report_rejects_traversal_id(self, tmp_path: Path) -> None:
        from report_store import read_report

        assert read_report(str(tmp_path), "../../x") is None

    def test_read_report_corrupt_or_non_dict_returns_none(self, tmp_path: Path) -> None:
        from report_store import read_report

        ws = tmp_path / "ws"
        target = ws / "docs" / "working" / "reviews"
        target.mkdir(parents=True)
        (target / "review_bad.json").write_text("{not-json", encoding="utf-8")
        (target / "review_arr.json").write_text("[1, 2]", encoding="utf-8")

        assert read_report(str(ws), "review_bad") is None
        assert read_report(str(ws), "review_arr") is None
        assert read_report(str(ws), "review_absent") is None


class TestRecallFactsOnly:
    async def test_store_report_writes_fact_not_full_json(
        self, mod: Any
    ) -> None:
        """store_report = 抽取事实写入：非全文 JSON，含定向 tags/metadata。"""
        stub = _StubMemoryBackend()
        mod.set_memory_backend(stub)

        await mod.store_report(
            "review-fact-1",
            {
                "task_id": "task-t",
                "summary": "总结陈词",
                "lessons": ["教训一", "教训二"],
                "recommendations": ["建议一"],
            },
        )

        assert len(stub.add_calls) == 1
        kwargs = stub.add_calls[0]
        assert kwargs["user_id"] == "review"
        assert kwargs["memory_type"] == "review"
        assert kwargs["tags"] == ["review_id:review-fact-1", "review_report"]
        assert kwargs["metadata"] == {"review_id": "review-fact-1"}
        content = kwargs["content"]
        # 事实形态：可读文本线索（含定向 id 与要点），不是报告全文 JSON
        assert "review-fact-1" in content and "总结陈词" in content
        with pytest.raises(json.JSONDecodeError):
            json.loads(content)

    async def test_finalize_emits_recall_fact(
        self, mod: Any, state: _FakeKernelState, tmp_path: Path
    ) -> None:
        """管道完成终态化时顺带写抽取事实（报告产出点单一入口）。"""
        stub = _StubMemoryBackend()
        mod.set_memory_backend(stub)
        state.set_task_row("task-e", str(tmp_path / "ws_e"))
        _inject_chat(mod, pipeline_id="pipe_e")
        rid = (await mod.review_ops(action="trigger", task_id="task-e", summary="s"))["review_id"]
        _complete_pipeline(state, "pipe_e", "完成结论")

        await mod.review_ops(action="get_report", review_id=rid)

        assert len(stub.add_calls) == 1
        assert "完成结论" in stub.add_calls[0]["content"]

    async def test_backend_failure_does_not_raise(self, mod: Any) -> None:
        """记忆后端故障 → 告警降级不抛（召回增强非真相面）。"""
        stub = _StubMemoryBackend()
        stub.add_error = RuntimeError("hindsight down")
        mod.set_memory_backend(stub)
        await mod.store_report("review-f2", {"task_id": "t", "summary": "s"})

    async def test_backend_absent_skips_silently(self, mod: Any) -> None:
        """未注入后端 → 跳过记忆写入，文件面/索引不受影响。"""
        await mod.store_report("review-f3", {"task_id": "t", "summary": "s"})
