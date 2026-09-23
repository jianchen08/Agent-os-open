# @feature: FP-0.2.可观测性 日志轮转 | @ci: python-coverage
"""compress_kernel_logs 行为测试：临时目录 fixture 断言压缩/保留/活动日志保护。

覆盖：dry-run 零副作用 / --apply 压缩与内容完整性 / 边界年龄（恰 3d 不压、4d 压）/
活动日志（最新日期）与裸 kernel.log、kernel.liveness、陌生文件不碰 / 归档保留窗
（≤30d 留、>30d 删）/ 失败路径不留半成品且原文件在 / CLI 退出码。
"""

from __future__ import annotations

import gzip
import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location(
    "compress_kernel_logs", REPO / "scripts" / "compress_kernel_logs.py"
)
assert _SPEC is not None and _SPEC.loader is not None  # 同仓固定路径，装配必成功
K = importlib.util.module_from_spec(_SPEC)
sys.modules["compress_kernel_logs"] = K
_SPEC.loader.exec_module(K)


def d(offset_days: int, today: date) -> str:
    return (today - timedelta(days=offset_days)).isoformat()


@pytest.fixture()
def env(tmp_path):
    """fixture 日志目录 + UTC 今日；返回 (dir, today, 路径字典, 原始内容字典)。"""
    today = K.today_utc()
    log_dir = tmp_path / "logs"
    log_dir.mkdir()

    def make(name: str, content: bytes = b"") -> Path:
        p = log_dir / name
        p.write_bytes(content)
        return p

    paths = {
        "old": make(f"kernel.log.{d(10, today)}", b"x" * 4096),
        "boundary3": make(f"kernel.log.{d(3, today)}", b"b" * 128),
        "boundary4": make(f"kernel.log.{d(4, today)}", b"c" * 128),
        "recent": make(f"kernel.log.{d(1, today)}", b"r" * 128),
        "active": make(f"kernel.log.{d(0, today)}", b"a" * 128),
        "bare": make("kernel.log", b"bare" * 32),
        "liveness": make("kernel.liveness", b"v" * 8),
        "alien": make("kernel_stderr.log", b"s" * 16),
    }
    contents = {key: p.read_bytes() for key, p in paths.items()}
    return log_dir, today, paths, contents


def test_dry_run_changes_nothing(env):
    log_dir, today, paths, _contents = env
    stats = K.Stats()
    K.process_dir(log_dir, today, compress_age=3, retain_days=30, apply=False, stats=stats)
    for p in paths.values():
        assert p.exists(), f"dry-run 不得动原文件：{p}"
    assert not (log_dir / "archive").exists(), "dry-run 不得建归档目录"
    # 计划面：>3d 的 old+boundary4 入压缩计划；active/bare 等入跳过
    assert len(stats.compressed) == 2
    assert any("archive" in item for item in stats.compressed)


def test_apply_compresses_old_and_preserves_recent(env):
    log_dir, today, paths, contents = env
    archive = log_dir / "archive"
    stats = K.Stats()
    K.process_dir(log_dir, today, compress_age=3, retain_days=30, apply=True, stats=stats)

    # 超窗文件：压缩归档 + 原文件删除 + 内容逐字节一致
    for key in ("old", "boundary4"):
        original = paths[key]
        assert not original.exists(), f"{key} 原文件应已删除"
        gz = archive / (original.name + ".gz")
        assert gz.exists(), f"{key} 应有归档"
        with gzip.open(gz, "rb") as f:
            assert f.read() == contents[key], f"{key} 归档内容须与原文件逐字节一致"

    # 窗口内/活动/非目标文件：原样
    for key in ("boundary3", "recent", "active", "bare", "liveness", "alien"):
        assert paths[key].exists(), f"{key} 不得被触碰"
    assert not stats.failures


def test_active_log_is_newest_not_necessarily_today(env):
    """时钟回拨/异常场景：最新嵌入日期文件即使不是今天也必须跳过。"""
    log_dir, today, _paths, _contents = env
    future = log_dir / f"kernel.log.{d(-2, today)}"  # 嵌入日期在未来（时钟回拨）
    future.write_bytes(b"future")
    stats = K.Stats()
    K.process_dir(log_dir, today, compress_age=3, retain_days=30, apply=True, stats=stats)
    assert future.exists(), "最新日期文件（含未来日期）不得触碰"


