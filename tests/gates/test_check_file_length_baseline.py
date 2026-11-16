# @feature: FP-GATE 文件规模棘轮门禁 | @ci: python-coverage
"""check_file_length_baseline.py 单测。

行为契约（断输入→输出，不钉实现）：
- 四档判别：新文件（非测试 >1500 / 测试 >3000）超线红、1499/2900 绿；
  存量未入册触线 2500/5000 红、贴线绿；冻结件超入册天花板红、降线内
  红并提示删条目；软提醒（非测试 >1000 / 测试 >2000）只报表不阻断；
- 新/存判定：新文件无基线可入，基线条目覆盖新文件 = 账目红；
- 豁免：命中豁免绿；缺归因理由红；路径失效红；
- 触碰即拆（第五维，触碰拆分线非测试/测试同线 5000）：>5000 文件（含冻结件）
  被触碰红、2500~5000 灰带（含贴线）触碰不红、未触碰不红（现状保持）、拆后
  回落触碰绿、豁免件触碰不红、扫描面外触碰路径忽略；
  逃生口 [skip-touch-split] 声明放行、范围不可解析跳过；
- 行数口径：CRLF 与 LF 同权（换行符计数）；
- --generate：成员资格不扩（存量超线文件不入册）、数值只紧不松（上涨拒绝）；
- --baseline-monotonic：对照 base 版本加行/升数红、删行/降数放行；
  base 处无基线文件（闸门 adoption 前区间）跳过放行、base 引用不可解析红；
- base 解析：--base > GITHUB_EVENT_BEFORE（全零视同缺失）> HEAD~1；
- 负控制（防恒绿）：真实仓状态基线与树一致，检查器子进程实跑退出 0；
  红路径已由上方合成漂移用例钉死。
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_file_length_baseline", SCRIPTS / "check_file_length_baseline.py"
    )
    assert spec is not None
    assert spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


# ── 测试文件判别 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("rel", "expected_test"),
    [
        ("kernel/crates/api/src/server.rs", False),
        ("kernel/crates/api/src/server/tests.rs", True),
        ("kernel/crates/api/src/plugin_watcher_test.rs", True),
        ("kernel/crates/engine/benches/loop_bench.rs", True),
        ("plugins/shared/approval/server.py", False),
        ("plugins/shared/approval/test_server.py", True),
        ("plugins/shared/approval/server_test.py", True),
        ("plugins/shared/approval/conftest.py", True),
        ("tests/test_demo.py", True),
        ("frontend/src/components/chat/ChatInput.tsx", False),
        ("frontend/src/components/chat/ChatInput.test.tsx", True),
        ("frontend/src/stores/pipeline.spec.ts", True),
        ("frontend/src/stores/__tests__/pipeline.ts", True),
        ("electron/main.ts", False),
    ],
)
def test_test_file_detection(rel: str, expected_test: bool):
    assert mod.is_test_file(rel) is expected_test


# ── 新文件档（1500 / 3000，无基线可入）──────────────────────────────


def test_new_nontest_over_1500_red_under_green():
    files = {"a/new_service.py": 1600}
    hard, soft, ledger = mod.check(files, {}, {}, frozenset({"a/new_service.py"}))
    assert len(hard) == 1
    assert "新文件硬闸 1500" in hard[0]
    assert len(soft) == 1  # 软提醒全类别报表：>1000 同时可见
    assert ledger == []


def test_new_nontest_1499_green_with_soft():
    hard, soft, ledger = mod.check({"a/new_service.py": 1499}, {}, {}, frozenset({"a/new_service.py"}))
    assert hard == []
    assert len(soft) == 1
    assert "1499" in soft[0]
    assert ledger == []


def test_new_test_file_over_3000_red():
    hard, _, _ = mod.check({"a/mod_test.rs": 3100}, {}, {}, frozenset({"a/mod_test.rs"}))
    assert len(hard) == 1
    assert "新文件硬闸 3000" in hard[0]


def test_new_test_file_2900_green_with_soft():
    hard, soft, ledger = mod.check({"a/mod_test.rs": 2900}, {}, {}, frozenset({"a/mod_test.rs"}))
    assert hard == []
    assert len(soft) == 1
    assert "2900" in soft[0]
    assert ledger == []


def test_new_file_ignores_baseline_protection():
    """基线条目保护不了新文件：超 1500 仍红 + 账目冲突。"""
    files = {"a/giant.py": 2600}
    hard, _, ledger = mod.check(files, {"a/giant.py": 2600}, {}, frozenset({"a/giant.py"}))
    assert len(hard) == 1
    assert "新文件硬闸 1500" in hard[0]
    assert len(ledger) == 1
    assert "删除该条目" in ledger[0]


# ── 存量未入册档（触线 2500 / 5000）─────────────────────────────────


def test_existing_unregistered_touch_line_red():
    hard, _, ledger = mod.check({"a/service.py": 2501}, {}, {}, frozenset())
    assert len(hard) == 1
    assert "存量触线 2500" in hard[0]
    assert ledger == []


@pytest.mark.parametrize("lines", [2500, 2499])
def test_existing_unregistered_below_line_green(lines: int):
    hard, soft, ledger = mod.check({"a/service.py": lines}, {}, {}, frozenset())
    assert hard == []
    assert ledger == []
    assert len(soft) == 1  # >1000 软提醒可见


def test_existing_test_file_touch_line_red():
    hard, _, _ = mod.check({"a/tests.rs": 5001}, {}, {}, frozenset())
    assert len(hard) == 1
    assert "存量触线 5000" in hard[0]


# ── 冻结件档（天花板 = 入册行数）────────────────────────────────────


def test_frozen_over_ceiling_red():
    hard, _, _ = mod.check({"a/giant.py": 2650}, {"a/giant.py": 2600}, {}, frozenset())
    assert len(hard) == 1
    assert "2650" in hard[0]
    assert "2600" in hard[0]


def test_frozen_at_ceiling_green():
    hard, _, ledger = mod.check({"a/giant.py": 2600}, {"a/giant.py": 2600}, {}, frozenset())
    assert hard == []
    assert ledger == []


@pytest.mark.parametrize("dropped_to", [2500, 900])
def test_frozen_dropped_below_line_forces_entry_removal(dropped_to: int):
    """降到线内（含贴线）→ 红并提示删除条目（机械强制收紧）。"""
    hard, _, ledger = mod.check({"a/giant.py": dropped_to}, {"a/giant.py": 2600}, {}, frozenset())
    assert hard == []
    assert len(ledger) == 1
    assert "删除条目" in ledger[0]


def test_deleted_file_forces_entry_removal():
    hard, _, ledger = mod.check({}, {"a/gone.py": 2600}, {}, frozenset())
    assert hard == []
    assert len(ledger) == 1
    assert "删除该条目" in ledger[0]


# ── 软提醒与豁免 ─────────────────────────────────────────────────────


def test_soft_lines_by_file_type():
    """非测试 >1000 / 测试 >2000 进软提醒；线内不报。"""
    files = {"a/service.py": 1001, "a/tests.rs": 2001, "a/small.py": 500}
    _, soft, ledger = mod.check(files, {}, {}, frozenset())
    assert len(soft) == 2
    assert ledger == []


def test_exempt_file_skipped():
    hard, soft, ledger = mod.check({"a/gen.py": 20000}, {}, {"a/gen.py": "生成物"}, frozenset())
    assert hard == []
    assert soft == []
    assert ledger == []


def test_exempt_missing_reason_red():
    _, _, ledger = mod.check({"a/gen.py": 20000}, {}, {"a/gen.py": ""}, frozenset())
    assert any("缺归因理由" in x for x in ledger)


def test_exempt_stale_path_red():
    _, _, ledger = mod.check({"a/live.py": 200}, {}, {"a/gone.py": "理由"}, frozenset())
    assert any("已不在扫描面" in x for x in ledger)


def test_baseline_exempt_conflict_red():
    _, _, ledger = mod.check({"a/gen.py": 20000}, {"a/gen.py": 20000}, {"a/gen.py": "生成物"}, frozenset())
    assert any("冲突" in x for x in ledger)


def test_line_count_crlf_lf_same_weight(tmp_path: Path):
    crlf = tmp_path / "crlf.txt"
    lf = tmp_path / "lf.txt"
    crlf.write_bytes(b"a\r\nb\r\nc\r\n")
    lf.write_bytes(b"a\nb\nc\n")
    assert mod.count_lines(crlf) == mod.count_lines(lf) == 3


# ── 文件解析（fail-closed 传递）──────────────────────────────────────


def test_parse_exemptions_reason_required(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "exemptions.txt"
    f.write_text("a/gen.py  # 生成物\nb/ bad.py\n", encoding="utf-8")
    monkeypatch.setattr(mod, "EXEMPTIONS_FILE", f)
    got = mod.parse_exemptions()
    assert got == {"a/gen.py": "生成物", "b/ bad.py": ""}


def test_parse_baseline_format_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    f = tmp_path / "baseline.txt"
    f.write_text("a/giant.py 1600\n# 注释\nb/x.rs abc\n", encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", f)
    with pytest.raises(SystemExit, match="格式非法"):
        mod.parse_baseline()


# ── --generate：成员不扩、数值只紧不松 ──────────────────────────────


def test_generate_refuses_to_loosen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a/giant.py 2600\n", encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", baseline)
    assert mod.generate({"a/giant.py": 2700}, {}, frozenset()) == 1
    assert baseline.read_text(encoding="utf-8").strip().splitlines()[-1] == "a/giant.py 2600"


def test_generate_never_absorbs_over_line_strays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """存量触线文件不许 --generate 吸收入册（触线必须拆或缩）。"""
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a/giant.py 2600\n", encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", baseline)
    files = {"a/giant.py": 2600, "b/stray.py": 2501}
    assert mod.generate(files, {}, frozenset()) == 0
    out = baseline.read_text(encoding="utf-8")
    assert "a/giant.py 2600" in out
    assert "b/stray.py" not in out


def test_generate_adoption_snapshot_from_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """空基线 = adoption 时刻：按存量线生成快照，新文件不入册。"""
    baseline = tmp_path / "baseline.txt"
    monkeypatch.setattr(mod, "BASELINE_FILE", baseline)
    files = {"a/giant.py": 2600, "b/new.py": 1600, "c/tests.rs": 5001}
    assert mod.generate(files, {}, frozenset({"b/new.py"})) == 0
    out = baseline.read_text(encoding="utf-8")
    assert "a/giant.py 2600" in out
    assert "b/new.py" not in out  # 新文件无基线可入
    assert "c/tests.rs 5001" in out  # 测试档按存量线 5000 判定


def test_generate_keeps_or_tightens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    baseline = tmp_path / "baseline.txt"
    baseline.write_text("a/giant.py 2600\nb/gone.py 3000\n", encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", baseline)
    files = {"a/giant.py": 2550, "b/gone.py": 3000, "c/new.py": 999}
    assert mod.generate(files, {}, frozenset()) == 0
    out = baseline.read_text(encoding="utf-8")
    assert "a/giant.py 2550" in out  # 收紧
    assert "b/gone.py 3000" in out  # 仍超线保留
    assert "c/new.py" not in out  # 线内不入基线


# ── --baseline-monotonic：账本棘轮 ──────────────────────────────────


def _git_in_root(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True, encoding="utf-8", check=True
    )
    return proc.stdout


def test_baseline_monotonic_red_on_added_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """相对 base 版本加行 = 红。"""
    old = tmp_path / "old.txt"
    old.write_text("# 仅注释\n", encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", tmp_path / "cur.txt")
    (tmp_path / "cur.txt").write_text("a/giant.py 2600\n", encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "subprocess",
        _FakeSubprocess(show_text=old.read_text(encoding="utf-8")),
    )
    assert mod.baseline_monotonic("FAKEBASE") == 1


def test_baseline_monotonic_green_on_tighten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    old = tmp_path / "old.txt"
    old.write_text("a/giant.py 2600\nb/gone.py 3000\n", encoding="utf-8")
    monkeypatch.setattr(mod, "BASELINE_FILE", tmp_path / "cur.txt")
    (tmp_path / "cur.txt").write_text("a/giant.py 2550\n", encoding="utf-8")
    monkeypatch.setattr(mod, "subprocess", _FakeSubprocess(show_text=old.read_text(encoding="utf-8")))
    assert mod.baseline_monotonic("FAKEBASE") == 0


class _FakeSubprocess:
    """替身 subprocess：支撑 baseline_monotonic 的 rev-parse/cat-file/git show 调用面。"""

    def __init__(self, show_text: str, rev_ok: bool = True, path_exists: bool = True):
        self._show_text = show_text
        self._rev_ok = rev_ok
        self._path_exists = path_exists

    def run(self, cmd, **_kwargs):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(cmd, 0 if self._rev_ok else 1, stdout="", stderr="")
        if cmd[:3] == ["git", "cat-file", "-e"]:
            return subprocess.CompletedProcess(cmd, 0 if self._path_exists else 1, stdout="", stderr="")
        if cmd[:2] == ["git", "show"] and len(cmd) >= 3 and cmd[2].endswith("file-length-baseline.txt"):
            return subprocess.CompletedProcess(cmd, 0, stdout=self._show_text, stderr="")
        raise AssertionError(f"未预期的子进程调用: {cmd}")


def test_baseline_monotonic_skip_when_base_predates_baseline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """base 处基线文件尚不存在（闸门 adoption 前区间）= 无对照面跳过，放行。"""
    monkeypatch.setattr(mod, "BASELINE_FILE", tmp_path / "cur.txt")
    (tmp_path / "cur.txt").write_text("a/giant.py 2600\n", encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "subprocess",
        _FakeSubprocess(show_text="", path_exists=False),
    )
    assert mod.baseline_monotonic("FAKEBASE") == 0


def test_baseline_monotonic_red_on_unresolvable_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """base 引用不可解析 = fail-closed 红（不与「无基线文件跳过」混淆）。"""
    monkeypatch.setattr(mod, "BASELINE_FILE", tmp_path / "cur.txt")
    (tmp_path / "cur.txt").write_text("a/giant.py 2600\n", encoding="utf-8")
    monkeypatch.setattr(
        mod,
        "subprocess",
        _FakeSubprocess(show_text="", rev_ok=False),
    )
    assert mod.baseline_monotonic("NOSUCHREF") == 1


# ── base 解析 ────────────────────────────────────────────────────────


def test_resolve_base_priority(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GITHUB_EVENT_BEFORE", "abc123")
    assert mod.resolve_base(None) == "abc123"
    assert mod.resolve_base("direct") == "direct"
    monkeypatch.setenv("GITHUB_EVENT_BEFORE", "0000000000000000000000000000000000000000")
    assert mod.resolve_base(None) == "HEAD~1"  # 全零 sha（新分支推送）回退
    monkeypatch.delenv("GITHUB_EVENT_BEFORE")
    assert mod.resolve_base(None) == "HEAD~1"


# ── 触碰即拆（第五维：触碰 × 超线 ⇒ 红；触碰拆分线非测试/测试同线 5000）──


def test_touched_over_split_line_red():
    """超触碰拆分线（5000）冻结件被触碰 ⇒ 红，违规消息按拍板话术。"""
    files = {"a/giant.py": 5500}
    baseline = {"a/giant.py": 5500}
    hard, soft, ledger = mod.check(files, baseline, {}, frozenset(), frozenset({"a/giant.py"}))
    assert len(hard) == 1
    assert hard[0].startswith("触碰即拆：文件 a/giant.py 行数 5500 超触线值 5000")
    assert "本次改动须同刀按职责拆分或缩行" in hard[0]
    assert len(soft) == 1  # 软提醒全类别报表不受影响
    assert ledger == []


@pytest.mark.parametrize(
    ("rel", "lines"),
    [("a/giant.py", 5500), ("a/tests.rs", 5500)],  # 非测试/测试同线 5000
)
def test_touched_split_line_same_for_test_and_nontest(rel: str, lines: int):
    files = {rel: lines}
    baseline = {rel: lines}
    hard, _, _ = mod.check(files, baseline, {}, frozenset(), frozenset({rel}))
    assert len(hard) == 1
    assert f"触碰即拆：文件 {rel} 行数 {lines} 超触线值 5000" in hard[0]


@pytest.mark.parametrize("lines", [3222, 5000])
def test_touched_gray_band_not_red(lines: int):
    """2500~5000 灰带（含贴线 5000）被触碰不红：server.rs/agentos-kernel.rs
    形态的装配/引导件放行，职责判据交评审执法。"""
    files = {"a/giant.py": lines}
    baseline = {"a/giant.py": lines}
    hard, _, ledger = mod.check(files, baseline, {}, frozenset(), frozenset({"a/giant.py"}))
    assert hard == []
    assert ledger == []


def test_untouched_over_line_no_touch_red():
    """超线未触碰：触碰维度不红（现状保持，四档各管各的）。"""
    baseline = {"a/giant.py": 5500}
    hard, _, _ = mod.check({"a/giant.py": 5500}, baseline, {}, frozenset(), frozenset())
    assert hard == []


@pytest.mark.parametrize("lines", [2501, 5200])
def test_untouched_stray_red_by_existing_tier_only(lines: int):
    """存量触线未触碰件仍由四档红，但不得出现触碰即拆消息（维度不串）。"""
    hard, _, _ = mod.check({"a/stray.py": lines}, {}, {}, frozenset(), frozenset())
    assert len(hard) == 1
    assert not any("触碰即拆" in x for x in hard)


@pytest.mark.parametrize("dropped_to", [5000, 4999])
def test_touched_after_split_back_in_line_green(dropped_to: int):
    """拆后回落触碰拆分线内（含贴线）再触碰 ⇒ 绿（同刀拆分即泄压）。

    基线条目保留（5500 天花板）、行数回落：四档绿（≤天花板）+ 触碰绿（≤5000）。
    """
    files = {"a/giant.py": dropped_to}
    baseline = {"a/giant.py": 5500}
    hard, _, ledger = mod.check(files, baseline, {}, frozenset(), frozenset({"a/giant.py"}))
    assert hard == []
    assert ledger == []


def test_touched_after_full_split_below_line_prompts_entry_removal():
    """拆到成长触线（2500）以下再触碰：触碰绿，账目红提示删基线条目（机械强制收紧）。"""
    files = {"a/giant.py": 2400}
    baseline = {"a/giant.py": 5500}
    hard, _, ledger = mod.check(files, baseline, {}, frozenset(), frozenset({"a/giant.py"}))
    assert hard == []
    assert len(ledger) == 1
    assert "删除条目" in ledger[0]


def test_touched_exempt_file_not_red():
    """豁免阀先查：显式豁免件（带归因）触碰不红。"""
    files = {"a/gen.py": 20000}
    hard, soft, ledger = mod.check(files, {}, {"a/gen.py": "生成物"}, frozenset(), frozenset({"a/gen.py"}))
    assert hard == []
    assert soft == []
    assert ledger == []


def test_touched_paths_outside_scan_ignored():
    """触碰集中的删除文件/非扫描面路径（文档配置）不参与触碰判定。"""
    files = {"a/live.py": 100}
    hard, _, _ = mod.check(files, {}, {}, frozenset(), frozenset({"a/gone.py", "docs/x.md", "a/live.py"}))
    assert hard == []


# ── 触碰范围解析（复用 check_diff_coverage 机制 + 逃生口）────────────


def test_skip_touch_marker_value():
    """逃生口标记与拍板形态一致（[skip-diff-cov] 同款设计）。"""
    assert mod.SKIP_TOUCH_MARKER == "[skip-touch-split]"


def test_resolve_touched_returns_diff_names(monkeypatch: pytest.MonkeyPatch):
    captured: dict[str, object] = {}

    def fake_resolve(ns, tag="[diff-cov]"):
        captured["range"] = ns.range
        captured["base"] = ns.base
        captured["tag"] = tag
        return ("AAA", "BBB", "merge-base(origin/x, HEAD)")

    monkeypatch.setattr(mod, "resolve_range", fake_resolve)
    monkeypatch.setattr(mod, "range_has_skip_marker", lambda _a, _b, _marker: False)
    monkeypatch.setattr(mod, "_git_diff_names", lambda _args: {"a/touched.py"})

    touched, desc = mod.resolve_touched("AAA..BBB", None)
    assert touched == frozenset({"a/touched.py"})
    assert desc == "merge-base(origin/x, HEAD)"
    assert captured["range"] == "AAA..BBB"
    assert captured["tag"] == "[touch-split]"  # 消费方标签换本闸


def test_resolve_touched_skip_marker_declared(monkeypatch: pytest.MonkeyPatch):
    """逃生口：range 内 commit 声明 [skip-touch-split] ⇒ 空触碰集放行并注明。"""
    seen_markers: list[str] = []

    def fake_marker(a: str, b: str, marker: str) -> bool:
        seen_markers.append(marker)
        return True

    monkeypatch.setattr(mod, "resolve_range", lambda _ns, **_kw: ("AAA", "BBB", "--range A..B"))
    monkeypatch.setattr(mod, "range_has_skip_marker", fake_marker)

    touched, desc = mod.resolve_touched(None, "main")
    assert touched == frozenset()
    assert "已声明" in desc
    assert seen_markers == [mod.SKIP_TOUCH_MARKER]  # 查的是触碰逃生口，不是 diff-cov 的


def test_resolve_touched_unresolvable_range_skips(monkeypatch: pytest.MonkeyPatch):
    """范围不可解析（根提交/无 HEAD^）= 空触碰集跳过（同 diff-coverage 放行口径）。"""
    monkeypatch.setattr(mod, "resolve_range", lambda _ns, **_kw: None)
    touched, desc = mod.resolve_touched(None, None)
    assert touched == frozenset()
    assert "未执行触碰检查" in desc


# ── 负控制（防恒绿假闸）──────────────────────────────────────────────


def test_real_repo_gate_green():
    """真实仓负控制：基线与当前树一致时子进程实跑必须退出 0。

    本用例所在提交的基线由 --generate 实测生成；若本用例红，说明有人
    改了超线文件没走棘轮（拆分后须 --generate 收紧基线，或入豁免并归因）。
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_file_length_baseline.py")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
        check=False,
    )
    assert proc.returncode == 0, f"文件长度闸在真实仓红（须走棘轮流程）：\n{proc.stdout}\n{proc.stderr}"
