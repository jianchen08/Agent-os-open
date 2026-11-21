#!/usr/bin/env python3
"""native cdylib 与内核 exe 同源（构建先后）检查，可选一键重编（--build）。

native FFI（对称借用协议）两侧必须同源编译：exe 晚于 dll = dll 可能用旧
native-sdk 布局编译，工具派发点位 SIGSEGV（2026-09-01/08-31 两次实证）。
产物与其依赖的源码比 mtime，早于即过期（同批编译的硬链接 mtime 相同，天然通过）。

**检查对象 = 运行面真身**：内核 NativePluginLoader 加载的是插件目录根那份
cdylib（manifest 相对路径），它必须同时满足两条：

1. 不早于 `kernel/crates/native-sdk` 源码（ABI 同源）；
2. 不早于**插件自身**源码（改了插件代码不重编 = 内核加载的是旧逻辑；
   2026-11-09 实测：tool_core 改了 messages.rs，旧口径只比 native-sdk，
   `--build` 直接放行，实际运行产物仍是旧代码）。

用法：
  python scripts/check_native_artifacts_sync.py            # 只检查
  python scripts/check_native_artifacts_sync.py --build    # 缺失/过期产物先重编再检查

--build 重编规则：插件目录带 Cargo.toml（源码在仓）→ cargo build --release
后把产物复制到插件目录根；无 Cargo.toml（如 native_test，产物只存在于源仓
之外）→ 跳过并警告，该插件由内核加载时另行报缺失，不阻塞其余插件。

产物命名与内核 NativePluginLoader::platform_artifact_name 对齐：
裸名按平台补全（Windows `X.dll` / macOS `libX.dylib` / Linux `libX.so`）。

退出码：0 = 全部同源；1 = 存在无法自动修复的过期产物或 exe 过期（打印指引）。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
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


def platform_artifact_name(artifact: str) -> str:
    """裸产物名按平台补全前缀/后缀（对齐 plugin-loader native_loader.rs）。"""
    if artifact.lower().endswith((".dll", ".so", ".dylib")):
        return artifact
    if os.name == "nt":
        return f"{artifact}.dll"
    if sys.platform == "darwin":
        return f"lib{artifact}.dylib"
    return f"lib{artifact}.so"


def build_artifact(plugin_dir: Path, artifact: str) -> Path | None:
    """在插件目录 cargo build --release 并把产物复制到目录根，返回产物路径。"""
    r = subprocess.run(
        ["cargo", "build", "--release"],
        cwd=plugin_dir,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if r.returncode != 0:
        tail = (r.stdout or "") + (r.stderr or "")
        print(tail[-1200:])
        return None
    product = plugin_dir / "target" / "release" / platform_artifact_name(artifact)
    if not product.exists():
        print(f"[native-sync] ❌ 构建成功但未找到产物 {product.name}（检查 crate lib 名与 artifact 声明是否一致）")
        return None
    dest = plugin_dir / product.name
    shutil.copy2(product, dest)
    return dest


def main() -> int:
    parser = argparse.ArgumentParser(description="native cdylib 与内核 exe 同源检查（--build 先重编缺失/过期产物）")
    parser.add_argument("--build", action="store_true", help="缺失/过期且有源码的产物先 cargo build --release 重编复制")
    args = parser.parse_args()

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
    stale: list[tuple[Path, bool, bool]] = []
    no_source: list[Path] = []
    checked = 0
    for manifest in native_manifests():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        artifact = (data.get("native") or {}).get("artifact")
        if not artifact:
            continue
        dll = manifest.parent / platform_artifact_name(artifact)
        checked += 1
        # 运行面真身 = 插件目录根这份 dll（内核 NativePluginLoader 按 manifest
        # 相对路径加载）：既不能早于 native-sdk（ABI 同源），也不能早于插件自身
        # 源码（改了自己不重编 = 跑的是旧逻辑，门禁说 OK 也是假的）。
        plugin_latest = latest_source_mtime(manifest.parent)
        abi_stale, source_stale = artifact_staleness(dll, sdk_latest, plugin_latest)
        if not (abi_stale or source_stale):
            continue
        if not (manifest.parent / "Cargo.toml").is_file():
            # 无源码可编（产物只存在于开发机）：跳过，不阻塞启动；
            # 插件加载时由内核按 NATIVE_ARTIFACT_NOT_FOUND 明确报错。
            no_source.append(dll)
            continue
        if args.build:
            rel = manifest.parent.relative_to(REPO) if manifest.parent.is_relative_to(REPO) else manifest.parent
            print(f"[native-sync] 重编 {dll.name}（{rel}）...")
            built = build_artifact(manifest.parent, artifact)
            if built is not None and not any(artifact_staleness(built, sdk_latest, plugin_latest)):
                continue
        stale.append((dll, abi_stale, source_stale))

    if no_source:
        print(f"[native-sync] ⚠ {len(no_source)} 个声明产物无仓内源码，跳过（插件加载时将报缺失）：")
        for dll in no_source:
            rel = dll.relative_to(REPO) if dll.is_relative_to(REPO) else dll
            print(f"  - {rel}")
    if stale:
        rc = 1
        print(
            f"[native-sync] ❌ {len(stale)} 个 cdylib 缺失/过期（运行面加载的就是插件目录这一份）："
        )
        for dll, abi_stale, source_stale in stale:
            rel = dll.relative_to(REPO) if dll.is_relative_to(REPO) else dll
            reasons = []
            if abi_stale:
                reasons.append("早于 native-sdk 源码（ABI 异源 → SIGSEGV 风险）")
            if source_stale:
                reasons.append("早于插件自身源码（跑的是旧逻辑）")
            print(f"  - {rel}（{'；'.join(reasons) or '缺失'}）")
    if exe_mtime < kernel_latest:
        rc = 1
        print(
            "[native-sync] ❌ 内核 exe 编译于 kernel 源码最近变更之前——需重编 exe（capability/invoker 逻辑含运行期修复）"
        )
    if rc == 0:
        suffix = f"；{len(no_source)} 个无源码跳过" if no_source else ""
        print(f"[native-sync] OK：{checked} 个 native cdylib 与内核 exe 同源{suffix}")
    else:
        print(
            "[native-sync] 修复: python scripts/check_native_artifacts_sync.py --build（cdylib）"
            " + cd kernel && cargo build --release --bin agentos-kernel（exe）"
        )
    return rc


def artifact_staleness(dll: Path, sdk_latest: float, plugin_latest: float) -> tuple[bool, bool]:
    """产物过期判定（纯函数）：返回 (ABI 过期, 插件源码过期)。

    - 产物缺失 → 两者皆 True；
    - 早于 native-sdk 源码 → ABI 过期（跨 FFI 布局可能不一致）；
    - 早于插件自身源码 → 源码过期（内核加载的是插件目录这份产物，改了不重编
      等于跑旧逻辑；2026-11-09 实测：tool_core 改了 messages.rs，本门禁按旧口径
      只比 native-sdk，`--build` 直接放行，实际 release 产物仍是旧代码）。
    """
    if not dll.exists():
        return True, True
    mtime = dll.stat().st_mtime
    return mtime < sdk_latest, mtime < plugin_latest


def latest_source_mtime(crates: Path) -> float:
    """目录下源码文件（rs/toml/Cargo.lock）的最新 mtime（构建产物/依赖目录排除）。

    同时用于 kernel/crates 与单个插件目录：插件目录里 .venv/target 等依赖与
    产物目录不算源码变更，否则一次 `uv sync` 就会把产物误判过期。
    """
    latest = 0.0
    if not crates.is_dir():
        return latest
    skip = {"target", "__pycache__", "node_modules", ".venv", ".git"}
    stack = [crates]
    while stack:
        d = stack.pop()
        try:
            for child in d.iterdir():
                if child.is_dir():
                    if child.name not in skip:
                        stack.append(child)
                    continue
                if child.suffix in (".rs", ".toml") or child.name == "Cargo.lock":
                    m = child.stat().st_mtime
                    latest = max(latest, m)
        except OSError:
            continue
    return latest


if __name__ == "__main__":
    sys.exit(main())
