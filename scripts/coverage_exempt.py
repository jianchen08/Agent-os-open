#!/usr/bin/env python
"""覆盖率豁免重型套件——单一名单点（DSH coverage-exempt-heavy-suites 适配）。

机制：插件测试车道拆两个并行 gate，全部测试仍然执行，只有重型套件不再交插桩税：

- **插桩 gate**（run_gates.py 的 `plugins-coverage`）：跑 BASE_TEST_PATHS，
  豁免套件经 ``--ignore`` 从收集中剔除，其余照常 ``--cov=plugins`` +
  ``--cov-fail-under=50``，承担全部阈值证明。
- **无插桩 gate**（`plugins-heavy`）：positional 参数恰好只跑豁免套件，
  不加 ``--cov``——正确性信号一点不缩水，只是不再被覆盖率插桩拖慢。

两条命令的参数都由本模块单点构造（instrumented_args / heavy_paths），
插桩侧与免插桩侧不可能漂移；CI 与本地经由 run_gates.py 调用同一构造。

**名单由阈值自动守护（misconfiguration fails loud）**：
若某个豁免套件实际在 pytest 进程内独家覆盖了某被度量文件（--cov=plugins），
把它豁免出去会让插桩 gate 当场跌破 fail-under 而红——名单错误无法静默通过，
不依赖人工维护名单的正确性。

**成员资格约定**（新增豁免条目必须逐项对账，随名单同文件维护）：
豁免套件在 pytest 进程内执行的每个被度量文件（[tool.coverage.run]
source=plugins），都必须已由其他非豁免套件覆盖，或本就不在阈值口径内；
其子进程执行的部分天然不被父进程 coverage 度量，无需对账。

用法：
    python scripts/coverage_exempt.py --check     # 配对校验（plugins-coverage-pairing 门禁）
    python scripts/coverage_exempt.py --print     # 打印两条 gate 的 pytest 参数
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 插件体系测试的完整路径清单（原 ci.yml python-plugins-test 内联列表的唯一来源，
# 迁移至此使插桩/免插桩两侧共用一个基集）。
BASE_TEST_PATHS: list[str] = [
    "plugins/test_system_plugins.py",
    "plugins/shared/system/llm/",
    "plugins/shared/system/tasks/",
    "plugins/shared/system/test_migration_batch3.py",
    "plugins/shared/tools/task/",
    "plugins/shared/tools/tests/",
    "plugins/shared/tools/builtin_tools/tests/",
    "tests/plugins/",
    "tests/suites/plugins/",
    "tests/channels/",
    # 门禁脚本单测（2026-08-20 覆盖率棘轮门禁批次：check_diff_coverage /
    # check_python_coverage_baseline / check_frontend_baseline 的解析器测试）
    "tests/gates/",
    "tests/test_security_check_allow_priority.py",
    "tests/test_security_check_isolation.py",
    "tests/test_track_cost_update_event.py",
    "tests/test_process_watchdog_integration.py",
    "tests/test_isolation_docker_timeout.py",
    "tests/test_isolation_container_self_heal.py",
    # P1-lite 白名单扩容试点（2026-08-16 本地逐文件实跑通过、且在全车道
    # 共跑上下文通过后纳入；test_security_check_soft_block_loop 单跑绿、与
    # test_security_check_isolation 双文件共跑也绿（conftest 裸模块逐出钩子
    # 已修复该对冲突），但全车道共跑仍 7 红（mock.patch 对裸模块名的事后
    # 解析命中车道内其他插件的同名模块），未纳入——迁移债）。
    # 同批未纳入复核（2026-08-16 单跑实测）：host_mode 1 红（危险工具双轨
    # 判定）、per_round 9 红、signature 2 红、workspace_mount 1 红（需真实
    # docker 且容器名冲突）、docker_recheck/io_error/l1_main_agent/
    # namespace_desync 单跑收集即 ImportError（pipeline 依赖车道 conftest）。
    "tests/test_isolation_checkpoint_security.py",
    "tests/test_isolation_concurrent_create.py",
    "tests/test_isolation_docker_provider_injection.py",
    "tests/test_isolation_io_error_self_heal.py",
    "tests/test_isolation_prune_throttle.py",
    "tests/test_isolation_sandbox.py",
    "tests/test_isolation_skills_copy.py",
    # 2026-08-21 覆盖率批次：既有绿灯测试接线进插桩车道（此前 @ci: none-local
    # 未接车道，目标模块整体不进覆盖面：python_packager/server.py（sidecar-only
    # 缺进程内导入）、download/tool.py、triggers_ext、monitoring 等）。
    # 全部目录 2026-08-21 本地单跑绿后接入。
    "plugins/shared/system/python_packager/",
    "plugins/shared/system/monitoring/",
    "plugins/shared/tools/download/",
    "plugins/shared/tools/triggers_ext/",
    "plugins/shared/tools/test_workspace_aware.py",
]

# 外部依赖 marker 过滤（requires_api/requires_redis/requires_db/requires_bwrap
# 标记已于 2026-08-25 清理——全仓零使用；原过滤表达式随之清空，两侧 gate
# 的 -m 参数与无过滤等价，保留该参数形态以防未来新增外部依赖标记时单点加回）。
MARKER_FILTER = ""


@dataclass(frozen=True)
class ExemptSuite:
    """一个豁免套件：positional 路径 + 逐项对账记录。

    新增条目要求：填齐 measured_in_process / covered_by 两个字段完成对账——
    说明它在 pytest 进程内执行了哪些被度量文件、这些文件的覆盖由谁接住。
    对账不成立的条目会让插桩 gate 跌破阈值而红（自动守护）。
    """

    path: str
    profile: str  # 为什么重（重型画像）
    measured_in_process: str  # 进程内执行的被度量代码对账
    covered_by: str  # 覆盖由谁接住


EXEMPT_SUITES: list[ExemptSuite] = [
    ExemptSuite(
        path="tests/plugins/test_plugin_smoke_matrix.py",
        profile=(
            "94 插件全量冒烟矩阵：85 个 Python sidecar 插件逐个以子进程加载"
            "（cwd=插件目录，与生产 sidecar 语义一致）+ 8 external_mcp + 1 native，"
            "每个参数化用例一次 Python 子进程启动 + 插件全量 import"
        ),
        measured_in_process=(
            "无——插件代码全部在探针子进程（plugin_probe.py）内执行，"
            "父进程 coverage 测不到子进程；测试文件自身仅 import json/os/subprocess/pytest"
        ),
        covered_by=(
            "各插件的进程内单测（tests/plugins/{system,input,output,shared}/ 与"
            " plugins/shared/**/test_*.py，均留在插桩 gate 内）+ 免插桩并行 gate 本身的红绿"
        ),
    ),
]


def heavy_paths() -> list[str]:
    """无插桩 gate 的 positional 路径：恰好等于豁免名单。"""
    return [s.path for s in EXEMPT_SUITES]


def instrumented_args() -> list[str]:
    """插桩 gate 的 pytest 参数：基集 + --ignore 豁免套件 + 公共过滤。"""
    args = list(BASE_TEST_PATHS)
    args.append("-m")
    args.append(MARKER_FILTER)
    args.append("--ignore=tests/manual")
    for p in heavy_paths():
        args.append(f"--ignore={p}")
    return args


def heavy_args() -> list[str]:
    """无插桩 gate 的 pytest 参数：豁免名单 + 公共过滤（无 --cov）。"""
    args = list(heavy_paths())
    args.append("-m")
    args.append(MARKER_FILTER)
    return args


def check() -> int:
    """配对校验（廉价、静态）：文件存在、豁免 ⊆ 基集树、参数不重叠。"""
    problems: list[str] = []

    for p in heavy_paths():
        path = ROOT / p
        if not path.exists():
            problems.append(f"豁免套件不存在: {p}")
            continue
        # 豁免路径必须被基集覆盖（基集中的某条目是其祖先）
        covered = any(
            p == base or p.startswith(base.rstrip("/\\") + "/") or p.startswith(base.rstrip("/\\") + "\\")
            for base in BASE_TEST_PATHS
        )
        if not covered:
            problems.append(f"豁免套件 {p} 不在 BASE_TEST_PATHS 覆盖范围内（免插桩 gate 会跑到基集之外的测试）")

    for base in BASE_TEST_PATHS:
        if not (ROOT / base).exists():
            problems.append(f"基集路径不存在: {base}")

    # 配对不变量：插桩侧剔除的 --ignore 名单 ≡ 免插桩侧 positional 名单
    ignores = sorted(
        a.removeprefix("--ignore=")
        for a in instrumented_args()
        if a.startswith("--ignore=") and a != "--ignore=tests/manual"
    )
    if ignores != sorted(heavy_paths()):
        problems.append(f"--ignore 名单与 positional 名单不一致: {ignores} != {sorted(heavy_paths())}")

    if problems:
        print("coverage_exempt: 配对校验失败：", file=sys.stderr)
        for item in problems:
            print(f"  - {item}", file=sys.stderr)
        return 1

    n_ignores = len(ignores)
    print(
        f"coverage_exempt: 配对校验通过——基集 {len(BASE_TEST_PATHS)} 条，豁免 {len(EXEMPT_SUITES)} 条"
        f"（--ignore {n_ignores} 条 = positional {len(heavy_paths())} 条）"
    )
    for s in EXEMPT_SUITES:
        print(f"  豁免: {s.path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mutex = parser.add_mutually_exclusive_group(required=True)
    mutex.add_argument("--check", action="store_true", help="配对校验（plugins-coverage-pairing 门禁）")
    mutex.add_argument("--print", dest="print_args", action="store_true", help="打印两条 gate 的 pytest 参数")
    ns = parser.parse_args()
    if ns.check:
        return check()
    print("插桩 gate（plugins-coverage）pytest 参数:")
    print("  " + " ".join(instrumented_args()))
    print("无插桩 gate（plugins-heavy）pytest 参数:")
    print("  " + " ".join(heavy_args()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
