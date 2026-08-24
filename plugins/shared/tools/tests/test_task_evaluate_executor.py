# @feature: FP-0.2.〇 评估管道装配 | @vision: V2 自进化闭环 | @ci: python-coverage
"""PipelineEvaluationExecutor（_executor.py，批次B 2026-08-24）单测。

覆盖（方案 docs/working/管道工作区关联与评估管道装配方案_20260824.md 批次B）：
1. tool 型指标本地执行——file_check 四种 check + workspace 相对路径解析；
2. 指标未定义/配置缺文件 → 诚实失败（不猜测语义）；
3. agent 型指标派发评估子管道——R2 契约断言（workspace/ws_meta 出生继承、
   evaluation.of_task/metric_id 登记、task_reminder 评估模式防递归、lineage
   有父、evaluator_agent、background）；
4. 结论回收——evaluation.detected_result（passed/score/feedback/pipeline_run_id）
   / 失败终态 / 派发失败 / 派发无 pipeline_id；
5. server.py on_load 装配——set_default_executor 注入生产执行器。

外部依赖零真实内核：chat_send/state_rows 全注入替身；指标配置用 tmp yaml。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_TESTS_DIR = Path(__file__).resolve().parent
_TE_DIR = _TESTS_DIR.parent / "task_evaluate"
_SYSTEM_ROOT = _TESTS_DIR.parents[2] / "system"

_PLUGIN_PATHS = tuple(str(_d) for _d in [_TE_DIR, _SYSTEM_ROOT, _SYSTEM_ROOT / "tasks"])
for _d in _PLUGIN_PATHS:
    if _d not in sys.path:
        sys.path.insert(0, _d)


@pytest.fixture(autouse=True)
def _ensure_plugin_paths():
    """平铺串扰自持：每用例前重插目录（对齐 test_task_evaluate_migration）。"""
    for _d in _PLUGIN_PATHS:
        if _d not in sys.path:
            sys.path.insert(0, _d)


def _load_executor_mod() -> Any:
    mod_name = "task_evaluate_executor_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TE_DIR / "_executor.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_METRICS_YAML = """
metrics:
  - name: file_check
    evaluator_type: tool
  - name: semantic_check
    evaluator_type: agent
