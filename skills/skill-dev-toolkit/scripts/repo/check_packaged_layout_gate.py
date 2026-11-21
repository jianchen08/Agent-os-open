"""打包布局门禁：electron-builder 之后对 unpacked 产物实跑 check_packaged_layout。

接线对象（调查报告 2026-10-15 §四 刀 A）：check_packaged_layout.py 已声明
「包内不出现 junction/symlink 形态 .venv」等红线，但打包流水线未执法——第
五包装机事故后立此硬门禁。检查对象=打包产物（electron-builder 的 unpacked
产物），比安装器静置期检查更早失败；error 级 finding = 非零退出 = 阻断出包。

产物根发现（无参数时，release/ 下并行取所有存在的形态）：
  win   release/win-unpacked/resources
  linux release/linux-unpacked/resources
  mac   release/mac*/<App>.app/Contents/Resources
一个产物根都没发现也判失败（门禁不得静默放过）。

用法：
  python scripts/check_packaged_layout_gate.py                 # 自动发现产物根
  python scripts/check_packaged_layout_gate.py <resources根>…  # 显式指定（测试用）
退出码：0 = 全部合格；1 = 任一产物根存在 error 级 finding，或未发现产物根。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_packaged_layout as cpl  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = REPO_ROOT / "release"


def discover_product_roots() -> list[Path]:
    """electron-builder unpacked 产物内的 resources 根（多平台形态并存时全查）。"""
    roots: list[Path] = []
    for name in ("win-unpacked", "linux-unpacked"):
        candidate = RELEASE_DIR / name / "resources"
        if candidate.is_dir():
            roots.append(candidate)
    for mac_dir in sorted(RELEASE_DIR.glob("mac*")):
        for app_bundle in sorted(mac_dir.glob("*.app")):
            candidate = app_bundle / "Contents" / "Resources"
            if candidate.is_dir():
                roots.append(candidate)
    return roots


def _check_mxc_runtime(root: Path) -> list[str]:
    """Windows 产物必须随包携带 MXC 执行器（native-win/wslc 隔离前置）。

    判据 = Windows 产物根（kernel/agentos-kernel.exe 在位）下
    ``mxc/x64/wxc-exec.exe`` 与 ``MXC_RUNTIME_MANIFEST.json`` 同时存在。
    Linux/macOS 产物不供应 MXC（ProcessContainer/WSLC 均 Windows 专属）。
    """
    if not (root / "kernel" / "agentos-kernel.exe").is_file():
        return []
    problems: list[str] = []
    exe = root / "mxc" / "x64" / "wxc-exec.exe"
    manifest = root / "mxc" / "MXC_RUNTIME_MANIFEST.json"
    if not exe.is_file():
        problems.append(f"MXC_EXECUTOR_MISSING: {exe}")
    if not manifest.is_file():
        problems.append(f"MXC_MANIFEST_MISSING: {manifest}")
    return problems


def check_one_root(root: Path) -> int:
    """对单个产物根实跑检查器，返回其 error 数。

    will_relocate 显式 True：打包位产物将被 NSIS 挪位安装，editable 绝对路径行
    （含指向打包位自身的第四犯形态）必须拦下——不得随默认值漂移。
    """
    findings = cpl.check_resources(root, will_relocate=True)
    # 载荷阶段豁免：HOST_VENV_MISSING 属现役设计（resources 排除 venv，装后由
    # 维护轮 junction 步骤/boot autoprovision 供给——配方 §4⑥），不阻断出包；
    # P0 防线是 JUNCTION_IN_PACKAGE（junction 打进包），与「venv 应缺席」是两回事。
    # 装机阶段检查（对已安装 resources 实跑）不受本豁免影响。
    errors = [f for f in findings if f.level == "error" and f.code != "HOST_VENV_MISSING"]
    waived = [f for f in findings if f.level == "error" and f.code == "HOST_VENV_MISSING"]
    warns = [f for f in findings if f.level == "warn"]
    infos = [f for f in findings if f.level == "info"]
    mxc_errors = _check_mxc_runtime(root)
    print(f"[门禁] 产物根 {root}")
    if not findings:
        print("  结构合格：_host 共享 venv 在位，无 junction/symlink 形态 .venv")
    for f in errors:
        print(f"  [ERROR] {f.code}: {f.path}")
        print(f"          {f.detail}")
    for problem in mxc_errors:
        print(f"  [ERROR] {problem}")
    for f in waived:
        print(f"  [豁免 ] {f.code}: {f.path}（载荷阶段现役设计缺席，装后 junction 供给）")
    for f in warns:
        print(f"  [WARN ] {f.code}: {f.path}")
        print(f"          {f.detail}")
    if infos:
        print(f"  [info ] {len(infos)} 条（合法形态备案，不阻断）")
    error_count = len(errors) + len(mxc_errors)
    print(
        f"  结论：{'不合格' if error_count else '合格'}"
        f"（error={error_count} 豁免={len(waived)} warn={len(warns)} info={len(infos)}）"
    )
    return error_count


def main() -> int:
    # Windows 控制台默认 cp1252，门禁 label 含 CJK 时 print 直接
    # UnicodeEncodeError（2026-11-06 env-gate pack-windows 实证，NSIS 出包
    # 成功后翻在链尾本闸）——入口重配置 UTF-8，与 check_ci_gate/run_gates 同刀。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if len(sys.argv) > 1:
        roots = [Path(a).resolve() for a in sys.argv[1:]]
        missing = [r for r in roots if not r.is_dir()]
        if missing:
            print(f"产物根不存在：{missing}", file=sys.stderr)
            return 1
    else:
        roots = discover_product_roots()
        if not roots:
            print(
                f"未发现 unpacked 产物（{RELEASE_DIR} 下无 win/linux/mac unpacked "
                "resources）——门禁不得静默放过，若确未出包请人工复核链路",
                file=sys.stderr,
            )
            return 1

    print(f"打包布局门禁：检查产物根 {len(roots)} 个（check_packaged_layout 口径）")
    total_errors = sum(check_one_root(root) for root in roots)
    if total_errors:
        print(f"打包布局门禁：不合格（error 合计 {total_errors}）——阻断出包")
        return 1
    print("打包布局门禁：全部合格")
    return 0


if __name__ == "__main__":
    sys.exit(main())
