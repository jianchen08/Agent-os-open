r"""内核日志压缩轮转（零内核改动）：tracing-appender daily 旧日志 gzip 归档。

背景：内核日志按天滚动写 `logs/kernel.log.YYYY-MM-DD`（纯 UTC 命名，
kernel/crates/api/src/bin/agentos-kernel.rs），不压缩——实测 ~37MB/天
（R238：147MB/4 天）。内核侧已带保留上限（`max_log_files(30)`），但窗口内
全是明文。本脚本把窗口内陈旧日志 gzip 到 `archive/` 子目录：

- **只认 `kernel.log.YYYY-MM-DD` 文件名**（严格日期后缀解析；`kernel.log`
  裸名、`kernel.liveness`、`kernel_stderr.log` 等一律不碰）；
- **活动日志不碰**：daily 轮转下写入者持有的句柄只可能是今天或昨天的文件
  （UTC 午夜轮转后、当日首写前仍持昨日句柄），故「最新嵌入日期文件且日期
  落在今日/昨日」恒跳过；再叠加年龄闸（默认 >3 天才压）双保险；
- 归档进 `archive/` 子目录：tracing-appender 的 max_log_files 清理只扫日志
  目录一层且按前缀计数，`archive/` 下的 `.gz` 对它不可见（不会被误删、
  不占 30 份明文额度）；归档自带的 30 天保留窗由本脚本负责；
- 写入安全：先写 `archive/*.gz.tmp` 临时文件 → 读回校验 uncompressed 字节数
  与原文件一致 → 原子改名 → 最后才删原文件；删原文件失败（句柄占用等）则
  回滚删 `.gz` 并告警，绝不留双份也绝不丢数据；
- 默认 dry-run（只报计划），`--apply` 才执行（对齐 bootstrap_plugin_envs 约定）。

用法：
  python scripts/compress_kernel_logs.py                 # dry-run，扫默认目录
  python scripts/compress_kernel_logs.py --apply         # 执行压缩+保留窗清理
  python scripts/compress_kernel_logs.py --apply \
      --dir "C:\Users\jc\AppData\Local\Programs\agent-os\resources\kernel\logs"
  python scripts/compress_kernel_logs.py --compress-age-days 7 --retain-days 14

自动触发：start_web_02.bat 在内核启动前对仓库 logs/ 跑 --apply；装机版可挂
Windows 计划任务（schtests）或手动执行（运维口径见 scripts/README.md）。
退出码：0 = 全部成功；1 = 存在失败项或目录不存在。
"""

from __future__ import annotations

import argparse
import dataclasses
import gzip
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# 严格匹配 tracing-appender 产物：kernel.log.YYYY-MM-DD（UTC 日期嵌入文件名）
LOG_NAME_RE = re.compile(r"^kernel\.log\.(\d{4}-\d{2}-\d{2})$")
ARCHIVE_DIR_NAME = "archive"


@dataclasses.dataclass
class Stats:
    compressed: list[str] = dataclasses.field(default_factory=list)
    retained: list[str] = dataclasses.field(default_factory=list)
    stale_archives: list[str] = dataclasses.field(default_factory=list)
    deleted_archives: list[str] = dataclasses.field(default_factory=list)
    skipped: list[str] = dataclasses.field(default_factory=list)
    failures: list[tuple[str, str]] = dataclasses.field(default_factory=list)


def default_log_dirs() -> list[Path]:
    """默认扫描目录：仓库 logs/（dev 内核 cwd 相对落盘面）。"""
    return [REPO / "logs"]


def today_utc() -> date:
    return datetime.now(timezone.utc).date()


def age_days(name_date: date, today: date) -> int:
    return (today - name_date).days


def gzip_file(src: Path, dst_dir: Path) -> Path:
    """流式 gzip 压缩 src → dst_dir/<name>.gz（临时文件+读回校验+原子改名）。

    返回最终 .gz 路径；任何失败不落半成品（临时文件就地清理），异常向上抛。
    """
    dst = dst_dir / (src.name + ".gz")
    tmp = dst_dir / (src.name + ".gz.tmp")
    original_size = src.stat().st_size
    try:
        with open(src, "rb") as fin, open(tmp, "wb") as fout:
            with gzip.GzipFile(filename=src.name, fileobj=fout, mode="wb") as gz:
                while chunk := fin.read(1024 * 1024):
                    gz.write(chunk)
        # 读回校验：uncompressed 字节数与原文件一致才承认归档成立
        with gzip.open(tmp, "rb") as check:
            verified = sum(len(c) for c in iter(lambda: check.read(1024 * 1024), b""))
        if verified != original_size:
            raise OSError(f"读回校验失败（{verified} != {original_size} 字节）")
        tmp.replace(dst)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return dst