"""


@pytest.fixture
def metrics_path(tmp_path: Path) -> str:
    p = tmp_path / "evaluation_metrics.yaml"
    p.write_text(_METRICS_YAML, encoding="utf-8")
    return str(p)


@pytest.fixture
def mod_exec() -> Any:
    m = _load_executor_mod()
    # 测试时限压回收轮询（默认 600s 生产上限会让无结论用例空转十分钟）
    m._POLL_INTERVAL_S = 0.01
    m._AGENT_RECOVER_TIMEOUT_S = 0.2
    return m


def _make_executor(
    mod: Any,
    chat_send: Any = None,
    state_rows: Any = None,
    metrics_path: str | None = None,
) -> Any:
    return mod.PipelineEvaluationExecutor(
        chat_send=chat_send or AsyncMock(return_value={"pipeline_id": "evalPipe1"}),
        state_rows=state_rows or (lambda: []),
        metrics_config_path=metrics_path,
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── tool 型指标：file_check 本地执行 ─────────────────────────


class TestToolMetricLocal:
    def test_exists_absolute(self, mod_exec: Any, metrics_path: str, tmp_path: Path) -> None:
        f = tmp_path / "out.txt"
        f.write_text("hi", encoding="utf-8")
        ex = _make_executor(mod_exec, metrics_path=metrics_path)
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": str(f)}}))
        assert r.results[0].passed is True

    def test_exists_missing_file_fails(self, mod_exec: Any, metrics_path: str) -> None:
        ex = _make_executor(mod_exec, metrics_path=metrics_path)
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": "D:/no/such"}}))
        assert r.results[0].passed is False
        assert "不存在" in r.results[0].message

    def test_relative_path_resolved_against_workspace(
        self, mod_exec: Any, metrics_path: str, tmp_path: Path
    ) -> None:
        (tmp_path / "sub").mkdir()
        ex = _make_executor(mod_exec, metrics_path=metrics_path)
        r = _run(
            ex.run_evaluation(
                "t1",
                ["file_check"],
                {"file_check": {"path": "sub", "check": "is_directory", "workspace": str(tmp_path)}},
            )
        )
        assert r.results[0].passed is True

    def test_not_empty_and_contains(
        self, mod_exec: Any, metrics_path: str, tmp_path: Path
    ) -> None:
        f = tmp_path / "log.txt"
        f.write_text("hello world", encoding="utf-8")
        ex = _make_executor(mod_exec, metrics_path=metrics_path)
        r = _run(
            ex.run_evaluation(
                "t1",
                ["file_check"],
                {"file_check": {"path": str(f), "check": "not_empty"}},
            )
        )
        r2 = _run(
            ex.run_evaluation(
                "t1",
                ["file_check"],
                {"file_check": {"path": str(f), "check": "contains", "pattern": "world"}},
            )
        )
        assert r.results[0].passed is True
        assert r2.results[0].passed is True

    def test_unknown_check_type_fails(self, mod_exec: Any, metrics_path: str, tmp_path: Path) -> None:
        ex = _make_executor(mod_exec, metrics_path=metrics_path)
        r = _run(
            ex.run_evaluation(
                "t1",
                ["file_check"],
                {"file_check": {"path": str(tmp_path), "check": "magic"}},
            )
        )
        assert r.results[0].passed is False
        assert "未知检查类型" in (r.results[0].error or "")


# ── 指标定义面：未定义/缺配置 → 诚实失败 ─────────────────────


class TestMetricDefinition:
    def test_undefined_metric_fails_honestly(self, mod_exec: Any, metrics_path: str) -> None:
        ex = _make_executor(mod_exec, metrics_path=metrics_path)
        r = _run(ex.run_evaluation("t1", ["no_such_metric"], {}))
        assert r.results[0].passed is False
        assert "未在 evaluation_metrics.yaml 定义" in (r.results[0].error or "")

    def test_missing_config_file_degrades_to_undefined(
        self, mod_exec: Any, tmp_path: Path
    ) -> None:
        ex = _make_executor(mod_exec, metrics_path=str(tmp_path / "gone.yaml"))
        r = _run(ex.run_evaluation("t1", ["file_check"], {"file_check": {"path": "x"}}))
        assert r.results[0].passed is False
        assert "未在 evaluation_metrics.yaml 定义" in (r.results[0].error or "")


# ── agent 型指标：评估子管道派发（R2 契约）+ 回收 ─────────────


class TestAgentMetricPipeline:
    def _task_rows(self, ws_path: str) -> list[dict[str, Any]]:
        return [
            {
                "pipeline_id": "taskP1",
                "ws_meta": {"path": ws_path, "mode": "worktree", "project_root": "D:/proj"},
                "lineage.origin_session_id": "sessRoot1",
            }
        ]

    def test_dispatch_contract_r2_workspace_inheritance(
        self, mod_exec: Any, metrics_path: str, tmp_path: Path
    ) -> None:
        """派发契约：workspace/ws_meta 出生继承（同目录跑）+ 登记键 + 防递归
        + lineage 有父 + evaluator_agent + background。"""
        ws = tmp_path / "ws-task"
        ws.mkdir()
        sent: list[dict[str, Any]] = []

        async def fake_chat(params: dict[str, Any]) -> dict[str, Any]:
            sent.append(params)
            return {"pipeline_id": "evalPipe1"}

        ex = _make_executor(
            mod_exec,
            chat_send=fake_chat,
            state_rows=(lambda: self._task_rows(str(ws))),
            metrics_path=metrics_path,
        )
        r = _run(
            ex.run_evaluation(
                "taskP1",
                ["semantic_check"],
                {"semantic_check": {"criteria": "结构完整", "workspace": str(ws)}},
            )
        )
        assert len(sent) == 1
        p = sent[0]
        assert p["create"] is True and p["background"] is True
        assert p["agent_id"] == "evaluator_agent"
        assert p["lineage"] == {"parent_pipeline_id": "taskP1", "origin_session_id": "sessRoot1"}
        st = p["state"]
        # R2：出生即带被评估任务工作区坐标（幂等跳过 workspace_lifecycle）
        assert st["workspace"] == str(ws)
        assert st["ws_meta"]["path"] == str(ws)
        assert st["ws_meta"]["project_root"] == "D:/proj"
        # 登记键（ADR 2026-08-24-eval-pipeline-state-keys）
        assert st["evaluation.of_task"] == "taskP1"
        assert st["evaluation.metric_id"] == "semantic_check"
        # 防递归：评估者模式
        assert st["plugin_configs"]["task_reminder"]["evaluation_mode"] is True
        # 回收超时不阻塞断言（detected_result 缺席 → 指标失败但派发契约已验证）
        assert r.results[0].pipeline_run_id == "evalPipe1"

    def test_recover_detected_result(
        self, mod_exec: Any, metrics_path: str, tmp_path: Path
    ) -> None:
        """回收：evaluation.detected_result → passed/score/feedback/pipeline_run_id。"""
        ws = tmp_path / "ws-task"
        ws.mkdir()
        mod_exec._POLL_INTERVAL_S = 0.01
        rows: list[dict[str, Any]] = self._task_rows(str(ws))
        # 首轮无结论，次轮评估管道行带 detected_result
        holder: dict[str, list[dict[str, Any]]] = {"rows": rows}
        eval_row: dict[str, Any] = {"pipeline_id": "evalPipe1"}

        async def state_rows() -> list[dict[str, Any]]:
            out = list(holder["rows"])
            if eval_row.get("evaluation.detected_result") is not None:
                out.append(dict(eval_row))
            return out

        async def fake_chat(params: dict[str, Any]) -> dict[str, Any]:
            async def _later() -> None:
                await asyncio.sleep(0.02)
                eval_row["evaluation.detected_result"] = {
                    "passed": True,
                    "score": 88,
                    "feedback": "结构完整",
                }

            asyncio.get_running_loop().create_task(_later())
            return {"pipeline_id": "evalPipe1"}

        ex = _make_executor(mod_exec, chat_send=fake_chat, state_rows=state_rows, metrics_path=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        m = r.results[0]
        assert m.passed is True
        assert m.score == 88.0
        assert m.message == "结构完整"
        assert m.pipeline_run_id == "evalPipe1"
        assert m.evaluator_output["passed"] is True

    def test_recover_failed_terminal(
        self, mod_exec: Any, metrics_path: str, tmp_path: Path
    ) -> None:
        """回收：评估子管道 failed 终态 → 指标诚实失败。"""
        mod_exec._POLL_INTERVAL_S = 0.01
        ws = tmp_path / "ws-task"
        ws.mkdir()

        async def state_rows() -> list[dict[str, Any]]:
            return [
                *self._task_rows(str(ws)),
                {"pipeline_id": "evalPipe1", "task.status": "failed"},
            ]

        ex = _make_executor(
            mod_exec,
            chat_send=AsyncMock(return_value={"pipeline_id": "evalPipe1"}),
            state_rows=state_rows,
            metrics_path=metrics_path,
        )
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "failed" in (r.results[0].error or "")

    def test_dispatch_failure_fails_metric(
        self, mod_exec: Any, metrics_path: str, tmp_path: Path
    ) -> None:
        ws = tmp_path / "ws-task"
        ws.mkdir()

        async def boom(_params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("chat down")

        ex = _make_executor(mod_exec, chat_send=boom, state_rows=(lambda: self._task_rows(str(ws))), metrics_path=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "派发失败" in (r.results[0].error or "")

    def test_task_without_workspace_fails_r2_guard(
        self, mod_exec: Any, metrics_path: str
    ) -> None:
        """R2 守卫：被评估任务无工作区坐标 → 诚实失败（不另起炉灶）。"""
        ex = _make_executor(mod_exec, state_rows=(lambda: [{"pipeline_id": "taskP1"}]), metrics_path=metrics_path)
        r = _run(ex.run_evaluation("taskP1", ["semantic_check"], {}))
        assert r.results[0].passed is False
        assert "无工作区坐标" in (r.results[0].error or "")


# ── server 装配：set_default_executor 注入 ───────────────────


class TestServerAssembly:
    def test_on_load_injects_default_executor(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "task_evaluate_server_assembly_test", _TE_DIR / "server.py"
        )
        assert spec is not None and spec.loader is not None
        server_mod = importlib.util.module_from_spec(spec)
        sys.modules["task_evaluate_server_assembly_test"] = server_mod
        try:
            spec.loader.exec_module(server_mod)
            import tool as tool_mod  # noqa: PLC0415 — _TE_DIR 已在 sys.path

            before = getattr(tool_mod, "_default_executor", None)
            asyncio.run(server_mod._on_load({}))
            after = tool_mod._default_executor
            assert after is not None, "on_load 应装配默认评估执行器"
            if before is None:
                # 清理：不毒化其它用例（还原为 None）
                tool_mod.set_default_executor(None)
        finally:
            sys.modules.pop("task_evaluate_server_assembly_test", None)
