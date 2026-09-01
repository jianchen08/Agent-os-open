#!/usr/bin/env python
"""mypy 基线锁：防止类型错误数增长。

机制：
- .github/mypy-baseline.txt 记录当前允许的 mypy 错误数上限（单个整数，"只减不增"）
- 新代码若让错误数增加 → CI 失败（拦截合并）
- 错误数减少 → 通过，但基线**不自动更新**（本地/CI 环境可能漂移，需 CI 验证后
  手动下调 .github/mypy-baseline.txt，见下方"基线维护"）

0.2 架构说明：原 0.1 的 ``src/`` 已迁移/删除，根 mypy 配置（pyproject.toml
``[tool.mypy]``）实际管辖的 Python 业务代码位于 ``plugins/shared``（平铺插件模块）。
本脚本因此检查 ``plugins/shared``，不再引用已不存在的 ``src/``。

为什么必须加 ``--explicit-package-bases``：
- 0.2 插件目录平铺，plugins/shared 下含 88 个同名 ``server.py``（每插件自成一目录、
  运行时靠 server.py 把自身目录插 sys.path 做裸名 import，无 ``__init__.py`` 包层级）。
- 若不加该标志，mypy 把所有 ``server.py`` 都解析为 ``server`` 模块 → "Duplicate module
  named 'server'" 阻断，**只报 1 个错就停止后续检查**，门禁形同虚设（基线恒为 1）。
- ``--explicit-package-bases`` 让 mypy 以目录名为 PEP 420 命名空间包基做解析，每个
  ``server.py`` 落到唯一模块路径，从而真正检查全部 ~417 个源文件、报出真实类型错误。

基线维护：
- 当前基线对应 mypy 2.2.0（uv.lock 锁定）+ py3.11 语义。CI 需用 ``uv run`` 跑本脚本
  以复现同一 mypy 版本，否则版本漂移会导致计数不稳。
- 修了 mypy 错误后：本地确认计数下降 → 提交时把 .github/mypy-baseline.txt 改成新计数。

用法（CI 或本地，建议在 uv 环境内）:
    uv run python scripts/check_mypy_baseline.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = ROOT / ".github" / "mypy-baseline.txt"


def count_mypy_errors() -> int:
    """运行 mypy 并返回错误数。

    目标为 0.2 根 pyproject 管辖的 ``plugins/shared``（取代已删除的 ``src/``）。

    ``--explicit-package-bases`` 是平铺插件架构的必需项（见模块 docstring）：没有它
    mypy 因 88 个同名 server.py 报 "Duplicate module" 阻断，只产出 1 个无意义错误。
    加上后 mypy 才会真正遍历全部源文件并产出真实类型错误计数。
    """
    result = subprocess.run(
        [
            "mypy",
            "plugins/shared",
            "--config-file",
            "pyproject.toml",
            "--no-incremental",
            "--explicit-package-bases",  # 平铺插件架构：消除同名 server.py 的 Duplicate module 阻断
            # （缓存后端由 pyproject sqlite_cache=false 治理：2.1.0 sqlite 默认开启，
            #  多进程并发写锁偶发 INTERNAL ERROR；--cache-dir=NONE 在 2.1.0 已失效）
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=False,
    )
    # mypy 输出里 count "error:" 行（每条错误行形如 "...: error: ... [code]"，
    # 汇总行 "Found N errors in M files" 不含冒号，不会被误计）
    output = result.stdout + result.stderr
    return sum(1 for line in output.splitlines() if "error:" in line)


def read_baseline() -> int:
    """读取基线文件。"""
    if not BASELINE_FILE.exists():
        return 0
    return int(BASELINE_FILE.read_text().strip())


def main() -> int:
    baseline = read_baseline()
    current = count_mypy_errors()

    print(f"基线: {baseline}")
    print(f"当前: {current}")

    if current > baseline:
        print(f"\n❌ mypy 错误数增加了 {current - baseline} 个（{baseline} → {current}）")
        print("新代码引入了类型错误。请修复，或在 .github/mypy-baseline.txt 调整基线（仅允许减少）。")
        return 1

    if current < baseline:
        print(f"\n✅ mypy 错误数减少了 {baseline - current} 个（{baseline} → {current}）")
        print("（基线不自动更新：本地与 CI 环境可能存在差异，请在 CI 验证后手动更新 .github/mypy-baseline.txt）")
        return 0

    print(f"\n✅ mypy 错误数与基线持平（{current}），无新增类型错误")
    return 0


if __name__ == "__main__":
    sys.exit(main())