def process_dir(log_dir: Path, today: date, compress_age: int, retain_days: int,
                apply: bool, stats: Stats) -> None:
    if not log_dir.is_dir():
        stats.failures.append((str(log_dir), "目录不存在"))
        return

    entries: list[tuple[date, Path]] = []
    for entry in log_dir.iterdir():
        m = LOG_NAME_RE.match(entry.name)
        if not m or not entry.is_file():
            continue
        try:
            name_date = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        entries.append((name_date, entry))

    newest = max((d for d, _ in entries), default=None)
    # 活动保护线：daily 轮转下写入者持有的句柄只可能是今天/昨天的文件
    # （UTC 午夜轮转后、当日首写前，写入者仍持昨日句柄）；更旧的「最新」文件
    # 无写入者可持有（写者活着就会落当日文件）。
    active_horizon = today - timedelta(days=1)
    for name_date, entry in sorted(entries):
        file_age = age_days(name_date, today)
        rel = str(entry)
        if name_date == newest and name_date >= active_horizon:
            stats.skipped.append(f"{rel}（活动日志，写入者持有句柄）")
            continue
        if file_age <= compress_age:
            stats.skipped.append(f"{rel}（年龄 {file_age}d ≤ {compress_age}d）")
            continue
        dst = log_dir / ARCHIVE_DIR_NAME / (entry.name + ".gz")
        if not apply:
            stats.compressed.append(f"{rel} → {dst}（年龄 {file_age}d）")
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            gzip_file(entry, dst.parent)
            entry.unlink()
            stats.compressed.append(f"{rel} → {dst}（年龄 {file_age}d）")
        except OSError as exc:
            stats.failures.append((rel, str(exc)))
            # 回滚半成品：原文件在 = 真相源，绝不留双份
            if dst.exists():
                try:
                    dst.unlink()
                except OSError:
                    stats.failures.append((str(dst), "回滚删除失败，请人工核查"))

    # 保留窗：archive/ 里超龄 .gz 删除（文件名同口径解析，解析不了不动）
    archive = log_dir / ARCHIVE_DIR_NAME
    if not archive.is_dir():
        return
    for gz in sorted(archive.glob("kernel.log.*.gz")):
        m = LOG_NAME_RE.match(gz.name[: -len(".gz")])
        if not m:
            continue
        try:
            name_date = date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if age_days(name_date, today) <= retain_days:
            stats.retained.append(str(gz))
            continue
        if apply:
            try:
                gz.unlink()
                stats.deleted_archives.append(str(gz))
            except OSError as exc:
                stats.failures.append((str(gz), str(exc)))
        else:
            stats.stale_archives.append(str(gz))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dir",
        action="append",
        default=None,
        help="日志目录（可重复）；缺省扫仓库 logs/。装机版示例见文档",
    )
    parser.add_argument(
        "--compress-age-days", type=int, default=3, help="超过该天数（UTC 口径）才压缩"
    )
    parser.add_argument(
        "--retain-days", type=int, default=30, help="归档 .gz 保留窗口（超龄删除）"
    )
    parser.add_argument("--apply", action="store_true", help="执行变更（默认 dry-run）")
    args = parser.parse_args()

    dirs = [Path(p) for p in args.dir] if args.dir else default_log_dirs()
    today = today_utc()
    stats = Stats()
    for log_dir in dirs:
        process_dir(log_dir, today, args.compress_age_days, args.retain_days,
                    args.apply, stats)

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[{mode}] 压缩窗口 >{args.compress_age_days}d，归档保留 {args.retain_days}d"
          f"（UTC 今日 {today.isoformat()}）")
    for item in stats.compressed:
        print(f"  压缩: {item}")
    for item in stats.stale_archives:
        print(f"  超龄归档: {item}（--apply 将删除）")
    for item in stats.deleted_archives:
        print(f"  清理: {item}")
    for item in stats.skipped:
        print(f"  跳过: {item}")
    for path, reason in stats.failures:
        print(f"  失败: {path}: {reason}", file=sys.stderr)
    print(
        f"完成：压缩 {len(stats.compressed)}，清理 {len(stats.deleted_archives)}，"
        f"跳过 {len(stats.skipped)}，失败 {len(stats.failures)}"
    )
    return 1 if stats.failures else 0


if __name__ == "__main__":
    sys.exit(main())
