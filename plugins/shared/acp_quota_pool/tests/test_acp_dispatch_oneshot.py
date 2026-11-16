# @feature: FP-0.2.ACP额度池 M1 | @ci: python-coverage
"""acp_engine oneshot 派发行为测试——真实子进程（fake one-shot CLI，零 mock）。

oneshot 后端 = spawn 命令 + args 占位符替换 + stdout 容错收集（zcode -p 语义）。
关键路径走真实进程边界（exit code/stdout/stderr/超时 kill），mock 仅限外部依赖
（fake clock 注入账本）。≥2 组有区分度输入（不同 goal/JSON 与非 JSON 输出/
撞墙与超时）；会话续做请求 = 配置面错误向上传播（引擎契约）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from acp_engine import DispatchRequest, dispatch
from acp_ledger import QuotaLedger
from acp_registry import ProviderSpec

pytestmark = pytest.mark.unit

_FAKE_AGENT = Path(__file__).resolve().parent / "fake_oneshot_agent.py"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def _spec(mode: str = "echo", timeout: float = 30.0) -> ProviderSpec:
    args = [str(_FAKE_AGENT), mode]
    if mode in ("echo", "plain", "empty"):
        args.append("{goal}")
    if mode == "slow":
        args.append("10")
    return ProviderSpec(
        id="fake-oneshot",
        name="Fake OneShot",
        command=sys.executable,
        args=tuple(args),
        timeout_seconds=timeout,
        quota_model="test",
        cost_class="paid",
        strength="unverified",
        backend="oneshot",
    )


def _ledger(tmp_path: Path) -> QuotaLedger:
    return QuotaLedger(tmp_path / "ledger.json", clock=_FakeClock())


def _request(goal: str, session_id: str = "") -> DispatchRequest:
    return DispatchRequest(goal=goal, cwd=str(_FAKE_AGENT.parent), session_id=session_id)


async def test_echo_roundtrip_substitutes_goal_and_records_one_shot(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("PING-alpha-7482"), _spec(), ledger)

    assert result["provider"] == "fake-oneshot"
    assert result["status"] == "ok"
    assert result["session_id"] == ""  # one-shot 口径：无会话可记
    assert "PING-alpha-7482" in result["summary"]
    row = ledger.report()[0]
    assert row["dispatch_total"] == 1
    assert row["ok_total"] == 1
    assert row["last_session_id"] == ""


async def test_distinct_goals_produce_distinct_summaries(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    first = await dispatch(_request("GOAL-one"), _spec(), ledger)
    second = await dispatch(_request("GOAL-two"), _spec(), ledger)

    assert "GOAL-one" in first["summary"]
    assert "GOAL-two" in second["summary"]
    assert first["summary"] != second["summary"]
    assert ledger.report()[0]["dispatch_total"] == 2


async def test_non_json_stdout_falls_back_to_raw_text(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("raw-goal-99"), _spec(mode="plain"), ledger)

    assert result["status"] == "ok"
    assert "plain-text-answer no json here" in result["summary"]
    assert ledger.report()[0]["ok_total"] == 1


@pytest.mark.parametrize(
    ("mode", "stdout_text"),
    [
        ("badjson", "{definitely not json"),  # { 开头但解析失败
        ("noresponse", '{"ok": true, "other": "field"}'),  # 合法 JSON 但无 response 字段
    ],
)
async def test_tolerant_parse_falls_back_to_raw_stdout(
    tmp_path: Path, mode: str, stdout_text: str
) -> None:
    """stdout 容错解析：JSON 形态残缺时回退原文，不误判失败（错误时可能非 JSON）。"""
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode=mode), ledger)

    assert result["status"] == "ok"
    assert stdout_text in result["summary"]
    row = ledger.report()[0]
    assert row["ok_total"] == 1


async def test_empty_stdout_exit_zero_is_failed(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode="empty"), ledger)

    assert result["status"] == "failed"
    assert "exit=0" in result["summary"]
    row = ledger.report()[0]
    assert row["ok_total"] == 0
    assert row["wall_hit"] is False


async def test_nonzero_exit_is_failed_not_wall(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode="fail"), ledger)

    assert result["status"] == "failed"
    assert "fake one-shot exploded" in result["summary"]
    row = ledger.report()[0]
    assert row["dispatch_total"] == 1
    assert row["ok_total"] == 0
    assert row["wall_hit"] is False
    assert row["last_status"] == "failed"


async def test_quota_wording_is_classified_as_wall(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode="quota"), ledger)

    assert result["status"] == "failed"
    row = ledger.report()[0]
    assert row["wall_hit"] is True
    assert row["wall_hit_total"] == 1
    assert row["last_wall_hit"]["error_class"] == "quota_or_rate_limit"


async def test_timeout_kills_process_and_classifies_timeout(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)

    result = await dispatch(_request("any"), _spec(mode="slow", timeout=1.0), ledger)

    assert result["status"] == "failed"
    row = ledger.report()[0]
    assert row["wall_hit"] is False
    assert row["timeline"][0]["error_class"] == "timeout"


def test_session_resume_on_oneshot_is_config_error_not_dispatch(tmp_path: Path) -> None:
    """oneshot 后端请求会话续做 = 配置面错误：ValueError 向上传播且不记账。"""
    ledger = _ledger(tmp_path)

    with pytest.raises(ValueError, match="oneshot"):
        asyncio.run(dispatch(_request("any", session_id="sess-x"), _spec(), ledger))

    assert ledger.report() == []  # 配置面错误不产生派发记录


def test_cli_dispatch_timeout_override_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI 车道：--timeout 覆盖注册表值（dataclasses.replace 透传 backend 全字段）。"""
    import json as json_mod

    from acp_pool import main as pool_main

    registry = tmp_path / "providers.toml"
    registry.write_text(
        "\n".join(
            [
                "version = 1",
                "[providers.fake-oneshot]",
                'name = "Fake OneShot"',
                'command = ' + repr(sys.executable),
                "args = ['" + str(_FAKE_AGENT).replace("\\", "\\\\") + "', 'echo', '{goal}']",
                "backend = \"oneshot\"",
                "timeout_seconds = 300",
                'cost_class = "paid"',
                'strength = "unverified"',
            ]
        ),
        encoding="utf-8",
    )
    ledger = tmp_path / "ledger.json"

    code = pool_main(
        [
            "dispatch",
            "--goal",
            "CLI-goal-42",
            "--cwd",
            str(_FAKE_AGENT.parent),
            "--timeout",
            "20",
            "--registry",
            str(registry),
            "--ledger",
            str(ledger),
            "--json",
        ]
    )

    assert code == 0
    payload = json_mod.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert "CLI-goal-42" in payload["summary"]
    assert payload["session_id"] == ""
    assert QuotaLedger(ledger).report()[0]["ok_total"] == 1
