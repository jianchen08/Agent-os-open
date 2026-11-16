#!/usr/bin/env python
"""移动壳 apiBase 漂移闸——声明面 vs 生成链一致性执法（D1①②，审查总册 P1-14②）。

真值链：capacitor.config.json plugins.ShellConfig.apiBase（declared face 声明
单点）→ app-mobile/scripts/syncweb.mjs 生成 www/shell-config.js + 注入
www/index.html 标签。执法核在本仓 syncweb 本体（--check 模式，单源渲染逻辑
零复制）：声明面缺失 / 生成物 apiBase 漂移（含 env 注入值 LINGXI_API_BASE
构建残留入库）/ 注入标签缺失，任一即退出码 1 并给再生成指引——fail-closed，
闸绝不允许退化成永绿假闸。

运行时侧配套（本闸执法在库面，运行时免疫在壳内）：MainActivity 换装的
ShellConfigWebViewClient 对 /shell-config.js 用声明面/sidecar 决策值现做现答
（ShellConfigResolver），生成物 apiBase 不再被运行时消费。

用法：
    python scripts/check_shell_config_sync.py    # 绿：退出 0
    # 红：漂移任一 → 退出 1，按指引跑 cd app-mobile && npm run syncweb 再
    # 按打包链惯例处置 www（deploy/build_mobile.sh 头注还原约定）

门禁归属：run_gates.py 的 shell-config-sync gate（fast），
CI 经 .github/workflows/ci.yml quality-gates job 调度。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SYNCWEB = Path(__file__).resolve().parent.parent / "app-mobile" / "scripts" / "syncweb.mjs"


def main() -> int:
    if not SYNCWEB.is_file():
        print(f"[shell-config-sync] FAIL syncweb 本体缺失: {SYNCWEB}", file=sys.stderr)
        return 1
    proc = subprocess.run(  # 退出码经 returncode 透传判定，非零不算异常
        ["node", str(SYNCWEB), "--check"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.stdout:
        print(proc.stdout, end="")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
