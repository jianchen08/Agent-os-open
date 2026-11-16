#!/usr/bin/env python
"""离线 venv 供给负载组装（pack 前一次性；产物 gitignored：electron/prereqs/{python,wheels}）。

2026-10-26 用户裁定「都放进去/离线也能用」：目标机零网络完成插件 venv 自愈。

- python/：仓内 `_host/.venv/pyvenv.cfg` 的 home 行 = 规范基座解释器（uv 管理的
  cpython-3.12，与全部 uv.lock 同代），整树拷贝为随包解释器（electron 经
  UV_PYTHON 注入，uv 建 venv 即用它——venv trampoline 指向安装目录内解释器，
  装机形态可迁移）。
- wheels/：plugins/shared 下全部 pyproject.toml+uv.lock 插件，`uv export`
  合并去重后按 win_amd64+cp312 标签 `pip download --only-binary` 全量取 wheel。
  网络**仅 pack 期**需要（打包机），目标机零网络（消费端 `--no-index
  --find-links`，内核 venv_provision 与 electron uvSyncRunner 同源）。

用法：python scripts/assemble_offline_payload.py [--skip-python]
失败即非零退出（缺件fail-closed，打包链 check_prereqs 亦会硬拦）。
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PREREQS = REPO_ROOT / "electron" / "prereqs"
PLUGINS = REPO_ROOT / "plugins" / "shared"
HOST_VENV = PLUGINS / "_host" / ".venv"


def base_python_dir() -> Path:
    """从 _host venv 的 pyvenv.cfg home 行解析规范基座解释器目录。"""
    cfg = HOST_VENV / "pyvenv.cfg"
    if not cfg.is_file():
        sys.exit(f"FAIL 基座判定源缺失：{cfg}（先在 dev 侧建好 _host venv）")
    home = None
    # uv 的 pyvenv.cfg 是无节头裸键格式（home = ...），configparser 不吃，手工解析
    for raw in cfg.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("home") and "=" in line:
            home = Path(line.split("=", 1)[1].strip())
            break
    if home is None or not home.exists():
        sys.exit(f"FAIL pyvenv.cfg home 不可解析或不存在：{cfg}")
    return home


def assemble_python(home: Path) -> None:
    dest = PREREQS / "python"
    if dest.exists():
        marker_ok = (dest / "python.exe").is_file()
        if marker_ok and filecmp.cmp(home / "python.exe", dest / "python.exe", shallow=False):
            print(f"[python] 在位且同版本，跳过：{dest}")
            return
        print(f"[python] 已存在但基座不同（{home} vs 既有），整树替换")
        shutil.rmtree(dest)
    print(f"[python] 整树拷贝 {home} -> {dest}（~分钟级，勿中断）")
    shutil.copytree(home, dest, symlinks=True, ignore_dangling_symlinks=True)
    print("[python] 完成")


def collect_plugin_dirs() -> list[Path]:
    return sorted(p.parent for p in PLUGINS.glob("*/pyproject.toml") if (p.parent / "uv.lock").is_file())


def merged_requirements(plugin_dirs: list[Path]) -> list[str]:
    """全部插件 uv export 合并去重（保留精确 pin 行；剔除空行/注释/-e 行）。"""
    merged: set[str] = set()
    for idx, proj in enumerate(plugin_dirs):
        reqs = subprocess.run(
            [
                "uv",
                "export",
                "--project",
                str(proj),
                "--format",
                "requirements.txt",
                "--no-hashes",
                "--no-dev",
                "--no-emit-project",
                # 只读导出：默认 export 会按当前镜像源刷新重写 uv.lock（2026-10-26
                # 实证 98 锁被静默改写 registry 源）——--frozen 锁即真相零改写。
                "--frozen",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        count = 0
        for raw in reqs.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-e "):
                continue
            merged.add(line)
            count += 1
        print(f"[reqs] {proj.name}: {count} 行")
        if idx % 20 == 19:
            print(f"[reqs] 进度 {idx + 1}/{len(plugin_dirs)}")
    return sorted(merged)


def download_wheels(reqs: list[str]) -> None:
    dest = PREREQS / "wheels"
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(reqs))
    print(f"[wheels] pip download {len(reqs)} 条 pin -> {dest}（网络仅本步，分钟级）")
    # 逐 pin 独立下载：合并集内同包多版本并存属预期（uv export 各自完整闭包），
    # pip 对同包异 pin 即使 --no-deps 也会报 ResolutionImpossible，逐行规避；
    # 失败即中断（缺 wheel 的插件离线自愈必败，fail-closed）。
    for idx, pin in enumerate(reqs, 1):
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "download",
                pin,
                "--no-deps",
                "-d",
                str(dest),
                "--only-binary=:all:",
                "--python-version",
                "312",
                "--platform",
                "win_amd64",
                "--implementation",
                "cp",
                "--abi",
                "cp312",
                "--disable-pip-version-check",
                "-q",
            ],
            check=True,
        )
        if idx % 25 == 0:
            print(f"[wheels] 进度 {idx}/{len(reqs)}")
    wheels = list(dest.glob("*.whl"))
    total = sum(w.stat().st_size for w in wheels)
    print(f"[wheels] 完成：{len(wheels)} wheels，{total / 1024 / 1024:.0f} MB")


def main() -> None:
    # CI Windows runner 控制台默认 cp1252，本脚本日志含 CJK（如「基座解释器」）
    # ——print 直接 UnicodeEncodeError 崩链（env-gate pack-windows 车道
    # 2026-11-05 实证）。入口显式重配置 UTF-8，宽于任何宿主控制台代码页。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-python", action="store_true", help="python/ 已在位时跳过整树拷贝")
    args = ap.parse_args()
    if sys.platform != "win32":
        sys.exit("FAIL 仅支持 Windows 打包机（win_amd64 载荷）")
    home = base_python_dir()
    print(f"[base] 基座解释器：{home}")
    if args.skip_python:
        print("[python] --skip-python 跳过")
    else:
        assemble_python(home)
    plugin_dirs = collect_plugin_dirs()
    if not plugin_dirs:
        sys.exit("FAIL plugins/shared 下没有任何 pyproject.toml+uv.lock 插件")
    print(f"[reqs] 待导出插件：{len(plugin_dirs)} 个")
    reqs = merged_requirements(plugin_dirs)
    # 构建后端兜底：锁内 path 依赖（如 agentos-plugin-sdk @ editable+../../sdk）
    # 在目标机 venv 创建期要就地构建（uv build isolation 独立解析构建依赖，
    # --no-index 下从 find-links 取）——build-system requires 必须入 wheelhouse。
    for backend in ("setuptools>=68.0", "wheel"):
        if not any(r.split("[")[0].split("=")[0].strip() == backend.split("=")[0] for r in reqs):
            reqs.append(backend)
    print(f"[reqs] 合并去重后 {len(reqs)} 条 pin（含构建后端兜底）")
    if not reqs:
        sys.exit("FAIL 合并 pin 集为空")
    download_wheels(reqs)
    print("[assemble] 完成：electron/prereqs/{python,wheels} 就绪（gitignored，随包走 extraResources）")


if __name__ == "__main__":
    main()
