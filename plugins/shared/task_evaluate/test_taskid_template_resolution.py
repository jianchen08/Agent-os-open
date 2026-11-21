# @feature: FP-0.2.〇 管道引擎与插件执行模型(内核地基) | @vision: V3 可嵌入 | @ci: python-coverage
"""task_evaluate 验收路径 {{task_id}} 模板替换（评估按真实 ID 实查）单测。

设计契约（AGENTS.md 载明）：派发时 task_id 未出生，任务书验收路径写模板
``{{task_id}}_<后缀>``（提交期双花括号白名单放行）；评估期 task_evaluate
自动替换 ``{{task_id}}`` 为真实 ID 后再实查，并在结果里回显实查路径。

实测 bug（本仓多起，任务 1a42e85aaa47 等）：指标参数值面的 {{task_id}}
有替换（tool.py _inject_workspace_and_placeholders），但完赛证据闸的
acceptance files / required_files 清单取自 AC 实例键后缀与 metadata 字面
清单，按**字面**路径实查 → 查不到 → agent 被迫落一份字面名文件。

覆盖面：
1. 豁免轨展开形态（file_check::{{task_id}}_x.md 实例键）+ 真实 ID 文件在
   → completed（修复前按字面实查必拒）；
2. 豁免轨字面 files 键形态（原始白话形态，多项含子目录）→ completed
   （第二组输入，防单点拟合）；
3. 诚实失败：真实 ID 文件缺失、工作空间只有**字面**名文件 → 拒绝
   （证明替换不是"哪个存在查哪个"的猜测，字面兜底文件不再救得了验收）；
4. 回执回显实查路径：拒绝回执含替换后路径、不含 {{task_id}} 字面字样；
5. 既有轨 required_files 同刀：metadata.required_files 含 {{task_id}}
   → 同样替换后实查；
6. 混合形态区分度：{{task_id}} 路径齐备 + 普通路径缺失 → 回执只列普通
   路径（替换路径不误列）；
7. 占位符替换纯函数：只替换 {{task_id}}，其他 {{...}}/{{state:...}} 形态
   原样保留（goal/描述白名单放行行为的评估面等价锚——本函数不消费 goal）。

任务领域走真实 TaskModel + 真实 TaskService（tmp 存储目录）；文件证据用
临时目录真实文件 + os.utime 精确控制 mtime；外部依赖仅 state 读/写面与
worktree 合并机制（注入替身，同 test_acceptance_files_evidence.py 口径）。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit

_TE_DIR = Path(__file__).resolve().parent
_TASKS_DIR = _TE_DIR.parents[0] / "tasks"
_SHARED_DIR = _TE_DIR.parents[0]

for _d in (_TE_DIR, _TASKS_DIR, _SHARED_DIR):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))


def _load_module() -> Any:
    mod_name = "task_eval_taskid_tpl_under_test"
    if mod_name in sys.modules:
        return sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, _TE_DIR / "tool.py")
    assert spec is not None
    assert spec.loader is not None, "cannot load tool.py"
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)  # 先执行后注册：导入失败即清缓存重抛，不留半初始化模块（防误导性 AttributeError 伪装导入失败）
    except BaseException:
        sys.modules.pop(mod_name, None)
        raise
    sys.modules[mod_name] = module
    return module


@pytest.fixture
def mod() -> Any:
    return _load_module()


@pytest.fixture
def service(tmp_path: Path) -> Any:
    """真实 TaskService（临时存储目录），导入前 tasks 目录强制置顶。"""
    _s = str(_TASKS_DIR)
    while _s in sys.path:
        sys.path.remove(_s)
    sys.path.insert(0, _s)
    sys.modules.pop("service", None)
    from service import TaskService

    return TaskService(data_dir=str(tmp_path / "tasks"))


def _metric(metric_id: str, passed: bool, **kw: Any) -> Any:
    from _eval_core import MetricResult

    return MetricResult(metric_id=metric_id, passed=passed, **kw)


def _eval_result(task_id: str, metrics: list[Any], *, summary: str = "") -> Any:
    from _eval_core import EvaluationResult

    r = EvaluationResult(task_id=task_id, results=metrics, summary=summary)
    r.compute_overall()
    return r


def _inject_tool(
    mod: Any,
    monkeypatch: Any,
    service: Any,
    state_rows: list[dict[str, Any]],
) -> tuple[Any, AsyncMock]:
    """注入服务获取/state 读面（含 ws_meta 行）/写面与合并机制替身。"""
    state_writer = AsyncMock()
    monkeypatch.setattr(mod.TaskEvaluateTool, "_get_task_service", lambda _self: service)
    monkeypatch.setattr(mod, "_state_reader", lambda: state_rows)
    monkeypatch.setattr(mod, "_state_writer", state_writer)
    monkeypatch.setattr(
        mod.worktree_merge, "merge_worktree_before_complete", lambda _task_id, _ws_meta: None
    )
    return mod.TaskEvaluateTool(), state_writer


def _baseline_epoch(task: Any) -> float:
    return datetime.fromisoformat(task.created_at).timestamp()


def _make_workspace(tmp_path: Path, task: Any, files: dict[str, float]) -> Path:
    """构造工作空间：相对路径 → 真实文件，mtime = 任务基线 + 偏移秒。

    文件名可含 ``{task.id}`` 动态段（调用方 f-string 预格式化）；字面
    ``{{task_id}}`` 名文件用 "LITERAL" 键标记，经 replace 生成。
    """
    base = _baseline_epoch(task)
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    for rel, offset in files.items():
        rel_path = rel.replace("LITERAL", "{{task_id}}")
        p = ws / rel_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("evidence", encoding="utf-8")
        t = base + offset
        os.utime(p, (t, t))
    return ws


def _rows_with_ws(task: Any, ws_meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    row: dict[str, Any] = {"pipeline_id": task.id}
    if ws_meta is not None:
        row["task.ws_meta"] = ws_meta
    return [row]


def _ac_expanded(paths: list[str]) -> dict[str, Any]:
    """acceptance_criteria 白话 files 展开形态（task_submit 主管道存储形态）。"""
    return {
        f"file_check::{p}": {"input_params": {"path": p, "check": "not_empty"}}
        for p in paths
    }


def _forbid_git_calls(mod: Any, monkeypatch: Any) -> None:
    """豁免轨道性质断言替身：任何 git 进程探测即失败（commit 面须整体跳过）。"""

    def _no_git(*args: Any, **kw: Any) -> Any:
        raise AssertionError("豁免轨道不得发起 git 进程探测（commit 面应整体跳过）")

    monkeypatch.setattr(mod.subprocess, "run", _no_git)


def _assert_completed(out: Any, state_writer: Any) -> None:
    assert out.success is True
    assert out.metadata["result"] == "completed"
    assert state_writer.await_args.args[1]["task.status"] == "completed"


def _assert_rejected(out: Any, state_writer: Any) -> str:
    """拒绝完赛：retry + 不落 completed 终态；返回回执文本。"""
    assert state_writer.await_count == 0
    assert out.success is True
    assert out.metadata["result"] == "retry"
    assert out.output["overall_passed"] is False
    receipt = out.metadata["message"]
    assert isinstance(receipt, str)
    assert receipt
    return receipt


class TestTaskIdTemplateResolution:
    @pytest.mark.asyncio
    async def test_expanded_form_resolved_to_real_id(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态1：豁免轨展开形态 file_check::{{task_id}}_x.md + 真实 ID 文件在
        → completed（修复前按字面实查必拒；真实仓 1a42e85aaa47 实测形态）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["{{task_id}}_报告.md"])},
        )
        ws = _make_workspace(tmp_path, task, {f"{task.id}_报告.md": 60.0})
        _forbid_git_calls(mod, monkeypatch)
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_raw_files_form_resolved_multiple_paths(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态2：字面 files 键形态（原始白话）多项含子目录 → 同样替换后实查
        completed（第二组输入，防单点拟合）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": {"files": ["{{task_id}}_a.md", "{{task_id}}_sub/b.md"]}},
        )
        ws = _make_workspace(
            tmp_path, task, {f"{task.id}_a.md": 60.0, f"{task.id}_sub/b.md": 120.0}
        )
        _forbid_git_calls(mod, monkeypatch)
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_literal_fallback_file_does_not_rescue(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态3（诚实失败）：真实 ID 文件缺失、工作空间只有**字面**名文件
        → 仍拒绝（替换不是"哪个存在查哪个"的猜测；字面兜底文件救不了验收，
        堵死'agent 被迫落字面文件'路径的回旋空间）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["{{task_id}}_报告.md"])},
        )
        # 工作空间只落字面名文件（历史实测中 agent 的被迫兜底形态）
        ws = _make_workspace(tmp_path, task, {"LITERAL_报告.md": 60.0})
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "{{task_id}}" not in receipt  # 回执不得再引导 agent 落字面文件

    @pytest.mark.asyncio
    async def test_receipt_echoes_resolved_path(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态4（回显契约）：拒绝回执含替换后的实查路径（真实 ID）、不含
        {{task_id}} 字面字样——评估者看到的是评估真正查过的路径。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"acceptance_criteria": _ac_expanded(["{{task_id}}_缺失.md"])},
        )
        ws = _make_workspace(tmp_path, task, {})
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert f"{task.id}_缺失.md" in receipt
        assert "{{task_id}}" not in receipt

    @pytest.mark.asyncio
    async def test_required_files_form_resolved_too(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态5：既有轨 required_files（metadata 字面清单）含 {{task_id}}
        → 同刀替换后实查（凡验收路径含 {{task_id}} 一律先替换）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={"required_files": ["{{task_id}}_交付.json"]},
        )
        ws = _make_workspace(tmp_path, task, {f"{task.id}_交付.json": 60.0})
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        _assert_completed(out, state_writer)

    @pytest.mark.asyncio
    async def test_mixed_forms_only_plain_missing_listed(
        self, mod: Any, service: Any, monkeypatch: Any, tmp_path: Path
    ) -> None:
        """态6（区分度）：{{task_id}} 路径齐备 + 普通路径缺失 → 回执只列普通
        路径（替换路径齐备不误列，不吞并不同形态的判定）。"""
        task = await service.create_task(
            title="评估任务",
            metadata={
                "acceptance_criteria": _ac_expanded(["{{task_id}}_在.md", "普通缺失.md"]),
            },
        )
        ws = _make_workspace(tmp_path, task, {f"{task.id}_在.md": 60.0})
        tool, state_writer = _inject_tool(
            mod, monkeypatch, service, _rows_with_ws(task, {"mode": "plain", "path": str(ws)})
        )
        out = await tool._complete_task(service, task, _eval_result(task.id, [_metric("m1", True)]))
        receipt = _assert_rejected(out, state_writer)
        assert "普通缺失.md" in receipt
        assert f"{task.id}_在.md" not in receipt


class TestTaskIdPlaceholderFunction:
    def test_only_task_id_replaced_other_tokens_preserved(self, mod: Any) -> None:
        """态7（纯函数性质）：只替换 {{task_id}}；{{workspace}}/{{state:...}}/
        单花 {title} 等其他形态原样保留——goal/描述类 {{...}} 白名单放行行为
        的评估面等价锚（本函数是唯一替换通道，不做通配剥除）。"""
        resolve = mod._resolve_task_placeholder
        tid = "1a42e85aaa47"
        assert resolve("docs/{{task_id}}_报告.md", tid) == f"docs/{tid}_报告.md"
        assert resolve(
            "{{state:context.task_id}}_{{task_id}}/{{workspace}}/{title}.md", tid
        ) == "{{state:context.task_id}}_" + tid + "/{{workspace}}/{title}.md"
        # 无占位符/空串原样
        assert resolve("plain.md", tid) == "plain.md"
        assert resolve("", tid) == ""
