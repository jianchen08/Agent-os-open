# @feature: FP-0.2.ACP额度池 M0 | @ci: python-coverage
"""acp_ledger 行为测试——额度账本 v0（记账面）。

时钟属外部依赖：构造时注入 fake clock（不真睡）。断言可观察行为：
报表输出与文件持久化内容，不戳私有态。关键行为各 ≥2 组区分输入
（成功/失败、撞墙/非撞墙、时间线有界收敛两种形态）。
"""

from __future__ import annotations

import json
from pathlib import Path

import acp_ledger  # noqa: F401 — 模块面访问 _user_data_dir（对齐断言）
import pytest
import user_space
from acp_ledger import TIMELINE_CAP, USER_DATA_DIR_ENV, WALL_CLASSES, QuotaLedger, default_ledger_path

pytestmark = pytest.mark.unit


class _FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def _make(path: Path) -> QuotaLedger:
    return QuotaLedger(path, clock=_FakeClock())


def test_success_and_failure_accounting_keeps_distinct_counts(tmp_path: Path) -> None:
    ledger = _make(tmp_path / "ledger.json")
    ledger.record_success("opencode", session_id="ses-1", summary="ok-1")
    ledger.record_success("opencode", session_id="ses-2", summary="ok-2")
    ledger.record_failure("opencode", error_class="timeout", message="prompt timed out")

    rows = {row["provider"]: row for row in ledger.report()}
    row = rows["opencode"]
    assert row["dispatch_total"] == 3
    assert row["ok_total"] == 2
    assert row["last_session_id"] == "ses-2"
    assert row["last_status"] == "failed"
    assert row["wall_hit"] is False  # timeout 非额度墙
    assert row["wall_hit_total"] == 0
    assert len(row["timeline"]) == 3
    # 时间线按序追加（fake clock 单调递增）
    assert [entry["status"] for entry in row["timeline"]] == ["ok", "ok", "failed"]


@pytest.mark.parametrize("error_class", sorted(WALL_CLASSES))
def test_wall_classes_raise_signal_flag_and_total(tmp_path: Path, error_class: str) -> None:
    ledger = _make(tmp_path / "ledger.json")
    ledger.record_failure("opencode", error_class=error_class, message="wall")
    ledger.record_failure("opencode", error_class="protocol_or_agent", message="not-a-wall")

    row = ledger.report()[0]
    assert row["wall_hit"] is True
    assert row["wall_hit_total"] == 1
    wall = row["last_wall_hit"]
    assert wall["error_class"] == error_class
    assert wall["message"] == "wall"
    assert wall["ts"]


def test_timeline_is_bounded_and_drops_oldest(tmp_path: Path) -> None:
    ledger = _make(tmp_path / "ledger.json")
    for i in range(TIMELINE_CAP + 5):
        ledger.record_success("opencode", session_id=f"ses-{i}", summary="")

    row = ledger.report()[0]
    assert row["dispatch_total"] == TIMELINE_CAP + 5  # 计数不受时间线截断影响
    assert len(row["timeline"]) == TIMELINE_CAP
    assert row["timeline"][0]["session_id"] == "ses-5"  # 最旧 5 条被逐出
    assert row["timeline"][-1]["session_id"] == f"ses-{TIMELINE_CAP + 4}"


def test_state_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    first = QuotaLedger(path, clock=_FakeClock())
    first.record_success("opencode", session_id="ses-p", summary="")

    second = QuotaLedger(path, clock=_FakeClock())
    second.record_failure("opencode", error_class="auth", message="401 unauthorized")

    row = second.report()[0]
    assert row["dispatch_total"] == 2
    assert row["ok_total"] == 1
    assert row["wall_hit_total"] == 1


def test_unseen_provider_absent_from_report(tmp_path: Path) -> None:
    ledger = _make(tmp_path / "ledger.json")
    assert ledger.report() == []


def test_corrupted_ledger_file_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(ValueError, match="损坏"):
        QuotaLedger(path, clock=_FakeClock())


def test_ledger_file_shape_is_portable_json(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    ledger = _make(path)
    ledger.record_success("opencode", session_id="ses-x", summary="hi")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert "opencode" in raw["providers"]
    assert raw["providers"]["opencode"]["last_session_id"] == "ses-x"


def test_default_ledger_path_follows_user_data_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(USER_DATA_DIR_ENV, str(tmp_path))

    assert default_ledger_path() == tmp_path / "acp_bridge" / "quota_ledger.json"


# ── user_space 对齐断言（多端平台差异普查 ⚠️-3 收敛锚）──────────


class TestUserSpaceCopyAlignment:
    """acp_ledger user-root 解析副本与共享根本体同输入同输出。

    本模块随技能载荷播种（``acp_seed.PAYLOAD_FILES`` 显式含
    ``scripts/acp_ledger.py``）到 ``<user_root>/skills/acp-quota-pool/``，
    播种副本对共享根裸模块不可达（禁共享根跨包 import 铁律）——自包含
    副本合法存在，防漂移靠本组对齐断言；改动须与
    ``plugins/shared/user_space.py`` 同刀（两侧锚注释互链）。
    """

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTOS_USER_ROOT", raising=False)
        monkeypatch.delenv(USER_DATA_DIR_ENV, raising=False)

    def test_user_data_dir_precedence_same_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """两级优先（DATA_DIR > USER_ROOT）两面逐一比对，未设两面同 None（零回落）。"""
        monkeypatch.setenv(USER_DATA_DIR_ENV, str(tmp_path / "d"))
        assert acp_ledger._user_data_dir() == user_space.user_data_dir() == tmp_path / "d"

        monkeypatch.delenv(USER_DATA_DIR_ENV)
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "root"))
        assert (
            acp_ledger._user_data_dir()
            == user_space.user_data_dir()
            == tmp_path / "root" / "data"
        )

        monkeypatch.delenv("AGENTOS_USER_ROOT")
        # 零回落：APPDATA/XDG 存在也不参与推导，两面同为 None
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
        assert acp_ledger._user_data_dir() is None
        assert user_space.user_data_dir() is None
