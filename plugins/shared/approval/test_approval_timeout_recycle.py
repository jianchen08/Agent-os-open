# @feature: FP-0.2.五 审批闭环 | @ci: python-coverage
"""审批拒绝终态的管道回收环（X3-F1，P-10 影子滞留修复）。

问题（用户体验巡检 P-10）：create_choice 的 wait_for_choice 超时/异常后按
「超时=拒绝」裁决只落 _record_decision 与日志，不向 pipeline-executor 发任何
恢复通知——内核侧经 pipeline-executor.suspend 落下的挂起簿记
（state.run_status="suspended" + suspend_request_id）无人回收：审批卡消失但
run 簿记恒挂 suspended（任务列表停摆态），下一轮 LLM 起手检查见 suspended 即
按「调用方停止」收场，用户无任何提示。对照组：用户显式应答走内核
interaction_response 链，簿记由 dispatch_interaction_response 回收
（run_status=running + 清凭据）。

修复契约（复用显式应答同款 resume 通道，不新造机制）：
- 拒绝裁决收敛处（rejected_reason 非 None）必回收挂起簿记——有挂起句柄即调
  pipeline-executor.resume，参数带完整句柄（run_id/branch_id/seq）+
  approval_id + 拒绝裁决 result（绝不放行：approved=False）。
- 回收成功：决策记录与返回面 resumed=True（管道可继续结算）；失败：仅留痕，
  不重武装 submit 重试面（超时经 submit 变相放行 = 语义弱化，禁止）。
- 无 run_id（纯交互）：无挂起簿记可回收，resume 不得被调用。
- 回归锚：显式批准路径 resume 语义零变化（result=所选选项）。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

# SDK 路径（agentos_plugin_sdk 未安装时）
_SDK_DIR = Path(__file__).resolve().parents[3] / "sdk" / "src"
if str(_SDK_DIR) not in sys.path:
    sys.path.insert(0, str(_SDK_DIR))

# plugins/shared 根（http_json 等裸名导入）
_SHARED_ROOT = Path(__file__).resolve().parents[1]
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))


def _load_server() -> Any:
    """动态加载 server.py（每次新建，模块级 _suspended/_ownership/_decisions 隔离）。"""
    mod_name = "approval_timeout_recycle_test"
    module_path = _PLUGIN_DIR / "server.py"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, module_path)
    assert spec is not None, "Cannot load server.py"
    assert spec.loader is not None, "Cannot load server.py"
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _inject_capability(module: Any, name: str, handle: Any) -> None:
    """把伪 capability handle 注入插件（外部依赖替身：内核能力面）。"""
    original = module.plugin.get_capability
    module.plugin.get_capability = lambda n: handle if n == name else original(n)  # type: ignore[method-assign]


_SUSPEND_RESULT = {
    "status": "suspended",
    "run_id": "run-x3f1",
    "branch_id": "main",
    "seq": 3,
}


class _RecordingPipeline:
    """伪 pipeline-executor handle：按方法回放结果，调用全留痕 (method, params)。

    suspend 回放内核真实返回形态（status/run_id/branch_id/seq）；resume 默认
    回放 {"status": "resumed", ...}，可注入异常（恢复通道故障）。
    """

    def __init__(
        self,
        suspend_result: dict[str, Any] | None = None,
        resume_exc: Exception | None = None,
    ) -> None:
        self._suspend_result = suspend_result or dict(_SUSPEND_RESULT)
        self._resume_exc = resume_exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, params))
        if method == "suspend":
            return dict(self._suspend_result)
        if self._resume_exc is not None:
            raise self._resume_exc
        return {"status": "resumed", "run_id": params.get("run_id", "")}

    def resume_calls(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "resume"]

    def suspend_calls(self) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == "suspend"]


class _WaitErrorHi:
    """伪 human-interaction handle：create 成功，wait 回放预设错误形态（拒绝收敛）。"""

    def __init__(self, wait_result: Any) -> None:
        self._wait = wait_result
        self.calls: list[str] = []

    async def call(self, method: str, params: dict) -> Any:  # noqa: ARG002
        self.calls.append(method)
        if method == "create_choice":
            return {"request_id": "req-x3f1"}
        if method == "wait_for_choice":
            return self._wait
        return {}


class _RaisingHi:
    """伪 human-interaction handle：wait 必抛（capability 传输故障注入）。"""

    async def call(self, method: str, params: dict) -> Any:  # noqa: ARG002
        if method == "create_choice":
            return {"request_id": "req-x3f1"}
        raise RuntimeError("capability transport broken")


class _SilentBus:
    """吞掉 emit 调用的伪 event-bus（本文件不关注事件载荷）。"""

    async def notify(self, method: str, params: dict) -> None:  # noqa: ARG002
        return None


def _setup(
    mod: Any, hi: Any, pipeline: _RecordingPipeline
) -> _RecordingPipeline:
    """注入 event-bus / human-interaction / pipeline-executor 三件套。"""
    _inject_capability(mod, "event-bus", _SilentBus())
    _inject_capability(mod, "human-interaction", hi)
    _inject_capability(mod, "pipeline-executor", pipeline)
    return pipeline


# ═══════════════════════════════════════════════════════════
# 拒绝裁决必回收挂起簿记（X3-F1 主契约）
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("wait_res", "expected_reason"),
    [
        ({"error": "timed out", "error_code": "INTERACTION_TIMEOUT"}, "timeout"),
        ({"error": "cancelled", "error_code": "INTERACTION_CANCELLED"}, "cancelled"),
        ({"error": "denied by user"}, "rejected"),
    ],
)
async def test_rejection_recycles_suspended_pipeline(
    wait_res: dict, expected_reason: str
) -> None:
    """拒绝裁决（超时/取消/拒绝错误形态）后必须回收挂起簿记。

    有挂起句柄即调 pipeline-executor.resume：参数带完整句柄 +
    approval_id + 拒绝裁决 result（approved=False 绝不放行）；
    决策记录与返回面 resumed=True（任务侧得到可继续信号）。
    """
    mod = _load_server()
    pipeline = _setup(mod, _WaitErrorHi(wait_res), _RecordingPipeline())

    out = await mod.create_choice(title="t", options=["批准"], run_id="run-x3f1")

    # 裁决语义不弱化：终态仍是拒绝
    assert out["status"] == "rejected"
    assert out["reason"] == expected_reason
    # 管道回收环：resume 恰好一次，参数携带句柄与拒绝裁决
    resumes = pipeline.resume_calls()
    assert len(resumes) == 1
    params = resumes[0]
    assert params["approval_id"] == "req-x3f1"
    assert params["run_id"] == "run-x3f1"
    assert params["branch_id"] == "main"
    assert params["seq"] == 3
    assert params["result"] == {"approved": False, "reason": expected_reason}
    # 决策记录与返回面如实反映回收
    assert out["resumed"] is True
    decision = mod._decisions["req-x3f1"]
    assert decision["approved"] is False
    assert decision["reason"] == expected_reason
    assert decision["resumed"] is True
    # 挂起/归属记录不残留（既有 finally 清理不变）
    assert "req-x3f1" not in mod._suspended
    assert "req-x3f1" not in mod._ownership


@pytest.mark.asyncio
async def test_wait_exception_recycles_suspended_pipeline() -> None:
    """wait 异常（capability 传输故障）同样收敛为拒绝并回收挂起簿记。"""
    mod = _load_server()
    pipeline = _setup(mod, _RaisingHi(), _RecordingPipeline())

    out = await mod.create_choice(title="t", options=["批准"], run_id="run-x3f1")

    assert out["status"] == "rejected"
    assert out["reason"] == "rejected"
    resumes = pipeline.resume_calls()
    assert len(resumes) == 1
    assert resumes[0]["result"] == {"approved": False, "reason": "rejected"}
    assert out["resumed"] is True
    assert mod._decisions["req-x3f1"]["resumed"] is True


@pytest.mark.asyncio
async def test_rejection_without_run_id_never_touches_pipeline() -> None:
    """纯交互（无 run_id）：无挂起簿记可回收，resume 不得被调用。"""
    mod = _load_server()
    pipeline = _RecordingPipeline()
    _setup(mod, _WaitErrorHi({"error": "timed out", "error_code": "INTERACTION_TIMEOUT"}), pipeline)

    out = await mod.create_choice(title="t", options=["批准"])

    assert out["status"] == "rejected"
    assert out["resumed"] is False
    assert pipeline.calls == []


@pytest.mark.asyncio
async def test_recycle_failure_keeps_rejected_terminal_without_rearm() -> None:
    """回收失败（resume 通道故障）：拒绝终态不变、不重武装 submit 重试面。

    重武装会让超时经 submit 变相放行（语义弱化）——只留痕。
    """
    mod = _load_server()
    pipeline = _RecordingPipeline(resume_exc=RuntimeError("executor gone"))
    _setup(
        mod,
        _WaitErrorHi({"error": "timed out", "error_code": "INTERACTION_TIMEOUT"}),
        pipeline,
    )

    out = await mod.create_choice(title="t", options=["批准"], run_id="run-x3f1")

    assert out["status"] == "rejected"
    assert out["resumed"] is False
    decision = mod._decisions["req-x3f1"]
    assert decision["approved"] is False
    assert decision["resumed"] is False
    # 不重武装：submit 重读终态，不再触碰恢复面
    assert "req-x3f1" not in mod._suspended
    prior = await mod.submit("req-x3f1", "x")
    assert prior["status"] == "rejected"
    assert prior["approved"] is False
    assert len(pipeline.resume_calls()) == 1


# ═══════════════════════════════════════════════════════════
# 回归锚：显式批准路径零变化
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_explicit_approve_resume_semantics_unchanged() -> None:
    """显式批准：resume 携带所选选项（result=选项串），终态 resolved+resumed。"""
    mod = _load_server()
    pipeline = _setup(
        mod,
        _WaitErrorHi({"selected_option": "批准"}),
        _RecordingPipeline(),
    )

    out = await mod.create_choice(title="t", options=["批准"], run_id="run-x3f1")

    assert out["status"] == "resolved"
    assert out["resumed"] is True
    resumes = pipeline.resume_calls()
    assert len(resumes) == 1
    assert resumes[0]["approval_id"] == "req-x3f1"
    assert resumes[0]["result"] == "批准"
    decision = mod._decisions["req-x3f1"]
    assert decision["approved"] is True
    assert decision["reason"] == "resolved"
    assert decision["resumed"] is True