def test_yesterday_newest_guarded_even_at_aggressive_window(env):
    """昨日「最新」文件是写入者可能持有的唯一历史窗口（轮转后当日首写前）：
    即便压缩窗压到 0 天也不得触碰；前天及更旧不受保护。"""
    log_dir, today, _paths, _contents = env
    for p in log_dir.iterdir():
        p.unlink()  # 清掉 fixture 其余文件，隔离本用例的日期布局
    yesterday = log_dir / f"kernel.log.{d(1, today)}"
    two_days_ago = log_dir / f"kernel.log.{d(2, today)}"
    yesterday.write_bytes(b"y")
    two_days_ago.write_bytes(b"t")

    stats = K.Stats()
    K.process_dir(log_dir, today, compress_age=0, retain_days=30, apply=True, stats=stats)
    assert yesterday.exists(), "昨日最新文件在激进窗口下仍受活动保护"
    assert not two_days_ago.exists(), "前天文件不受活动保护（可压缩）"


def test_retention_window_deletes_stale_keeps_fresh(env):
    log_dir, today, _paths, _contents = env
    archive = log_dir / "archive"
    archive.mkdir()
    stale = archive / f"kernel.log.{d(40, today)}.gz"
    fresh = archive / f"kernel.log.{d(10, today)}.gz"
    stale.write_bytes(b"stale")
    fresh.write_bytes(b"fresh")

    stats = K.Stats()
    K.process_dir(log_dir, today, compress_age=3, retain_days=30, apply=True, stats=stats)
    assert not stale.exists() and any(stale.name in x for x in stats.deleted_archives)
    assert fresh.exists(), "窗口内归档不得删除"


def test_failure_leaves_original_and_no_partial_archive(env, monkeypatch):
    log_dir, today, paths, _contents = env

    def boom(src: Path, dst_dir: Path) -> Path:
        raise OSError("模拟磁盘满")

    monkeypatch.setattr(K, "gzip_file", boom)
    stats = K.Stats()
    K.process_dir(log_dir, today, compress_age=3, retain_days=30, apply=True, stats=stats)
    assert paths["old"].exists(), "压缩失败时原文件必须保留（真相源）"
    assert not (log_dir / "archive" / (paths["old"].name + ".gz")).exists()
    assert len(stats.failures) == 2  # old + boundary4 两项各自失败留痕
    assert all("模拟磁盘满" in reason for _, reason in stats.failures)


def test_missing_dir_is_failure(env):
    stats = K.Stats()
    K.process_dir(Path("Z:/definitely/missing"), K.today_utc(), 3, 30, True, stats)
    assert len(stats.failures) == 1


def test_cli_exit_codes_and_multiple_dirs(env, tmp_path, capsys, monkeypatch):
    log_dir, today, _paths, _contents = env
    other = tmp_path / "other_logs"
    other.mkdir()
    (other / f"kernel.log.{d(9, today)}").write_bytes(b"o" * 64)

    # dry-run 缺省目录不指定 → 不报错退出 0（仓库 logs/ 真实存在）
    monkeypatch.setattr(sys, "argv", ["compress_kernel_logs.py"])
    assert K.main() == 0
    capsys.readouterr()

    # apply 双目录 → 成功退出 0，两目录各自归档
    monkeypatch.setattr(
        sys, "argv",
        ["compress_kernel_logs.py", "--apply", "--dir", str(log_dir), "--dir", str(other)],
    )
    assert K.main() == 0
    out = capsys.readouterr().out
    assert (other / "archive").is_dir()
    assert "DRY-RUN" not in out

    # 不存在的目录 → 退出 1
    monkeypatch.setattr(
        sys, "argv", ["compress_kernel_logs.py", "--apply", "--dir", str(tmp_path / "nope")]
    )
    assert K.main() == 1
    assert "目录不存在" in capsys.readouterr().err


def test_gzip_file_roundtrip_and_tmp_cleanup(tmp_path):
    src = tmp_path / "kernel.log.2026-01-01"
    src.write_bytes(b"payload" * 1000)
    dst_dir = tmp_path / "archive"
    dst_dir.mkdir()
    out = K.gzip_file(src, dst_dir)
    assert out.name == "kernel.log.2026-01-01.gz"
    assert not list(dst_dir.glob("*.tmp")), "临时文件必须清理"
    with gzip.open(out, "rb") as f:
        assert f.read() == src.read_bytes()
