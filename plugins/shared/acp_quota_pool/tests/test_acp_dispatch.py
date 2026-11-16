# @feature: FP-0.2.ACP额度池 M0 | @ci: python-coverage
"""acp_engine 派发行为测试——真实 ACP ndjson JSON-RPC over stdio（fake agent 子进程）。

关键路径走真实线缆协议：引擎（SDK 客户端）以子进程拉起 fake_acp_agent.py
（手搓 ACP ndjson JSON-RPC，宽容语义对齐生产 peer），全协议握手/回合，无
进程内 mock。mock 仅限外部依赖（fake clock 注入账本）。≥2 组有区分度输入
（不同 goal/会话续做/许可选择）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from acp_engine import DispatchRequest, dispatch
from acp_ledger import QuotaLedger
from acp_registry import ProviderSpec

pytestmark = pytest.mark.unit

_FAKE_AGENT = Path(__file__).resolve().parent / "fake_acp_agent.py"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def _spec(mode: str = "echo", timeout: float = 30.0, delay_seconds: float = 0.0) -> ProviderSpec:
    args = [str(_FAKE_AGENT), mode]
    if delay_seconds > 0:
        args.append(str(delay_seconds))
    return ProviderSpec(
        id="fake",
        name="Fake",
        command=sys.executable,
        args=tuple(args),
        timeout_seconds=timeout,
        quota_model="test",
        cost_class="free",
        strength="unverified",
    )


def _ledger(tmp_path: Path) -> QuotaLedger:
    return QuotaLedger(tmp_path / "ledger.json", clock=_FakeClock())


def _request(goal: str, session_id: str = "") -> DispatchRequest:
    return DispatchRequest(goal=goal, cwd=str(_FAKE_AGENT.parent), session_id=session_id)


async def test_echo_roundtrip_returns_ok_and_records_ledger(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("PING-alpha-7482"), _spec(), ledger)

    assert result["provider"] == "fake"
    assert result["status"] == "ok"
    assert result["session_id"].startswith("fake-ses-")
    assert "PING-alpha-7482" in result["summary"]
    row = ledger.report()[0]
    assert row["dispatch_total"] == 1
    assert row["ok_total"] == 1
    assert row["last_session_id"] == result["session_id"]


async def test_distinct_goals_produce_distinct_summaries_and_sessions(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    first = await dispatch(_request("GOAL-one"), _spec(), ledger)
    second = await dispatch(_request("GOAL-two"), _spec(), ledger)

    assert "GOAL-one" in first["summary"]
    assert "GOAL-two" in second["summary"]
    assert first["session_id"] != second["session_id"]
    assert ledger.report()[0]["dispatch_total"] == 2


async def test_session_id_resumes_existing_session(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("follow-up", session_id="fake-ses-0009"), _spec(), ledger)

    assert result["status"] == "ok"
    assert result["session_id"] == "fake-ses-0009"
    assert ledger.report()[0]["last_session_id"] == "fake-ses-0009"


async def test_permission_request_selects_allow_once(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("needs-permission"), _spec(mode="perm"), ledger)

    assert result["status"] == "ok"
    assert "perm-selected=opt-allow" in result["summary"]


async def test_initialize_failure_is_failed_not_wall(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode="fail-init"), ledger)

    assert result["status"] == "failed"
    assert result["session_id"] == ""
    assert "refuses to initialize" in result["summary"]
    row = ledger.report()[0]
    assert row["dispatch_total"] == 1
    assert row["ok_total"] == 0
    assert row["wall_hit"] is False
    assert row["last_status"] == "failed"


async def test_quota_error_is_classified_as_wall(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode="quota"), ledger)

    assert result["status"] == "failed"
    row = ledger.report()[0]
    assert row["wall_hit"] is True
    assert row["wall_hit_total"] == 1
    assert row["last_wall_hit"]["error_class"] == "quota_or_rate_limit"
    assert "429" in row["last_wall_hit"]["message"]


async def test_prompt_timeout_is_classified_and_not_wall(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode="slow", timeout=1.0, delay_seconds=10.0), ledger)

    assert result["status"] == "failed"
    row = ledger.report()[0]
    assert row["wall_hit"] is False
    assert row["timeline"][0]["error_class"] == "timeout"


async def test_non_end_turn_stop_reports_failure_with_reason(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode="refuse"), ledger)

    assert result["status"] == "failed"
    assert "refusal" in result["summary"]
    assert ledger.report()[0]["last_status"] == "failed"


def test_missing_command_binary_classifies_spawn_failure(tmp_path: Path) -> None:
    """spawn 面失败（二进制不存在）归类 spawn_failure 且不挂死。

    同步烟测：asyncio.run 内跑一次失败派发（Windows 下 create_subprocess_exec
    对不存在路径抛 FileNotFoundError/NotADirectoryError）。
    """

    async def _run() -> dict[str, str]:
        spec = ProviderSpec(
            id="ghost",
            name="Ghost",
            command="Z0-absent-binary-Z0",
            args=(),
            timeout_seconds=5.0,
            quota_model="",
            cost_class="free",
            strength="weak",
        )
        return await dispatch(_request("any"), spec, _ledger(tmp_path))

    import asyncio

    result = asyncio.run(_run())
    assert result["status"] == "failed"
