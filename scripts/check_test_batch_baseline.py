#!/usr/bin/env python
"""散落测试批次失败数基线锁：防止 pre-existing 失败数增长。

机制（与 check_frontend_baseline.py 同构）：
- .github/test-batch-baseline.txt 记录各批次允许的失败数上限
- 跑指定批次的 pytest，解析失败数
- 失败数 > 基线 → 退出码 1（CI 红，拦截合并）
- 失败数 ≤ 基线 → 退出码 0（CI 绿，允许持平，鼓励逐步修复后收紧基线）

用途（ci.yml test job 第 4/5 批次）：
  替换原先的 `|| true` 容错——既不让 pre-existing 失败误红 CI，
  又把"只许减不许增"的约束真正落地（原先 || true 完全不约束）。

用法：
    python scripts/check_test_batch_baseline.py --batch 4
    python scripts/check_test_batch_baseline.py --batch 5
    python scripts/check_test_batch_baseline.py --batch 4 --init   # 首次写入基线

DEBT: 基线值含 +1 余量防 CI 慢机 Timeout 抖动误红。ceiling=当前 batch4/5
  各取 真实失败数(2)+1=3，真实失败多为 pytest-timeout 打断的单测。upgrade=当
  pre-existing Timeout 失败被修复或测试稳定性提升后，运行 `--init` 收紧到真实值。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".github" / "test-batch-baseline.txt"

# ── 批次定义：与 ci.yml 原内联 pytest 调用保持一致 ──────────────────
# 每个批次记录 pytest 的位置参数 + --ignore 列表，由本脚本拼装命令。
# 这样 ci.yml 与脚本的测试范围单一来源，避免漂移。

BATCH_4_IGNORES = [
    "tests/test_cross_domain_discovery.py",
    "tests/test_directory_generator.py",
    "tests/test_memory_metrics.py",
    "tests/test_integration.py",
    "tests/test_pipeline_integration.py",
    "tests/test_external_project_e2e.py",
    "tests/test_new_project_e2e.py",
    "tests/test_rag_integration.py",
    "tests/test_external_chat.py",
    "tests/test_asr_service.py",
    "tests/test_media_provider.py",
    "tests/test_media_review.py",
    "tests/test_media_review_service.py",
    "tests/test_pgvector_store.py",
    "tests/test_isolation_container_self_heal.py",
    "tests/test_isolation_docker_timeout.py",
    "tests/test_isolation_prune_throttle.py",
]

BATCHES: dict[int, dict] = {
    4: {
        "paths": ["tests/test_*.py", "tests/test_external_tools/", "tests/integration/"],
        "ignores": BATCH_4_IGNORES,
    },
    5: {
        "paths": [
            "tests/suites/agent",
            "tests/suites/cli",
            "tests/suites/m6_plugins",
            "tests/suites/pipeline",
            "tests/suites/stage",
            "tests/suites/task",
            "tests/suites/tools",
            "tests/suites/websocket",
            "tests/suites/test_plugin_type_slot.py",
            "tests/suites/memory/",
            "tests/suites/plugins/",
        ],
        "ignores": [],
    },
}


def _expand_paths(paths: list[str]) -> list[str]:
    """展开路径中的 shell 通配符（*, ?, [）。

    本脚本用 subprocess.run 不经 shell 调用 pytest，而 ci.yml 原先是 shell
    调用（shell 会展开 glob）。为保持等价，这里手动展开：
    含通配符的路径用 glob 展开；无匹配则保留原样（让 pytest 报 collection
    error，而非静默收集 0 个测试导致假绿）。
    """
    expanded: list[str] = []
    for p in paths:
        if any(ch in p for ch in "*?["):
            # 用 Path.glob 展开；pattern 可能含目录前缀，拆分 root 与 pattern。
            pp = Path(p)
            matches = sorted(str(m) for m in pp.parent.glob(pp.name))
            expanded.extend(matches if matches else [p])
        else:
            expanded.append(p)
    return expanded


def build_pytest_cmd(batch: int, deselect_file: Path) -> list[str]:
    """拼装指定批次的 pytest 命令（与 ci.yml 原内联调用等价）。"""
    cfg = BATCHES[batch]
    cmd: list[str] = [sys.executable, "-m", "pytest", *_expand_paths(cfg["paths"])]
    for ig in cfg["ignores"]:
        cmd += ["--ignore", ig]
    cmd += [
        "--continue-on-collection-errors",
        "--timeout=30",
        "--timeout-method=thread",
        "--tb=line",
        "--no-header",
        "-q",
    ]
    # 与 ci.yml 一致：从 known-skipped-tests.txt 生成 --deselect
    if deselect_file.exists():
        for line in deselect_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            cmd += ["--deselect", stripped]
    return cmd


def count_failures(output: str) -> int:
    """从 pytest -q 输出解析失败数。

    优先用末尾总结行：
        3 failed, 145 passed, 12 skipped in 8.32s
    无总结行时（pytest 被 --timeout 打断、未输出 summary）回退到
    从 progress 字符 + Timeout 标记统计：
        - progress 行（只含 .Fsex 等）里的 F / E 各算 1 个失败
        - 每个 "+++++ Timeout +++++" 标记算 1 个失败（pytest-timeout 超时计为 failed）
    """
    # 优先：末尾总结行的 "N failed"
    matches = re.findall(r"(\d+)\s+failed", output)
    if matches:
        return int(matches[-1])

    # 回退：progress + Timeout 计数
    failed = 0
    for ln in output.splitlines():
        # Timeout 标记（pytest-timeout 的 --timeout 打断单个测试）
        if "+++++ Timeout +++++" in ln:
            failed += 1
            continue
        # progress 行：剥离空白后只含 pytest 进度字符（. F s E x r X）
        body = ln.replace(" ", "")
        body = re.sub(r"\+*\s*Timeout\s*\+*", "", body)
        if body and all(c in ".FsExrxX" for c in body):
            failed += body.count("F") + body.count("E")
    return failed


def read_baseline(batch: int) -> int:
    """读取指定批次的失败数基线。无文件返回 9999（占位，不收紧）。"""
    if not BASELINE_FILE.exists():
        return 9999
    key = f"batch{batch}_failures="
    for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith(key):
            return int(line.split("=", 1)[1].strip())
    return 9999


def write_baseline(batch: int, value: int) -> None:
    """更新指定批次的基线值（保留其他批次和注释）。"""
    lines = []
    key = f"batch{batch}_failures="
    found = False
    if BASELINE_FILE.exists():
        for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(key):
                lines.append(f"{key}{value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}{value}")
    BASELINE_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="散落测试批次失败数基线锁")
    parser.add_argument(
        "--batch",
        type=int,
        required=True,
        choices=[4, 5],
        help="批次号（4=tests/test_*.py+integration，5=tests/suites/*）",
    )
    parser.add_argument("--init", action="store_true", help="首次模式：把当前失败数写入基线（不比对）")
    args = parser.parse_args()

    deselect_file = ROOT / ".github" / "known-skipped-tests.txt"
    cmd = build_pytest_cmd(args.batch, deselect_file)
    print(f"━━━ 批次 {args.batch} 基线锁：运行 pytest ━━━")
    print("  命令:", " ".join(cmd[:6]), "...")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT, check=False)
    output = result.stdout + result.stderr
    # 打印输出供 CI 日志排查（截断过长输出）
    print(output[-3000:] if len(output) > 3000 else output)

    current = count_failures(output)
    baseline = read_baseline(args.batch)

    if args.init:
        write_baseline(args.batch, current)
        print(f"\n✅ --init：批次 {args.batch} 基线写入 batch{args.batch}_failures={current}")
        return 0

    print("\n         基线    当前")
    print(f"批次 {args.batch}:  {baseline:<6}  {current}")

    if current > baseline:
        print(f"\n❌ 失败数增加了（batch{args.batch} {baseline}→{current}）")
        print("请修复新增的失败，或在 .github/test-batch-baseline.txt 调整基线（仅允许减少）。")
        return 1

    if current < baseline:
        print(f"\n✅ 失败数减少了（batch{args.batch} {baseline}→{current}）")
        print("（基线不自动更新：本地与 CI 环境可能存在差异，请在 CI 验证后手动收紧基线）")
        return 0

    print("\n✅ 与基线持平，无新增失败")
    return 0


if __name__ == "__main__":
    sys.exit(main())
