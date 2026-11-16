# @feature: FP-0.2.〇 RP引擎(作者权威+/cheat) | @vision: V1 可进化 | @ci: python-coverage
"""rp_state_audit 共享审计流水 append 单源契约测试（RP 批 3 自 2-Ⅰ 落账步提取）。

两个写方共享同一文件与幂等契约（防两处漂移）：
- state_marker_parse（落账步）：idem={pipeline_id}:{尾条 assistant seq}；
- mode_material_inject 守卫（/cheat 命令行）：idem={pipeline_id}:cheat:{用户消息 seq}。

锁定行为：行信封 {ts, source, idem, **row}；同 idem 重放整批跳过；坏行不阻断
幂等扫描；ws 缺失/IO 失败降级返回 False 不抛；行级 source 覆盖缺省（behavior 行）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SHARED = Path(__file__).resolve().parents[1] / "plugins" / "shared"
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))

from rp_state_audit import append_audit_rows, has_idem, last_assistant_seq, story_idem, suffixed  # noqa: E402


def _read(ws: Path) -> list[dict[str, Any]]:
    audit = ws / "rp_state_audit.jsonl"
    assert audit.is_file()
    return [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]


def test_append_stamps_envelope_and_persists(tmp_path: Path) -> None:
    rows = [
        {"key": "cheating", "op": "=", "from": False, "to": True, "reason": "cheat_on"},
        {"key": "好感度", "op": "=", "from": 1, "to": 2, "reason": "accepted"},
    ]

    written = append_audit_rows(str(tmp_path), rows, "pipe-1:cheat:9", "cheat")

    assert written is True
    out = _read(tmp_path)
    assert len(out) == 2
    assert all(row["source"] == "cheat" for row in out)
    assert all(row["idem"] == "pipe-1:cheat:9" for row in out)
    assert all(row["ts"] for row in out), "信封带时间戳"
    assert out[0]["reason"] == "cheat_on"
    assert out[0]["from"] is False
    assert out[0]["to"] is True


def test_append_row_level_source_overrides_default(tmp_path: Path) -> None:
    """行为行自带 source=behavior：行级覆盖缺省（作弊轮 behavior 归因不丢）。"""
    rows = [{"key": "好感度", "op": "=", "from": 1, "to": 2,
             "reason": "cheat_bypass", "source": "behavior", "behavior": "怒目"}]

    append_audit_rows(str(tmp_path), rows, "pipe-1:2", "cheat")

    (row,) = _read(tmp_path)
    assert row["source"] == "behavior"
    assert row["behavior"] == "怒目"


def test_append_replay_same_idem_skips_batch(tmp_path: Path) -> None:
    rows = [{"key": "cheating", "op": "=", "from": True, "to": False, "reason": "cheat_off"}]

    assert append_audit_rows(str(tmp_path), rows, "pipe-1:cheat:9", "cheat") is True
    assert append_audit_rows(str(tmp_path), rows, "pipe-1:cheat:9", "cheat") is False

    assert len(_read(tmp_path)) == 1, "同 idem 重放不双记"


@pytest.mark.parametrize(
    "existing",
    [
        "{这不是JSON}\n",
        "null\n",
        "[1, 2]\n",
    ],
)
def test_append_bad_existing_lines_do_not_block_idem_scan(
    tmp_path: Path, existing: str
) -> None:
    audit = tmp_path / "rp_state_audit.jsonl"
    audit.write_text(existing, encoding="utf-8")
    rows = [{"key": "cheating", "op": "=", "from": False, "to": True, "reason": "cheat_on"}]

    assert append_audit_rows(str(tmp_path), rows, "pipe-1:cheat:9", "cheat") is True

    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2, "坏行保留 + 新行追加"
    assert json.loads(lines[1])["idem"] == "pipe-1:cheat:9"


def test_append_missing_workspace_degrades_false(tmp_path: Path) -> None:
    assert append_audit_rows("", [], "pipe-1:1", "cheat") is False
    assert append_audit_rows(None, [{"key": "k"}], "pipe-1:1", "cheat") is False  # type: ignore[arg-type]
    assert not (tmp_path / "rp_state_audit.jsonl").exists()


def test_append_io_failure_degrades_false_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("占用路径", encoding="utf-8")

    written = append_audit_rows(
        str(blocker), [{"key": "cheating", "op": "=", "from": False, "to": True}], "p:1", "cheat"
    )

    assert written is False, "ws 指向文件 = IO 失败降级，不抛不阻断调用方"


def test_append_empty_rows_with_valid_workspace_returns_true(tmp_path: Path) -> None:
    """空行表 + ws 可得 = 无事可记不算失败（不建文件，返回 True）。"""

    assert append_audit_rows(str(tmp_path), [], "pipe-1:1", "cheat") is True
    assert not (tmp_path / "rp_state_audit.jsonl").exists()


def test_last_assistant_seq_fail_soft_shapes() -> None:
    """last_assistant_seq 降级面（评审修复刀 idem 单源派生）：messages 缺席/
    非 list / 无 assistant 消息三形态 → None（fail-soft，不抛不猜）。"""
    assert last_assistant_seq({}) is None, "messages 键缺席 = None"
    assert last_assistant_seq({"messages": "不是列表"}) is None, "messages 非 list = None"
    assert last_assistant_seq({"messages": [{"role": "user", "seq": 1}]}) is None, (
        "无 assistant 消息（扫描穷尽）= None"
    )


def test_last_assistant_seq_returns_last_assistant_seq() -> None:
    """倒序扫描命中尾条 assistant 的 seq；user/中间消息不干扰。"""
    state = {"messages": [
        {"role": "user", "seq": 1},
        {"role": "assistant", "seq": 2},
        {"role": "user", "seq": 3},
        {"role": "assistant", "seq": 4},
    ]}
    assert last_assistant_seq(state) == 4, "尾条 assistant seq（倒序首命中）"


def test_story_idem_base_and_suffixed_domain_space() -> None:
    """story 基式 idem 单源格式 + suffixed 分域（评审修复刀两处派生点下沉）。"""
    assert story_idem("pipe-9", 7) == "pipe-9:7", "基式 = {pipeline_id}:{seq}"
    assert story_idem("pipe-9", None) == "pipe-9:-", "seq 缺席 = '-' 占位（幂等仍可判等）"
    assert suffixed("pipe-9:7", ":game") == "pipe-9:7:game", "game 域批与 story 批分幂等空间"


# ── has_idem 探针（RP 面板 v3.1 刀1：init effects 会话首轮幂等闸的读面）────────


def test_has_idem_false_when_missing_or_invalid_workspace(tmp_path: Path) -> None:
    """文件缺失 / ws 无效 = False（闸未建立，调用方按未结算处置）。"""
    assert has_idem(str(tmp_path), "pipe-1:init") is False
    assert has_idem(None, "pipe-1:init") is False
    assert has_idem("", "pipe-1:init") is False


def test_has_idem_true_after_append_and_shared_with_dedup(tmp_path: Path) -> None:
    """append 落盘后探针命中；探针与追加幂等同空间（同 idem 重放不双记）。"""
    idem = "pipe-1:init"
    assert has_idem(str(tmp_path), idem) is False
    assert append_audit_rows(str(tmp_path), [{"key": "好感度", "op": "+", "from": 0, "to": 5, "reason": "accepted"}], idem, "init") is True
    assert has_idem(str(tmp_path), idem) is True
    # 同 idem 再追加 = 幂等跳过（探针面与写面同一份判定）
    assert append_audit_rows(str(tmp_path), [{"key": "好感度", "op": "+", "from": 5, "to": 10, "reason": "accepted"}], idem, "init") is False
    assert len(_read(tmp_path)) == 1


def test_has_idem_corrupt_lines_do_not_block_scan(tmp_path: Path) -> None:
    """坏行跳过不影响探针判定（与追加幂等扫描同口径）。"""
    (tmp_path / "rp_state_audit.jsonl").write_text(
        "{坏行\n" + json.dumps({"ts": "t", "source": "init", "idem": "pipe-9:init", "key": "k"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    assert has_idem(str(tmp_path), "pipe-9:init") is True
    assert has_idem(str(tmp_path), "pipe-8:init") is False


def test_has_idem_read_failure_returns_false(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """读取 IO 失败 = False（闸未建立，不抛出——探针降级与写面同口径）。"""
    import rp_state_audit as audit_module

    (tmp_path / "rp_state_audit.jsonl").write_text("{}\n", encoding="utf-8")

    def boom(self, *args, **kwargs):
        raise OSError("模拟读取失败")

    monkeypatch.setattr(audit_module.Path, "read_text", boom)
    assert has_idem(str(tmp_path), "pipe-1:init") is False
