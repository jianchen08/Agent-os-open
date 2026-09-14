# @feature: FP-0.2.二 内部模块 manifest | @ci: python-coverage
"""enhanced_search / builtin server 未覆盖分支补测（簇 J 覆盖率批次）。

靶单来自 HEAD 实测（coverage.xml 2026-09-13 为 d9d1f71c4 口径，之后
``0de43b745``（二进制闸）与 ``b88aed617``（墙钟预算）两次改动使行号漂移；
本文件按 HEAD 语义逐条钉行为）：

search_tool.py
- 单文件路径遍历（``is_file()`` 分支）：os.walk 对文件产出空迭代，显式构造
  单次遍历才能命中——含 filename 模式与 text 模式两路；
- ``search_path`` 经 workspace 判定后不存在 → 失败结果（不是空结果）；
- 剪枝生效路径（搜索根落在仓库根内，dirs 被 walk 循环剔除）与剪枝为空集
  两态（性质：prune 非空时被剔除目录零命中）；
- filename 模式：file_pattern 不匹配跳过、凭据文件跳过、max_results 截断；
- text 模式：file_pattern 不匹配跳过、凭据文件跳过、二进制命中/未命中、
  二进制文件体量标注、max_results 截断（行内与文件级两处早停）；
- 墙钟预算：timeout_seconds <= 0 时首个目录即超时 → 截断标注 + message；
- ``_binary_contains`` 读取失败（OSError）→ False。

server.py
- ``run()`` 委托 ``create_plugin().run()``（进程入口，替身钉委托契约）。

结构性不可达/环境依赖残留（逐条说明，勿硬凑）：
1. ``file_path.open("rb")`` 的 ``except OSError: continue``（HEAD 159-160）与
   text 分支 ``read_text`` 的 ``except OSError: continue``（HEAD 180-181）
   是两级不同窗口：前者锁首字节（sniff 读 64KB）即命中，后者须让前 64KB
   可读、锁位在 sniff 窗口之外（read_text 全量读失败）。两者均在本文件用
   msvcrt 字节锁实测覆盖；Linux CI 自动 skip（msvcrt 为 Windows 专有）。
   ``_binary_contains`` 的 ``except OSError: return False``（HEAD 56-57）同法。
2. ``if len(results) >= max_results`` 的文件级早停（HEAD 198-199）仅在
   ``max_results <= 0`` 边界可达：正常上限下任何一次 append 触顶都会在
   行级检查（196-197）先行返回、或二进制/文件名分支各自返回，进入新文件
   时 len(results) 恒小于 max。本文件用 ``max_results=0`` 钉该边界。
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agentos_builtin_tools.search_tool import (  # noqa: E402
    _binary_contains,
    enhanced_search,
)

_HAS_MSVCRT = os.name == "nt"


async def _search(**kwargs: Any) -> Any:
    return await enhanced_search(**kwargs)


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    """注入工作空间（相对路径锚定根）。"""
    root = tmp_path / "ws"
    root.mkdir()
    return root


# ══════════════════ 单文件路径（is_file 分支） ══════════════════


class TestSingleFilePath:
    async def test_text_mode_single_file_hits(self, ws: Path) -> None:
        """text 模式指向具体文件：显式单次遍历命中行（非空迭代）。"""
        target = ws / "solo.py"
        target.write_text("x = 1\nNEEDLE = 2\ny = 3\n", encoding="utf-8")

        result = await _search(query="needle", path=str(target), workspace=str(ws))

        assert result.success
        rows = result.output["results"]
        assert [r["line_number"] for r in rows] == [2]
        assert rows[0]["content"] == "NEEDLE = 2"

    async def test_filename_mode_single_file(self, ws: Path) -> None:
        """filename 模式指向具体文件：文件名入结果且行号为 0。"""
        target = ws / "report_needle.md"
        target.write_text("irrelevant", encoding="utf-8")

        result = await _search(
            query="report_needle", path=str(target), search_type="filename", workspace=str(ws)
        )

        assert result.success
        rows = result.output["results"]
        assert len(rows) == 1
        assert rows[0]["line_number"] == 0
        assert rows[0]["content"] == "report_needle.md"

    async def test_single_file_without_match_yields_empty(self, ws: Path) -> None:
        """性质：单文件无命中 → 空结果而非失败（成功与"找不到"可区分）。"""
        target = ws / "plain.txt"
        target.write_text("nothing relevant\n", encoding="utf-8")

        result = await _search(query="needle", path=str(target), workspace=str(ws))

        assert result.success
        assert result.output["results"] == []
        assert result.metadata.get("count") == 0


class TestPathNotFound:
    async def test_missing_path_rejected_with_name(self, ws: Path) -> None:
        """根内不存在的路径 → 失败且 error 带原路径（两态：文件与目录）。"""
        for missing in ("ghost.txt", "ghost_dir"):
            result = await _search(query="needle", path=missing, workspace=str(ws))
            assert result.success is False
            assert missing in (result.error or "")


# ══════════════════ 剪枝（prune）两态 ══════════════════


class TestPruneBranches:
    async def test_prune_active_skips_denied_dirs(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """剪枝非空：被剔除目录零命中，未被剔除目录照常命中。"""
        import repo_anchor

        prune_dir = ws / "vendor"
        kept_dir = ws / "src"
        prune_dir.mkdir()
        kept_dir.mkdir()
        (prune_dir / "a.txt").write_text("needle in vendor", encoding="utf-8")
        (kept_dir / "b.txt").write_text("needle in src", encoding="utf-8")

        monkeypatch.setattr(
            repo_anchor, "repo_walk_prune", lambda _root: {os.path.normcase(str(prune_dir))}
        )

        result = await _search(query="needle", path=str(ws), workspace=str(ws))

        assert result.success
        names = {Path(r["file_path"]).name for r in result.output["results"]}
        assert names == {"b.txt"}, "剪枝目录必须整体跳过"

    async def test_prune_empty_walks_everything(
        self, ws: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """剪枝空集（根在仓库外）：两目录都命中——性质上与剪枝态相反。"""
        import repo_anchor

        for name in ("vendor", "src"):
            d = ws / name
            d.mkdir()
            (d / f"{name}.txt").write_text(f"needle {name}", encoding="utf-8")

        monkeypatch.setattr(repo_anchor, "repo_walk_prune", lambda _root: set())

        result = await _search(query="needle", path=str(ws), workspace=str(ws))

        assert result.success
        names = {Path(r["file_path"]).name for r in result.output["results"]}
        assert names == {"vendor.txt", "src.txt"}


# ══════════════════ filename 模式分支 ══════════════════


class TestFilenameMode:
    async def test_pattern_filter_and_sensitive_skip(self, ws: Path) -> None:
        """file_pattern 不匹配跳过；凭据文件名不进结果（两组有区分度输入）。"""
        (ws / "hit.py").write_text("x", encoding="utf-8")
        (ws / "hit.md").write_text("x", encoding="utf-8")
        (ws / ".env").write_text("SECRET=1", encoding="utf-8")
        (ws / "hit_env").write_text("x", encoding="utf-8")

        result = await _search(
            query="hit", path=".", search_type="filename", file_pattern="*.py", workspace=str(ws)
        )

        assert result.success
        names = [Path(r["file_path"]).name for r in result.output["results"]]
        assert names == ["hit.py"], "非匹配模式与凭据文件都被跳过"

    async def test_max_results_truncates_filename_mode(self, ws: Path) -> None:
        """filename 模式 max_results 截断：返回恰 max_results 条且 truncated=True。"""
        for i in range(5):
            (ws / f"needle_{i}.txt").write_text("x", encoding="utf-8")

        result = await _search(
            query="needle", path=".", search_type="filename", max_results=2, workspace=str(ws)
        )

        assert result.success
        assert len(result.output["results"]) == 2
        assert result.metadata.get("truncated") is True

    async def test_underscore_matched_literally(self, ws: Path) -> None:
        """性质：filename 模式对 ``*`` 通配的 file_pattern 全收，命中数等于文件数。"""
        for i in range(3):
            (ws / f"f{i}.log").write_text("x", encoding="utf-8")

        result = await _search(
            query="f", path=".", search_type="filename", file_pattern="*.log", workspace=str(ws)
        )

        assert result.success
        assert len(result.output["results"]) == 3


# ══════════════════ text 模式分支 ══════════════════


class TestTextMode:
    async def test_pattern_and_sensitive_skips(self, ws: Path) -> None:
        """text 模式：非匹配 pattern 跳过、凭据文件内容不回传。"""
        (ws / "code.py").write_text("needle = 1\n", encoding="utf-8")
        (ws / "notes.md").write_text("needle = 2\n", encoding="utf-8")
        (ws / ".env").write_text("needle=secret\n", encoding="utf-8")

        result = await _search(
            query="needle", path=".", file_pattern="*.py", workspace=str(ws)
        )

        assert result.success
        names = {Path(r["file_path"]).name for r in result.output["results"]}
        assert names == {"code.py"}

        everything = await _search(query="needle", path=".", workspace=str(ws))
        all_names = {Path(r["file_path"]).name for r in everything.output["results"]}
        assert all_names == {"code.py", "notes.md"}, "凭据文件必须整体缺席"

    async def test_context_lines_window(self, ws: Path) -> None:
        """上下文窗口按 context_lines 收放（0 与 1 两组输入）。"""
        f = ws / "ctx.txt"
        f.write_text("l1\nl2\nNEEDLE\nl4\nl5\n", encoding="utf-8")

        tight = await _search(query="NEEDLE", path=".", context_lines=0, workspace=str(ws))
        wide = await _search(query="NEEDLE", path=".", context_lines=1, workspace=str(ws))

        assert tight.output["results"][0]["context_before"] == []
        assert tight.output["results"][0]["context_after"] == []
        assert wide.output["results"][0]["context_before"] == ["l2"]
        assert wide.output["results"][0]["context_after"] == ["l4"]

    async def test_max_results_truncates_text_mode(self, ws: Path) -> None:
        """text 模式行级截断：命中数受限且 truncated 标注。"""
        f = ws / "many.txt"
        f.write_text("\n".join(f"needle {i}" for i in range(10)), encoding="utf-8")

        result = await _search(query="needle", path=".", max_results=3, workspace=str(ws))

        assert result.success
        assert len(result.output["results"]) == 3
        assert result.metadata.get("truncated") is True

    async def test_multi_file_truncation_stops_walk(self, ws: Path) -> None:
        """文件级早停：第一文件即达上限时后续文件不再进入（性质：条目数=上限）。"""
        for i in range(4):
            (ws / f"bulk_{i}.txt").write_text("needle\nneedle\n", encoding="utf-8")

        result = await _search(query="needle", path=".", max_results=1, workspace=str(ws))

        assert result.success
        assert len(result.output["results"]) == 1

    async def test_zero_limit_file_level_early_stop(self, ws: Path) -> None:
        """``max_results=0`` 边界：无命中文件遍历后即在上限检查处早停。

        上限 0 时行级检查不可能先行触发（空结果列表），文件循环末尾的
        ``len(results) >= max_results`` 是唯一出口；性质上返回空结果且已截断。
        """
        (ws / "noisy.txt").write_text("nothing matches here\n", encoding="utf-8")

        result = await _search(query="needle", path=".", max_results=0, workspace=str(ws))

        assert result.success
        assert result.output["results"] == []
        assert result.metadata.get("truncated") is True


# ══════════════════ 二进制闸分支 ══════════════════


class TestBinaryBranches:
    async def test_binary_hit_and_scan_miss(self, ws: Path) -> None:
        """二进制命中报体量；扫描预算外命中不报（两组输入）。"""
        hit = ws / "has.bin"
        hit.write_bytes(b"\x00head needle tail\x00")
        miss = ws / "miss.bin"
        miss.write_bytes(b"\x00unrelated\x00")

        result = await _search(query="needle", path=".", workspace=str(ws))

        assert result.success
        rows = result.output["results"]
        assert len(rows) == 1
        assert str(hit.stat().st_size) in rows[0]["content"]
        assert "miss.bin" not in rows[0]["file_path"]

    async def test_binary_hit_early_stop_at_limit(self, ws: Path) -> None:
        """二进制命中触顶：返回恰 max_results 条并停止遍历（两组输入对照）。"""
        for i in range(3):
            (ws / f"hit{i}.bin").write_bytes(b"\x00needle payload\x00")

        result = await _search(query="needle", path=".", max_results=1, workspace=str(ws))

        assert result.success
        assert len(result.output["results"]) == 1
        assert result.metadata.get("truncated") is True

    async def test_binary_contains_budget_and_bytes(self, tmp_path: Path) -> None:
        """``_binary_contains`` 纯函数契约：字节命中 True / 未命中 False。"""
        import re

        blob = tmp_path / "payload.bin"
        blob.write_bytes(b"\x00\xffneedle\x00")

        assert _binary_contains(blob, re.compile("needle")) is True
        assert _binary_contains(blob, re.compile("absent")) is False

    async def test_binary_contains_missing_file_is_false(self, tmp_path: Path) -> None:
        """读取失败（OSError）→ False，不向上抛（扫描面 fail-safe）。"""
        import re

        assert _binary_contains(tmp_path / "ghost.bin", re.compile("x")) is False

    @pytest.mark.skipif(not _HAS_MSVCRT, reason="msvcrt 字节锁仅 Windows 可用")
    async def test_unreadable_text_file_skipped(self, ws: Path) -> None:
        """可 stat 但读失败的文本文件跳过（NUL 探测即 OSError 的窗口）。

        锁住首字节：``resolve()`` 成功而 ``read`` 抛 PermissionError，
        该文件整体跳过且不炸。
        """
        import msvcrt

        victim = ws / "locked.txt"
        victim.write_text("needle inside\n", encoding="utf-8")
        other = ws / "free.txt"
        other.write_text("needle outside\n", encoding="utf-8")

        fd = os.open(victim, os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            result = await _search(query="needle", path=".", workspace=str(ws))
        finally:
            os.close(fd)

        assert result.success
        names = {Path(r["file_path"]).name for r in result.output["results"]}
        assert names == {"free.txt"}, "读失败文件整体跳过，不进入结果"

    @pytest.mark.skipif(not _HAS_MSVCRT, reason="msvcrt 字节锁仅 Windows 可用")
    async def test_binary_contains_read_failure_is_false(self, tmp_path: Path) -> None:
        """``_binary_contains`` 的 OSError 分支：锁住字节后扫描读失败 → False。"""
        import msvcrt
        import re

        blob = tmp_path / "locked.bin"
        blob.write_bytes(b"\x00needle\x00" + b"pad" * 64)

        fd = os.open(blob, os.O_RDWR)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            assert _binary_contains(blob, re.compile("needle")) is False
        finally:
            os.close(fd)

    @pytest.mark.skipif(not _HAS_MSVCRT, reason="msvcrt 字节锁仅 Windows 可用")
    async def test_text_read_failure_beyond_sniff_window_skipped(self, ws: Path) -> None:
        """文本文件在 sniff 窗口之外不可读 → 整体跳过（read_text 抛 OSError）。

        NUL 探测读前 64KB 成功（判定为文本、进入内容展开），锁位在 64KB
        之后使 read_text 全量读失败——该文件跳过，同目录其他文件照常命中。
        """
        import msvcrt

        from agentos_builtin_tools.search_tool import _BINARY_SNIFF_BYTES

        victim = ws / "wide.txt"
        victim.write_bytes(
            b"needle-prefix\n" + b"x" * (_BINARY_SNIFF_BYTES + 16) + b"\nneedle-tail\n"
        )
        neighbour = ws / "ok.txt"
        neighbour.write_text("needle neighbour\n", encoding="utf-8")

        fd = os.open(victim, os.O_RDWR)
        try:
            os.lseek(fd, _BINARY_SNIFF_BYTES + 8, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            result = await _search(query="needle", path=".", workspace=str(ws))
        finally:
            os.close(fd)

        assert result.success
        names = {Path(r["file_path"]).name for r in result.output["results"]}
        assert names == {"ok.txt"}, "sniff 通过但全文读失败的文件必须整体跳过"


# ══════════════════ 墙钟预算 ══════════════════


class TestWallClockBudget:
    async def test_expired_budget_reports_truncation_message(self, ws: Path) -> None:
        """预算 <= 0：首个目录即判定超时，message 如实标注且 truncated=True。"""
        (ws / "a.txt").write_text("needle\n", encoding="utf-8")

        result = await _search(
            query="needle", path=".", timeout_seconds=-1.0, workspace=str(ws)
        )

        assert result.success
        assert result.metadata.get("truncated") is True
        assert "遍历超时" in (result.metadata.get("message") or "")

    async def test_generous_budget_returns_full_results(self, ws: Path) -> None:
        """性质：预算充足时不标注截断（与耗尽态相反）。"""
        (ws / "a.txt").write_text("needle\n", encoding="utf-8")

        result = await _search(
            query="needle", path=".", timeout_seconds=60.0, workspace=str(ws)
        )

        assert result.success
        assert result.metadata.get("truncated") is False
        assert not result.metadata.get("message")


# ══════════════════ os_walk_depth 深度限制 ══════════════════


class TestWalkDepth:
    async def test_max_depth_prunes_deep_levels(self, ws: Path) -> None:
        """max_depth 到达后不再下钻（浅层命中、深层零命中）。"""
        deep = ws / "d1" / "d2" / "d3"
        deep.mkdir(parents=True)
        (ws / "top.txt").write_text("needle top\n", encoding="utf-8")
        (deep / "bottom.txt").write_text("needle bottom\n", encoding="utf-8")

        result = await _search(query="needle", path=".", max_depth=1, workspace=str(ws))

        assert result.success
        names = {Path(r["file_path"]).name for r in result.output["results"]}
        assert "top.txt" in names
        assert "bottom.txt" not in names


# ══════════════════ builtin server.run 委托 ══════════════════


class TestBuiltinServerRun:
    def test_run_delegates_to_plugin_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``run()`` 的唯一职责：create_plugin().run()（入口等价于 SDK run）。"""
        server_path = _SRC / "agentos_builtin_tools" / "server.py"
        spec = importlib.util.spec_from_file_location("builtin_server_gap_under_test", server_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["builtin_server_gap_under_test"] = module
        try:
            spec.loader.exec_module(module)
            calls: list[str] = []

            class _StubPlugin:
                def run(self) -> None:
                    calls.append("run")

            monkeypatch.setattr(module, "create_plugin", lambda: _StubPlugin())

            module.run()
        finally:
            sys.modules.pop("builtin_server_gap_under_test", None)

        assert calls == ["run"]
