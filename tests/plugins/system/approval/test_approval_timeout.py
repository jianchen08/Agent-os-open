# @feature: FP-0.2.五 审批闭环 | @vision: V2 全能闭环 | @ci: python-coverage
"""approval.create_choice 超时/异常语义单测（F-APPROVAL-1）。

产品决策：审批等待超时 = **拒绝**（不恢复管道、记录拒绝状态），
不再"超时即恢复放行"；默认超时调大（24h=86400s）且可经环境变量覆盖。

契约依据：
- human-interaction 的 ``wait_for_choice`` 在 service 层 *raise*
  ``InteractionTimeoutError``；经 capability 层（工具处理器）收敛为
  返回 ``{"error": ..., "error_code": "INTERACTION_TIMEOUT", ...}``。
  故 approval 视角下，超时表现为「返回值带 error 键」或「cap 调用 raise」
  两种形态——本测试两类都覆盖，修复必须对二者一视同仁地拒绝。

加载方式：本仓库存在大量同名 ``server.py``（人机交互/管道各插件均有一个），
而 tests/plugins/conftest.py 的裸名串扰治理钩子会对 ``sys.modules["server"]``
做逐测试驱逐——裸 ``import server`` 在多插件目录下解析不确定。故此处用
importlib 从绝对路径加载被测模块到唯一模块名，彻底绕开裸名冲突，保证确定性。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


# ============================================================
# 确定性加载被测模块（唯一名，绕开 sys.modules["server"] 串扰）
# ============================================================

_SERVER_PATH = (
    Path(__file__).resolve().parents[4]
    / "plugins" / "shared" / "system" / "approval" / "server.py"
)


def _load_server() -> Any:
    spec = importlib.util.spec_from_file_location("approval_server_under_test", _SERVER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


server = _load_server()


# ============================================================
# 装配：在 server.plugin 上注入假的 human-interaction / pipeline-executor
# ============================================================


def _install_caps(
    *,
    wait_res: dict[str, Any] | None = None,
    wait_exc: BaseException | None = None,
    create_res: dict[str, Any] | None = None,
    resume_ok: bool = True,
) -> tuple[list[tuple[str, dict[str, Any]]], list[tuple[str, dict[str, Any]]]]:
    """注入假的 capability 句柄，返回 (human_calls, pipeline_calls) 调用记录。

    - human.create_choice → create_res（默认 {"request_id": "req-test"}）
    - human.wait_for_choice → wait_res，或 raise wait_exc
    - pipeline.suspend → {"run_id":"run-1","branch_id":"b1","seq":1}
    - pipeline.resume → resume_ok 时 {}，否则 raise（模拟 executor 恢复失败）
    """
    from agentos_plugin_sdk import CapabilityHandle

    human_calls: list[tuple[str, dict[str, Any]]] = []
    pipeline_calls: list[tuple[str, dict[str, Any]]] = []

    # SDK CapabilityHandle.call 透传三参 (method, params, timeout)
    # （25681164c 起 timeout 为 call_fn 契约一部分），fake 对齐该签名。
    async def human_call_fn(
        method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        human_calls.append((method, params))
        if method == "create_choice":
            return create_res if create_res is not None else {"request_id": "req-test"}
        if method == "wait_for_choice":
            if wait_exc is not None:
                raise wait_exc
            return wait_res
        return {}

    async def pipeline_call_fn(
        method: str, params: dict[str, Any], timeout: float | None = None
    ) -> Any:
        pipeline_calls.append((method, params))
        if method == "suspend":
            return {"run_id": "run-1", "branch_id": "b1", "seq": 1}
        if method == "resume" and not resume_ok:
            raise RuntimeError("executor resume failed")
        return {}

    # plugin._capabilities 是内核能力句柄注入槽（外部依赖，无公开注入 API）；
    # 两个假句柄隔离 human-interaction / pipeline-executor 外部服务。
    server.plugin._capabilities["human-interaction"] = CapabilityHandle(
        "human-interaction", call_fn=human_call_fn
    )
    server.plugin._capabilities["pipeline-executor"] = CapabilityHandle(
        "pipeline-executor", call_fn=pipeline_call_fn
    )
    return human_calls, pipeline_calls


def _resume_calls(pipeline_calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    return [c for c in pipeline_calls if c[0] == "resume"]


@pytest.fixture(autouse=True)
def _reset_state() -> None:
    """每个测试前后清掉模块级 _suspended / _decisions，杜绝跨用例污染。

    二者是模块级真实状态（无公开 reset API），此为测试隔离性清理而非断言。
    """
    server._suspended.clear()
    server._decisions.clear()
    server.plugin._capabilities.clear()
    yield
    server._suspended.clear()
    server._decisions.clear()
    server.plugin._capabilities.clear()


# ============================================================
# 超时/异常 = 拒绝
# ============================================================


class TestApprovalTimeoutReject:
    async def test_timeout_returns_rejected_and_does_not_resume(self) -> None:
        """超时（cap 返回 error）→ status=rejected、不恢复、句柄清理、拒绝可查。"""
        _, pipeline_calls = _install_caps(
            wait_res={
                "error": "交互超时",
                "error_code": "INTERACTION_TIMEOUT",
                "request_id": "req-test",
            }
        )
        result = await server.create_choice(
            title="继续执行？", options=["同意", "拒绝"], run_id="run-1"
        )

        # 拒绝语义：不恢复管道
        assert result["status"] == "rejected"
        assert result["resumed"] is False
        assert result.get("reason")

        # 杜绝句柄泄漏：_suspended 必须已清理（内部不变量，无公开读面；
        # 其公开后果——管道挂死——正是本契约要防的）
        assert "req-test" not in server._suspended
        assert len(server._suspended) == 0

        # 关键护栏：超时绝不恢复管道（"超时即恢复"= 绕过审批）
        assert _resume_calls(pipeline_calls) == []

        # 拒绝状态可查（公开面）：submit 返回已记录的终态，且绝不二次恢复
        queried = await server.submit(request_id="req-test", result="whatever")
        assert queried["status"] == "rejected"
        assert queried["approved"] is False
        assert queried["reason"] == "timeout"
        assert queried["resumed"] is False
        assert _resume_calls(pipeline_calls) == []

    async def test_wait_exception_cleans_suspended_and_does_not_resume(self) -> None:
        """wait_for_choice raise（连接中断等）→ 同样拒绝 + 句柄不泄漏。"""
        _, pipeline_calls = _install_caps(wait_exc=RuntimeError("connection reset"))

        result = await server.create_choice(
            title="继续执行？", options=["同意"], run_id="run-1"
        )

        assert result["status"] == "rejected"
        assert result["resumed"] is False
        assert len(server._suspended) == 0
        assert _resume_calls(pipeline_calls) == []
        # 拒绝终态可查（公开面）
        queried = await server.submit(request_id="req-test", result="whatever")
        assert queried["status"] == "rejected"
        assert queried["approved"] is False

    async def test_submit_after_timeout_returns_rejected(self) -> None:
        """超时决断后，submit 命中已拒绝结果，不再尝试恢复。"""
        _install_caps(
            wait_res={
                "error": "交互超时",
                "error_code": "INTERACTION_TIMEOUT",
                "request_id": "req-test",
            }
        )
        await server.create_choice(title="t", options=["a"], run_id="run-1")

        res = await server.submit(request_id="req-test", result="whatever")
        assert res["status"] == "rejected"
        assert res["approved"] is False


# ============================================================
# 正常路径回归护栏
# ============================================================


class TestApprovalNormalPath:
    async def test_normal_choice_resumes_pipeline(self) -> None:
        """用户正常选择 → 恢复管道、清理句柄、记录 approved（行为不变）。"""
        _, pipeline_calls = _install_caps(
            wait_res={"request_id": "req-test", "selected_option": "1"}
        )

        result = await server.create_choice(
            title="继续执行？", options=["同意", "拒绝"], run_id="run-1"
        )

        assert result["status"] == "resolved"
        assert result["selected_option"] == "1"
        assert result["resumed"] is True
        # 句柄不泄漏（内部不变量，见超时用例注释）
        assert len(server._suspended) == 0
        assert len(_resume_calls(pipeline_calls)) == 1
        # 决断终态可查（公开面）：submit 命中已决断记录，不再重复恢复
        queried = await server.submit(request_id="req-test", result="1")
        assert queried["status"] == "resolved"
        assert queried["approved"] is True


# ============================================================
# 恢复失败不谎报（M13：决策记录 resumed 真值 + 维持挂起态）
# ============================================================


class TestResumeFailureHonesty:
    async def test_create_choice_resume_failed_keeps_pending_and_submit_retries(self) -> None:
        """选择后 resume 失败 → 显式 resume_failed、不落 approved 决策、保持挂起。

        submit 重试路径：executor 恢复后返回 resolved + resumed=True（真值）。
        """
        human_calls, pipeline_calls = _install_caps(
            wait_res={"request_id": "req-test", "selected_option": "0"},
            resume_ok=False,
        )
        result = await server.create_choice(
            title="继续执行？", options=["同意", "拒绝"], run_id="run-1"
        )

        assert result["status"] == "resume_failed"
        assert result["resumed"] is False
        assert result.get("reason")
        # 不落 approved 决策（谎报源）：submit 不得回放 approved→resumed=True
        assert "req-test" not in server._decisions
        # 挂起记录保持（供 submit 重试）
        assert "req-test" in server._suspended

        # 命中已失败决断前，submit 走重试而非谎报终态
        _, retry_calls = _install_caps(
            wait_res={"request_id": "req-test", "selected_option": "0"},
            resume_ok=True,
        )
        retried = await server.submit(request_id="req-test", result="0")
        assert retried["status"] == "resolved"
        assert retried["resumed"] is True
        assert len(_resume_calls(retry_calls)) == 1
        # 成功后决策记录携带 resumed 真值
        assert server._decisions["req-test"]["resumed"] is True

    async def test_submit_direct_resume_failed_does_not_record_approved(self) -> None:
        """submit 直恢复路径 resume 失败 → resume_failed，不落 approved、不清理挂起。"""
        human_calls, pipeline_calls = _install_caps(resume_ok=False)
        server._suspended["req-direct"] = {
            "suspend_handle": {"run_id": "run-1", "branch_id": "b1", "seq": 1},
            "run_id": "run-1",
            "created_at": 0.0,
        }

        res = await server.submit(request_id="req-direct", result="yes")
        assert res["status"] == "resume_failed"
        assert res["resumed"] is False
        assert "req-direct" not in server._decisions
        assert "req-direct" in server._suspended

        # 二次 submit（executor 恢复正常）成功，回放真值
        _install_caps(resume_ok=True)
        res2 = await server.submit(request_id="req-direct", result="yes")
        assert res2["status"] == "resolved"
        assert res2["resumed"] is True
        assert server._decisions["req-direct"]["resumed"] is True

    async def test_decision_replay_reports_true_resumed_value(self) -> None:
        """已决断记录回放 resumed 真值：无 run_id 决断 resumed=False 不被推成 True。

        （M13 回归：旧实现 resumed 恒等于 approved，approved=True 即谎报已恢复。）
        """
        _install_caps(wait_res={"request_id": "req-test", "selected_option": "0"})
        result = await server.create_choice(title="t", options=["a"])  # 无 run_id

        assert result["status"] == "resolved"
        assert result["resumed"] is False
        queried = await server.submit(request_id="req-test", result="0")
        assert queried["status"] == "resolved"
        assert queried["approved"] is True
        assert queried["resumed"] is False


# ============================================================
# 默认超时调大 + 可配置
# ============================================================


class TestApprovalDefaultTimeout:
    async def test_default_timeout_is_86400_and_propagates(self) -> None:
        """不传 timeout 时，默认 86400 透传给 human-interaction.create_choice。"""
        human_calls, _ = _install_caps(
            wait_res={"request_id": "req-test", "selected_option": "0"}
        )
        await server.create_choice(title="t", options=["a"], run_id="run-1")

        create_call = [c for c in human_calls if c[0] == "create_choice"][0]
        assert create_call[1]["timeout_seconds"] == 86400

    async def test_caller_can_override_timeout(self) -> None:
        """显式传 timeout 仍生效（向后兼容）。"""
        human_calls, _ = _install_caps(
            wait_res={"request_id": "req-test", "selected_option": "0"}
        )
        await server.create_choice(title="t", options=["a"], run_id="run-1", timeout=30)

        create_call = [c for c in human_calls if c[0] == "create_choice"][0]
        assert create_call[1]["timeout_seconds"] == 30

    def test_module_default_is_86400(self) -> None:
        assert server.DEFAULT_TIMEOUT_SECONDS == 86400

    def test_default_timeout_env_overridable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """环境变量在加载期决定公开常量 DEFAULT_TIMEOUT_SECONDS；非法值回退 86400。"""
        monkeypatch.setenv("APPROVAL_DEFAULT_TIMEOUT_SECONDS", "120")
        assert _load_server().DEFAULT_TIMEOUT_SECONDS == 120.0

        monkeypatch.setenv("APPROVAL_DEFAULT_TIMEOUT_SECONDS", "not-a-number")
        assert _load_server().DEFAULT_TIMEOUT_SECONDS == 86400.0

        monkeypatch.delenv("APPROVAL_DEFAULT_TIMEOUT_SECONDS", raising=False)
        assert _load_server().DEFAULT_TIMEOUT_SECONDS == 86400.0
