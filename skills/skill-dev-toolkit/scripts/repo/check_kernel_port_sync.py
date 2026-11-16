#!/usr/bin/env python
"""内核端口同步闸——生成一致性执法（E4 单源化后形态）。

单一真值与生成物都在 scripts/generate_port_constants.py：真值区（全仓唯一
可执行硬编码点）→ 渲染两侧 DO-NOT-EDIT 生成物：

  - electron/packaged-kernel-port.generated.ts   （消费方 kernel-manager.ts
    re-export 为 KERNEL_DEFAULT_PORT）
  - frontend/src/constants/packagedKernelPort.generated.ts
                                                 （消费方 websocket.ts 拼
                                                 PACKAGED_KERNEL_WS_ORIGIN）

本闸执法 = 重渲染与落盘生成物逐字节一致（生成幂等）：手改生成物 / 改真值
未重新生成 / 生成物缺失，任一即退出码 1 并给出再生成指引——fail-closed，
闸绝不允许退化成永绿假闸。历史形态（两处手写常量正则抽取比对，整改
K4，实锤见 docs/working/全仓独立审查整改方案_20261010.md §3-K4 与
docs/decisions/2026-09-20-packaged-kernel-port-default-9101.md）已被生成
单源取代。

用法：
    python scripts/check_kernel_port_sync.py    # 绿：退出 0
    # 红：漂移任一 → 退出 1，按指引跑 scripts/generate_port_constants.py 再 commit

门禁归属：run_gates.py 的 kernel-port-sync gate（fast），
CI 经 .github/workflows/ci.yml traceability-gate job 调度。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from generate_port_constants import ELECTRON_TS, FRONTEND_TS, check_files


def main() -> int:
    return check_files(ELECTRON_TS, FRONTEND_TS)


if __name__ == "__main__":
    sys.exit(main())
