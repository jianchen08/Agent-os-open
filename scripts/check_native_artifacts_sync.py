#!/usr/bin/env python3
"""native cdylib 与内核 exe 同源（构建先后）检查。

native FFI（对称借用协议）两侧必须同源编译：exe 晚于 dll = dll 可能用旧
native-sdk 布局编译，工具派发点位 SIGSEGV（2026-09-01/08-31 两次实证）。
dll mtime 早于 exe mtime 即过期（同批编译的硬链接 mtime 相同，天然通过）。

退出码：0 = 全部同源；1 = 存在过期 dll（打印重编命令）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGINS = REPO / "plugins" / "shared"
KERNEL_EXE = REPO / "kernel" / "target" / "release" / "agentos-kernel.exe"
KERNEL_BIN = REPO / "kernel" / "target" / "release" / "agentos-kernel"


def native_manifests() -> list[Path]:
    """遍历 plugins/shared 找声明 native.artifact 的 manifest（剪枝 node_modules/.venv）。"""
    out: list[Path] = []
    if not PLUGINS.is_dir():
        return out
    skip = {"node_modules", ".venv", "__pycache__", "target", ".git"}
    stack = [PLUGINS]
    while stack:
        d = stack.pop()
        try:
            for child in d.iterdir():
                if not child.is_dir():
                    continue
                if child.name in skip:
                    continue
                stack.append(child)
        except OSError:
            continue
        m = d / "plugin.json"
        if m.is_file():
            out.append(m)
    return out


def main() -> int:
    exe = KERNEL_EXE if KERNEL_EXE.exists() else KERNEL_BIN
    if not exe.exists():
        print("[native-sync] 内核可执行文件不存在，跳过检查（首次构建流程自带全量编译）")
        return 0
    exe_mtime = exe.stat().st_mtime
    # 同源口径：dll 的 ABI 只取决于 native-sdk 源码；exe 取决于全部 kernel 源码。
    # 各自与「自己依赖的源码最新变更时刻」比较，跨类别 mtime 差异不算异源
    # （exe 同源重编会让它晚于 dll，但不构成 dll 过期）。
    sdk_latest = latest_source_mtime(REPO / "kernel" / "crates" / "native-sdk")
    kernel_latest = latest_source_mtime(REPO / "kernel" / "crates")

    rc = 0
    stale: list[Path] = []
    checked = 0
    for manifest in native_manifests():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        artifact = (data.get("native") or {}).get("artifact")
        if not artifact:
            continue
        dll = manifest.parent / (artifact if artifact.endswith(".dll") else f"{artifact}.dll")
        checked += 1
        if not dll.exists() or dll.stat().st_mtime < sdk_latest:
            stale.append(dll)

    if stale:
        rc = 1
        print(f"[native-sync] ❌ {len(stale)} 个 cdylib 编译于 native-sdk 源码最近变更之前（异源 → SIGSEGV 风险）：")
        for dll in stale:
            rel = dll.relative_to(REPO) if dll.is_relative_to(REPO) else dll
            print(f"  - {rel}")
            src_dir = dll.parent / "target"
            if src_dir.is_dir():
                print(
                    f"    重编: cd {src_dir.parent} && cargo build --release && cp target/release/{dll.name} {dll.name}"
                )
    if exe_mtime < kernel_latest:
        rc = 1
        print(
            "[native-sync] ❌ 内核 exe 编译于 kernel 源码最近变更之前——需重编 exe（capability/invoker 逻辑含运行期修复）"
        )
    if rc == 0:
        print(f"[native-sync] OK：{checked} 个 native cdylib 与内核 exe 同源")
    else:
        print(
            "[native-sync] 全量同源重编: cd kernel && cargo build --release --bin agentos-kernel，再逐插件 cargo build --release"
        )
    return rc


def latest_source_mtime(crates: Path) -> float:
    """kernel/crates 下源码文件（rs/toml）的最新 mtime（构建产物 target 排除）。"""
    latest = 0.0
    if not crates.is_dir():
        return latest
    skip = {"target", "__pycache__", "node_modules"}
    stack = [crates]
    while stack:
        d = stack.pop()
        try:
            for child in d.iterdir():
                if child.is_dir():
                    if child.name not in skip:
                        stack.append(child)
                    continue
                if child.suffix in (".rs", ".toml"):
                    m = child.stat().st_mtime
                    latest = max(latest, m)
        except OSError:
            continue
    return latest


if __name__ == "__main__":
    sys.exit(main())
