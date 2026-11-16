#!/usr/bin/env python
"""装机版内核端口常量生成器——单一真值区 → 两侧 TS 生成物（E4 单源化）。

真值与依据（ADR 2026-09-20-packaged-kernel-port-default-9101）：装机版打包
内核默认监听端口与回源 host。全仓可执行硬编码只允许出现在本文件真值区；
两侧 TS 常量文件均为本脚本生成物（DO-NOT-EDIT）：

  - electron/packaged-kernel-port.generated.ts
  - frontend/src/constants/packagedKernelPort.generated.ts

消费形态（符号面不变）：
  - electron/kernel-manager.ts 取生成物 PACKAGED_KERNEL_PORT re-export 为
    KERNEL_DEFAULT_PORT（ADR 2026-09-16 的 spawn/探测/回源解析链零改动）；
  - frontend/src/constants/websocket.ts 以生成物拼 PACKAGED_KERNEL_WS_ORIGIN。

执法：scripts/check_kernel_port_sync.py = 生成一致性闸（重渲染与落盘文件
逐字节比对；生成幂等 = 绿）。改端口/主机只改下方真值区 → 跑本脚本重写
两侧 → 生成物连同 commit；只改真值忘再生成 → 闸红。

用法：
    python scripts/generate_port_constants.py           # 写出两侧生成物
    python scripts/generate_port_constants.py --check   # 只校验，漂移退出 1
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── 单一真值区（全仓唯一硬编码点；ADR 2026-09-20）─────────────────────
# host 钉 127.0.0.1 回环：回源主机属同刀契约面（原闸同口径，host 变化=显式改真值）。
PACKAGED_KERNEL_HOST = "127.0.0.1"
PACKAGED_KERNEL_PORT = 9101

# ── 生成物登记面（符号搬移时同刀改这里）───────────────────────────────
ELECTRON_TS = ROOT / "electron" / "packaged-kernel-port.generated.ts"
FRONTEND_TS = ROOT / "frontend" / "src" / "constants" / "packagedKernelPort.generated.ts"

_MARKER = "*** DO NOT EDIT — GENERATED FILE ***"

_HEADER = f"""\
/**
 * {_MARKER}
 *
 * 装机版内核端口常量（ADR 2026-09-20-packaged-kernel-port-default-9101）。
 * 由 scripts/generate_port_constants.py 单一真值区生成；手改必被
 * scripts/check_kernel_port_sync.py（重渲染逐字节一致闸）打回。
 * 改端口/主机 = 改生成器真值区 → python scripts/generate_port_constants.py
 * → 两侧生成物随动，连同本文件一起 commit。
 */
"""

_BODY = """\

/** 打包件内核回源主机（钉回环；回源主机属同刀契约面） */
export const PACKAGED_KERNEL_HOST = {q}{host}{q}{semi}

/** 打包件内核默认监听端口（与 dev 栈默认 9100 错峰，两套环境默认并存） */
export const PACKAGED_KERNEL_PORT = {port}{semi}
"""


@dataclasses.dataclass(frozen=True)
class _Style:
    """生成物代码风格（按消费侧工程约定：electron 双引号带分号，frontend 单引号无分号）。"""

    quote: str
    semi: str


_STYLES: dict[str, _Style] = {
    "electron": _Style(quote='"', semi=";"),
    "frontend": _Style(quote="'", semi=""),
}


def _validate(host: str, port: int) -> None:
    """真值合法性（fail-closed）：非法真值拒绝渲染，绝不产出坏生成物。"""
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError(f"端口非法: {port!r}（须 1..65535 整数）")
    if not host or "://" in host or "/" in host:
        raise ValueError(f"host 非法: {host!r}（须裸主机名/IP，不带 scheme/路径）")


def render_ts_for(host: str, port: int, style: str) -> str:
    """按风格渲染生成物正文（纯函数；同真值同输出 = 生成确定性）。"""
    _validate(host, port)
    st = _STYLES[style]
    return _HEADER + _BODY.format(q=st.quote, semi=st.semi, host=host, port=port)


def write_outputs(electron_ts: Path = ELECTRON_TS, frontend_ts: Path = FRONTEND_TS) -> None:
    """写出两侧生成物（LF 字节；幂等——同真值重复写出零 diff）。"""
    for path, style in ((electron_ts, "electron"), (frontend_ts, "frontend")):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(render_ts_for(PACKAGED_KERNEL_HOST, PACKAGED_KERNEL_PORT, style))


def check_files(electron_ts: Path = ELECTRON_TS, frontend_ts: Path = FRONTEND_TS) -> int:
    """生成一致性检查：落盘文件与按侧风格重渲染逐字节比对（读侧按通用换行
    归一，CRLF 检出任无谓红）；任一侧漂移/缺失 → 1 并给出再生成指引
    （fail-closed）。"""
    red = False
    for path, style in ((electron_ts, "electron"), (frontend_ts, "frontend")):
        expected = render_ts_for(PACKAGED_KERNEL_HOST, PACKAGED_KERNEL_PORT, style)
        try:
            actual = path.read_text(encoding="utf-8")
        except OSError:
            print(f"[kernel-port-sync] FAIL: 生成物缺失: {path}")
            red = True
            continue
        if actual != expected:
            print(f"[kernel-port-sync] FAIL: 生成物与真值重渲染不一致: {path}")
            print(
                "[kernel-port-sync] 手改生成物 / 改真值未再生成均属漂移；"
                "跑 python scripts/generate_port_constants.py 重写后连同 commit。"
            )
            red = True
    if red:
        return 1
    print(f"[kernel-port-sync] OK: 两侧生成物 ≡ 真值重渲染 (host={PACKAGED_KERNEL_HOST} port={PACKAGED_KERNEL_PORT})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="装机版内核端口常量生成器（单一真值 → 两侧 TS 生成物）")
    parser.add_argument("--check", action="store_true", help="只校验不写盘；漂移退出 1（闸用）")
    args = parser.parse_args(argv)
    if args.check:
        return check_files()
    write_outputs()
    print(f"[kernel-port-sync] 已生成: {ELECTRON_TS}")
    print(f"[kernel-port-sync] 已生成: {FRONTEND_TS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
