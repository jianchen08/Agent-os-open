#!/usr/bin/env python3
# @feature: G3 复现车道脚手架（内存治理 ADR 2026-10-19 §7 执行器） | @ci: scripts-lint
"""内核内存风暴复现车道：插件配置写入风暴驱动 + memstats 观测采样。

设计真值：docs/decisions/2026-10-19-memory-governor-and-process-pooling.md（§7
复现车道判据）+ docs/working/内核commit爆涨与整机卡顿_诊断修复方案_20261010.md
（§1.5 教训：良性量级合成负载从未复现过爆涨——本车道对准生产风暴形态：对内核
监听的插件配置目录做高频微改，驱动 pull 路径 is_host_stale → 宿主组重建链路，
同时定时采样 /api/v1/system/memstats 的 committed/abandoned 序列落 CSV）。

车道流程（完整步骤）：

  1. 启动内核（脚本自身绝不拉起内核——装机侧动作先问用户）：
       - dev 形态：仓内 launcher 正常拉起，业务面 :9100（--base-url 默认值）。
       - 装机形态：electron 拉起，业务面 :9101（--base-url http://127.0.0.1:9101）；
         动装机环境前必须先获用户确认。
       - 鉴权：设 AGENTOS_ADMIN_PASSWORD 环境变量（脚本只读 env；连接安全铁律：
         口令恒读 .env/env，禁历史值）。
       - 对照轮（A/B）：基线轮不设任何候选开关；对照轮的开关 env 以当轮被验刀
         的任务书为准（如 G3b 自适应清扫节奏落地后的开关）。
  2. 确认 --target-dir 指向内核实际监听的插件配置根（风暴只改这里既有合法
     清单，退出时逐文件恢复原字节）：
       - dev：<repo>/user_root/plugins
       - 装机：%APPDATA%\\agentos\\plugins
  3. 自检（干跑：不发网络请求、不碰目标目录、不产 CSV；仅在系统临时目录
     自清理沙箱内校验参数组合与快照/还原逻辑）：
       python scripts/memory_storm_replay.py --self-test
  4. 跑风暴（ADR §7 受控负载 = 每分钟 100+ 次写入 x 30 分钟）：
       python scripts/memory_storm_replay.py --target-dir <插件配置根> \
           --writes-per-minute 120 --duration-min 30 --out storm_<轮名>.csv
  5. 判读 --out CSV（列：utc_ts/elapsed_s/committed_bytes/abandoned_pages/
     process_commit_bytes/process_rss_bytes/sample_error）。

判据（ADR §7 四条与验收归属；②③④为后续刀落地后的验收项，本脚本只供数
不判定）：
  (1) 整机不卡——内核 commit 峰值 < 预算：看 CSV process_commit_bytes 列峰值
      （10-12 ADR 口径）；committed_bytes/abandoned_pages 看 mimalloc 面。
  (2) 宿主冷启次数 = 0——G2（D1 进程池 + D2 成员级热换）落地后的验收项；
      本脚本不实现，人工对照 /api/v1/plugins/hosts 重建计数。
  (3) WARN 进通知中心——G1 Governor（D5）落地后的验收项；本脚本不实现。
  (4) 硬限 exit 75 排空重启——D4 Job 硬限落地后的验收项；本脚本不实现，
      表现 = 风暴中内核进程以退出码 75 退出并自动重启（采样线程对内核重启
      窗口容错：采样失败记 sample_error 列继续跑，不中断风暴）。

现场恢复边界：写前快照（每文件首次触碰时记原字节）、退出时（正常结束 /
异常 / Ctrl+C）逐文件还原原字节；进程被 SIGKILL 强杀时无法恢复，需手动
还原（快照不落盘，属车道数据非资产，丢失不抢救）。

纯标准库实现，零新依赖（2026-10-19 任务书约定）。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "http://127.0.0.1:9100"
DEFAULT_WRITES_PER_MINUTE = 100
DEFAULT_DURATION_MIN = 30
DEFAULT_MEMSTATS_INTERVAL_SEC = 5.0
DEFAULT_USERNAME = "admin"
MEMSTATS_PATH = "/api/v1/system/memstats"
LOGIN_PATH = "/api/v1/auth/login"
MANIFEST_NAME = "plugin.json"
HTTP_TIMEOUT_SEC = 5.0
CSV_FIELDS = [
    "utc_ts",
    "elapsed_s",
    "committed_bytes",
    "abandoned_pages",
    "process_commit_bytes",
    "process_rss_bytes",
    "sample_error",
]


class LaneError(Exception):
    """车道操作失败（登录/采样/响应形状），采样面按容错记录不中断风暴。"""


# ── 车道逻辑（纯函数/纯类，供 --self-test 与单测直接驱动） ──────────────────


def validate_args(
    target_dir: Path | None,
    writes_per_minute: int,
    duration_min: float,
    memstats_interval_sec: float,
    password: str | None,
) -> str | None:
    """参数组合校验：合法返回 None，非法返回错误说明（不抛异常便于测试）。"""
    if writes_per_minute <= 0:
        return f"--writes-per-minute 必须 > 0，当前 {writes_per_minute}"
    if duration_min <= 0:
        return f"--duration-min 必须 > 0，当前 {duration_min}"
    if memstats_interval_sec <= 0:
        return f"--memstats-interval-sec 必须 > 0，当前 {memstats_interval_sec}"
    if not password:
        return "缺管理员口令：设 AGENTOS_ADMIN_PASSWORD 环境变量（或 --password）"
    if target_dir is None:
        return "缺 --target-dir（内核实际监听的插件配置根）"
    if not target_dir.is_dir():
        return f"--target-dir 不存在或不是目录：{target_dir}"
    return None


def discover_manifests(target_dir: Path) -> list[Path]:
    """递归发现既有合法插件清单（plugin.json 且 JSON 可解析）；非法清单跳过。"""
    manifests: list[Path] = []
    for path in sorted(target_dir.rglob(MANIFEST_NAME)):
        try:
            json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        manifests.append(path)
    return manifests


def mutate_manifest_bytes(original: bytes, seq: int) -> bytes:
    """对清单做内容微改：JSON 语义等价、字节必不同（whitespace 重排）。

    语义等价保证清单始终合法（G2 校验照常通过，风暴驱动的就是合法配置的
    变更检测）；字节必不同保证 mtime/hash 两类过期判定都能命中。
    """
    obj = json.loads(original.decode("utf-8-sig"))
    out = json.dumps(obj, ensure_ascii=False, indent=2 if seq % 2 == 0 else None)
    mutated = (out if out.endswith("\n") else out + "\n").encode("utf-8")
    if mutated == original:  # 原文件恰好同序列化形态：补一个换行保证字节变化
        mutated = mutated + b"\n"
    return mutated


class StormReplay:
    """写入风暴执行器：轮转微改目标清单，逐文件保存原字节用于退出还原。"""

    def __init__(self, manifests: list[Path], seed: int) -> None:
        if not manifests:
            raise LaneError("清单列表为空，无法驱动风暴")
        rng = random.Random(seed)
        self._order = list(manifests)
        rng.shuffle(self._order)
        self._originals: dict[Path, bytes] = {}
        self.seq = 0

    def write_once(self) -> Path:
        """轮转写入下一份清单（首次触碰先快照原字节）。"""
        path = self._order[self.seq % len(self._order)]
        if path not in self._originals:
            self._originals[path] = path.read_bytes()
        path.write_bytes(mutate_manifest_bytes(self._originals[path], self.seq))
        self.seq += 1
        return path

    def restore_all(self) -> list[str]:
        """把所有触碰过的文件还原为原字节；返回逐条失败说明（空列表=全成）。"""
        errors: list[str] = []
        for path, original in self._originals.items():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(original)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        return errors

    def touched_count(self) -> int:
        """本次风暴触碰过的文件数（即退出时要还原的文件数）。"""
        return len(self._originals)


def _stat_or_blank(stats: dict[str, Any], key: str) -> Any:
    """读 memstats 字段：缺失/None 落空串（0 是有效观测值，不能吞）。"""
    value = stats.get(key, "")
    return "" if value is None else value


def build_csv_row(elapsed_s: float, stats: dict[str, Any], error: str) -> dict[str, Any]:
    """把 memstats JSON 快照折叠成 CSV 行（字段缺失/None 落空串，不猜值）。"""
    return {
        "utc_ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "elapsed_s": f"{elapsed_s:.1f}",
        "committed_bytes": _stat_or_blank(stats, "committed_bytes"),
        "abandoned_pages": _stat_or_blank(stats, "abandoned_pages"),
        "process_commit_bytes": _stat_or_blank(stats, "process_commit_bytes"),
        "process_rss_bytes": _stat_or_blank(stats, "process_rss_bytes"),
        "sample_error": error,
    }


# ── 内核观测面（HTTP，仅真实车道运行时使用） ────────────────────────────────


class KernelSampler:
    """memstats 采样器：登录换 Bearer token 后 GET；401 丢 token 待重登。"""

    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._token: str | None = None

    def login(self) -> None:
        body = json.dumps({"username": self._username, "password": self._password}).encode("utf-8")
        req = urllib.request.Request(
            self._base + LOGIN_PATH,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            token = json.loads(resp.read().decode("utf-8")).get("access_token")
        if not token:
            raise LaneError(f"登录响应缺 access_token：{self._base}{LOGIN_PATH}")
        self._token = str(token)

    def fetch_memstats(self) -> dict[str, Any]:
        """GET memstats；token 缺失先登录；401 丢 token 并按 LaneError 上抛。"""
        if self._token is None:
            self.login()
        assert self._token is not None
        req = urllib.request.Request(
            self._base + MEMSTATS_PATH,
            headers={"Authorization": f"Bearer {self._token}"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
                stats = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                self._token = None
            raise LaneError(f"memstats HTTP {exc.code}") from exc
        if not isinstance(stats, dict):
            raise LaneError("memstats 响应不是 JSON 对象")
        return stats

    def sample_once(self) -> tuple[dict[str, Any], str]:
        """采样一次：返回 (stats, 错误说明)；失败丢 token 重登后重试一次。"""
        first_error: str
        try:
            return self.fetch_memstats(), ""
        except (LaneError, urllib.error.URLError, OSError, ValueError) as exc:
            first_error = f"{type(exc).__name__}: {exc}"
            self._token = None
        try:
            return self.fetch_memstats(), ""
        except (LaneError, urllib.error.URLError, OSError, ValueError) as exc:
            return {}, f"{first_error} | retry {type(exc).__name__}: {exc}"


def sampler_loop(
    sampler: KernelSampler,
    interval_sec: float,
    csv_path: Path,
    start_monotonic: float,
    stop_event: threading.Event,
) -> int:
    """采样主循环：写一行 flush 一行（风暴被杀也保留已采序列）；返回采样数。"""
    count = 0
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        while not stop_event.is_set():
            stats, error = sampler.sample_once()
            writer.writerow(build_csv_row(time.monotonic() - start_monotonic, stats, error))
            fh.flush()
            count += 1
            stop_event.wait(interval_sec)
    return count


# ── 车道编排 ────────────────────────────────────────────────────────────────


def run_lane(args: argparse.Namespace) -> int:
    """真实车道：前置检查 → 起采样线程 → 跑风暴 → 还原现场。"""
    target_dir = Path(args.target_dir)
    password = args.password or os.environ.get("AGENTOS_ADMIN_PASSWORD")
    error = validate_args(
        target_dir,
        args.writes_per_minute,
        args.duration_min,
        args.memstats_interval_sec,
        password,
    )
    if error:
        print(f"[配置错误] {error}", file=sys.stderr)
        return 2
    assert password is not None

    manifests = discover_manifests(target_dir)
    if not manifests:
        print(f"[配置错误] 目标目录未发现任何合法 {MANIFEST_NAME}：{target_dir}", file=sys.stderr)
        return 2

    sampler = KernelSampler(args.base_url, args.username, password)
    try:
        sampler.login()
        sampler.fetch_memstats()
    except (LaneError, urllib.error.URLError, OSError, ValueError) as exc:
        print(
            f"[前置失败] 内核观测面不可用（{args.base_url}{MEMSTATS_PATH}）：{exc}\n"
            "脚本不会拉起内核（装机侧动作先问用户）；请先确认内核已启动与口令后重试。",
            file=sys.stderr,
        )
        return 2

    csv_path = Path(args.out)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    storm = StormReplay(manifests, args.seed)
    stop_event = threading.Event()
    start = time.monotonic()
    sampler_thread = threading.Thread(
        target=sampler_loop,
        args=(sampler, args.memstats_interval_sec, csv_path, start, stop_event),
        name="memstats-sampler",
        daemon=True,
    )
    sampler_thread.start()
    print(
        f"[风暴开始] 目标={target_dir} 清单数={len(manifests)} "
        f"速率={args.writes_per_minute}/min 时长={args.duration_min}min "
        f"采样间隔={args.memstats_interval_sec}s 观测CSV={csv_path}"
    )
    exit_code = 0
    try:
        interval_sec = 60.0 / args.writes_per_minute
        deadline = start + args.duration_min * 60
        next_report = start + 60.0
        while time.monotonic() < deadline:
            write_started = time.monotonic()
            storm.write_once()
            time.sleep(max(0.0, interval_sec - (time.monotonic() - write_started)))
            if time.monotonic() >= next_report:
                print(f"[进度] 已写 {storm.seq} 次 / {time.monotonic() - start:.0f}s")
                next_report += 60.0
        print(f"[风暴结束] 共写入 {storm.seq} 次")
    except KeyboardInterrupt:
        print("[中断] 收到 Ctrl+C，还原现场后退出", file=sys.stderr)
        exit_code = 130
    finally:
        stop_event.set()
        sampler_thread.join(timeout=args.memstats_interval_sec * 2 + HTTP_TIMEOUT_SEC)
        errors = storm.restore_all()
        if errors:
            exit_code = exit_code or 1
            print(f"[还原失败] {len(errors)} 个文件未还原成功（需手动检查）：", file=sys.stderr)
            for line in errors:
                print(f"  - {line}", file=sys.stderr)
        else:
            print(f"[现场还原] {storm.touched_count()} 个触碰文件已恢复原字节")
    return exit_code


# ── 自检（干跑：不发网络请求、不碰目标目录、不产 CSV） ──────────────────────


def run_self_test() -> int:
    """自检：校验参数组合与快照/还原逻辑，全部通过返回 0。"""
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"——{detail}" if detail and not ok else ""))
        if not ok:
            failures.append(name)

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "plugins"
        target.mkdir()
        (target / "p1").mkdir()
        (target / "p1" / MANIFEST_NAME).write_text('{"id": "p1"}', encoding="utf-8")

        # 1. 参数组合校验：非法组合逐条拒绝，合法组合放行
        check("缺口令拒绝", validate_args(target, 100, 30, 5.0, None) is not None)
        check("缺目标目录拒绝", validate_args(None, 100, 30, 5.0, "pw") is not None)
        check("目标目录不存在拒绝", validate_args(Path(tmp) / "nope", 100, 30, 5.0, "pw") is not None)
        check("写入速率 0 拒绝", validate_args(target, 0, 30, 5.0, "pw") is not None)
        check("时长 0 拒绝", validate_args(target, 100, 0, 5.0, "pw") is not None)
        check("采样间隔 0 拒绝", validate_args(target, 100, 30, 0.0, "pw") is not None)
        check("合法组合放行", validate_args(target, 100, 30, 5.0, "pw") is None)

        # 2. 清单发现：坏 JSON 跳过、嵌套目录收编、结果与预期集合精确相等
        (target / "broken").mkdir()
        (target / "broken" / MANIFEST_NAME).write_text("not json {", encoding="utf-8")
        (target / "deep" / "nested").mkdir(parents=True)
        (target / "deep" / "nested" / MANIFEST_NAME).write_text('{"id": "deep"}', encoding="utf-8")
        expected = sorted([target / "deep" / "nested" / MANIFEST_NAME, target / "p1" / MANIFEST_NAME])
        check("清单发现精确匹配", discover_manifests(target) == expected)

        # 3. 微改语义：JSON 等价 + 字节必不同（多 seq 连续成立）
        originals = {p: p.read_bytes() for p in expected}
        sample = expected[0].read_bytes()
        semantic_ok = all(json.loads(mutate_manifest_bytes(sample, seq)) == json.loads(sample) for seq in range(6))
        byte_diff_ok = all(mutate_manifest_bytes(sample, seq) != sample for seq in range(6))
        check("微改 JSON 语义等价", semantic_ok)
        check("微改字节必不同", byte_diff_ok)

        # 4. 快照/还原闭环：风暴改字节 → 外部再篡改 → 还原回原字节
        storm = StormReplay(expected, seed=7)
        touched = storm.write_once()
        check("风暴写入改变字节", touched.read_bytes() != originals[touched])
        touched.write_bytes(b"externally clobbered")
        check("还原无失败报告", storm.restore_all() == [])
        check("还原恢复原字节", touched.read_bytes() == originals[touched])

        # 5. 强删重建：触碰过的文件被外部删除后，还原能重建原字节
        storm2 = StormReplay(expected, seed=7)
        touched2 = storm2.write_once()
        touched2.unlink()
        check("强删后还原无失败报告", storm2.restore_all() == [])
        check("删除文件被重建为原字节", touched2.exists() and touched2.read_bytes() == originals[touched2])

    # 6. CSV 行形状：字段顺序、None/缺失落空串、错误说明透传
    row = build_csv_row(1.5, {"committed_bytes": 100, "abandoned_pages": None}, "net down")
    check(
        "CSV 行形状",
        list(row) == CSV_FIELDS
        and row["committed_bytes"] == 100
        and row["abandoned_pages"] == ""
        and row["sample_error"] == "net down",
    )

    print(f"自检结果：{'全部通过' if not failures else str(len(failures)) + ' 项失败'}")
    return 0 if not failures else 1


# ── CLI ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="内核内存风暴复现车道（插件配置写入风暴 + memstats 采样）",
    )
    parser.add_argument("--self-test", action="store_true", help="干跑自检：不发请求不碰目标目录不产 CSV")
    parser.add_argument(
        "--target-dir",
        help="内核实际监听的插件配置根（dev=user_root/plugins，装机=%%APPDATA%%\\agentos\\plugins）",
    )
    parser.add_argument(
        "--writes-per-minute",
        type=int,
        default=DEFAULT_WRITES_PER_MINUTE,
        help=f"每分钟写入次数（默认 {DEFAULT_WRITES_PER_MINUTE}，ADR §7 要求 100+）",
    )
    parser.add_argument(
        "--duration-min", type=float, default=DEFAULT_DURATION_MIN, help=f"风暴时长分钟（默认 {DEFAULT_DURATION_MIN}）"
    )
    parser.add_argument("--seed", type=int, default=0, help="清单轮转顺序洗牌种子（默认 0）")
    parser.add_argument("--out", help="memstats 采样序列 CSV 输出路径")
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"内核业务面基址（默认 {DEFAULT_BASE_URL}，装机形态传 :9101）",
    )
    parser.add_argument("--username", default=DEFAULT_USERNAME, help=f"登录用户名（默认 {DEFAULT_USERNAME}）")
    parser.add_argument("--password", help="管理员口令（默认读 AGENTOS_ADMIN_PASSWORD 环境变量）")
    parser.add_argument(
        "--memstats-interval-sec",
        type=float,
        default=DEFAULT_MEMSTATS_INTERVAL_SEC,
        help=f"采样间隔秒（默认 {DEFAULT_MEMSTATS_INTERVAL_SEC}）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test:
        return run_self_test()
    if not args.out:
        print("[配置错误] 缺 --out（memstats 采样 CSV 输出路径）", file=sys.stderr)
        return 2
    return run_lane(args)


if __name__ == "__main__":
    sys.exit(main())
