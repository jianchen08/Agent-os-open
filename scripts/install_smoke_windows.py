#!/usr/bin/env python
"""NSIS 静默安装 + 装后冒烟（CI 打包车道的「装得上、装得对、起得来」实测）。

用户裁定 2026-10-28：CI 必含打包环境的检测与测试——本脚本是 pack-windows
车道的收口断言，对 electron-builder 产物做五段验证：

  1. 静默安装   安装器 /S /D=<临时目录>（per-user 免提权安装，2026-11-12 起
                不再需要管理员；installer-custom.nsh 的孤儿清障/静默语义一并
                实测——装机坑集 NSIS /S 必带 /D= 的机械回归）；
  2. 装后布局   复用 scripts/check_packaged_layout.py（单一真值）校验
                <安装目录>/resources，再加随包件硬断言（内核 exe/合宿 venv/
                离线三件套 uv+python+wheels/rclone/WinFsp msi）；
  3. 清单回读   复用 deploy/verify_deploy_manifest.py（单一真值，--strict）把
                安装位实际字节与包内 deploy-manifest.json 逐文件对账——缺失/
                哈希不符/旧版残留一律红（ADR 2026-11-10-deploy-freshness-
                verification 的 CP 执法位：装完是否真的落地）；
  4. 内核启动   安装位内核带临时库+隔离端口拉起，/health 轮询 200（同
                release.yml Linux 冒烟范式；CI 一次性 runner 无装机实例，
                不触「bash 直拉内核=毒化」铁律的共存机边界）；
  5. 插件运行时健康（2026-10-29 装机实证补课）：/health 绿 ≠ 插件活——
                启动日志失败签名扫描（No module named/venv 解释器缺失/
                has no attribute）+ 随包 _host venv 的 SDK 导入与
                editable 安装指向越界检查（check_plugin_runtime_health.py
                单一真值；editable 第三犯的通用执法位）。

用法：
  python scripts/install_smoke_windows.py --installer "release/灵汐助手 Setup 0.2.2.exe"
      [--dest <安装目录>] [--port 9117] [--install-timeout 600] [--boot-timeout 120]
退出码：0 = 五段全过；1 = 任一断言红；2 = 环境错（非 Windows/参数缺）。
仅 Windows 可执行（NSIS 安装器与内核 exe 均为 Windows 产物）。
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 随包件硬断言（rel 相对 resources 根, kind, min_bytes|min_children）。布局细节由
# check_packaged_layout.py 单一真值承载，这里只查「装机坑集」高危面。
CARRY_SPEC: list[tuple[str, str, int]] = [
    ("kernel/agentos-kernel.exe", "file", 1_000_000),
    ("frontend/dist/index.html", "file", 1),
    ("deploy-manifest.json", "file", 1),  # 装后回读清单（afterPack 钩子产出）
    ("plugins/shared", "dir", 50),  # plugin.json 计数下限（百级平铺）
    ("plugins/shared/_host/.venv", "dir", 3),  # 合宿 venv 真身随包
    ("plugins/sdk/src", "dir", 1),
    ("config", "dir", 1),
    ("skills", "dir", 1),
    ("prereqs/uv.exe", "file", 10_000_000),
    ("prereqs/python", "dir", 3),  # 离线三件套·随包解释器（判据：目录非空）
    ("prereqs/wheels", "dir", 10),  # 离线三件套·wheelhouse
    ("bin/rclone.exe", "file", 10_000_000),
    ("prereqs/winfsp-2.1.25156.msi", "file", 1_000_000),
]


def installer_argv(installer: Path, dest: Path) -> list[str]:
    """NSIS 静默安装 argv：/S + /D=<目录>（/D 必须是最后一个参数、不带引号）。"""
    return [str(installer), "/S", f"/D={dest}"]


def missing_carry(resources: Path, spec: list[tuple[str, str, int]] | None = None) -> list[str]:
    """返回不满足随包契约的条目描述（空列表 = 全过）。"""
    problems: list[str] = []
    for rel, kind, floor in spec or CARRY_SPEC:
        path = resources / rel
        if kind == "file":
            if not path.is_file():
                problems.append(f"缺文件 {rel}")
            elif path.stat().st_size < floor:
                problems.append(f"文件过小 {rel}（{path.stat().st_size} < {floor}）")
        elif not path.is_dir():
            problems.append(f"缺目录 {rel}")
        else:
            children = sum(1 for _ in path.iterdir())
            if children < floor:
                problems.append(f"目录过空 {rel}（{children} < {floor}）")
    return problems


def find_resources(dest: Path) -> Path:
    resources = dest / "resources"
    if not resources.is_dir():
        raise FileNotFoundError(f"安装目录缺 resources/：{dest}")
    return resources


def verify_deployed_manifest(resources: Path) -> tuple[bool, str]:
    """部署清单回读（ADR 2026-11-10）：安装位实际字节 vs 包内 deploy-manifest.json。

    --strict：缺失 / 哈希不符 / 旧版多余文件任一即红——CI 打包车道对「装完是否
    真的把本次发版落地」的机械执法位。清单本身缺失同样判红（afterPack 钩子未
    生效 = 打包链缺陷，不是可跳过的环境问题）。
    """
    manifest = resources / "deploy-manifest.json"
    if not manifest.is_file():
        return False, f"缺部署清单 {manifest}（afterPack 钩子未产出？）"
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "deploy" / "verify_deploy_manifest.py"),
            "verify",
            "--root", str(resources),
            "--manifest", str(manifest),
            "--strict",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return proc.returncode == 0, f"{proc.stdout}{proc.stderr}".strip()


def run_installer(installer: Path, dest: Path, timeout_s: int) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        installer_argv(installer, dest),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"静默安装非零退出 {proc.returncode}：{proc.stdout} {proc.stderr}")
    if not any(dest.iterdir()):
        raise RuntimeError("静默安装后安装目录为空（/S /D 未生效或安装器静默失败）")


def cleanup_install(dest: Path, wait_s: float = 30.0) -> None:
    """收尾：先跑安装位卸载器（清 HKCU 卸载项/快捷方式），再删安装目录。

    per-user 安装的卸载器零提权（RequestExecutionLevel user）；卸载器缺席或
    失败不阻断，目录删除兜底。冒烟安装位必须在测试后消失——失败路径同样
    收尾（诊断信息已随输出打印）。
    """
    for exe in sorted(dest.glob("Uninstall*.exe")):
        try:
            proc = subprocess.run([str(exe), "/S"], capture_output=True, timeout=120, check=False)
            print(f"[install-smoke] 卸载器 {exe.name} rc={proc.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"[install-smoke] 卸载器 {exe.name} 执行失败（继续删目录）：{exc}")
    # NSIS 卸载器把自身复制到临时目录执行后原进程即返回，删除是异步的：
    # 等目录自然消失（上限 30s），超时再 rmdir 兜底，避免把在途删除误判成残留。
    deadline = time.monotonic() + wait_s
    while dest.exists() and time.monotonic() < deadline:
        time.sleep(0.5)
    subprocess.run(["cmd", "/C", "rmdir", "/S", "/Q", str(dest)], capture_output=True, check=False)
    if dest.exists():
        print(f"[install-smoke] WARN 安装目录未完全删除：{dest}")


def boot_smoke(kernel_exe: Path, port: int, timeout_s: int) -> tuple[bool, str, Path]:
    """临时库 + 隔离端口拉起安装位内核，/health 轮询 200；结束杀全树。

    返回 (是否存活, 详情, workdir)——workdir/logs 是本次启动日志，供插件
    运行时健康签名扫描（/health 活着 ≠ 插件运行时健康，2026-10-29）。
    """
    workdir = Path(tempfile.mkdtemp(prefix="agentos-boot-smoke-"))
    env = dict(os.environ)
    env["AGENTOS_DB_PATH"] = str(workdir / "smoke.db")
    env["AGENTOS_KERNEL_PORT"] = str(port)
    # 内核口令无默认值：随机播种一次性口令，冒烟只探 /health 不消费它
    env["AGENTOS_ADMIN_PASSWORD"] = secrets.token_hex(16)
    proc = subprocess.Popen(
        [str(kernel_exe)],
        cwd=workdir,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    deadline = time.monotonic() + timeout_s
    code = 0
    url = f"http://127.0.0.1:{port}/health"
    try:
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    code = resp.status
                    if code == 200:
                        return True, f"/health -> 200（pid {proc.pid}）", workdir
            except (urllib.error.URLError, OSError):
                code = 0
            if proc.poll() is not None:
                return False, f"内核提前退出 rc={proc.returncode}（日志 {workdir}/logs）", workdir
            time.sleep(2)
        return False, f"/health 未在 {timeout_s}s 内返回 200（最后一次 HTTP {code}）", workdir
    finally:
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )


def _kernel_log_tail(workdir: Path, lines: int = 40) -> str:
    log_dir = workdir / "logs"
    logs = sorted(str(p) for p in log_dir.glob("*")) if log_dir.is_dir() else []
    if not logs:
        return ""
    newest = logs[-1]
    try:
        content = Path(newest).read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(content[-lines:])
    except OSError:
        return ""


def main() -> int:
    # Windows 控制台默认 cp1252，冒烟输出含 CJK 时 print 直接
    # UnicodeEncodeError（2026-11-06 env-gate Silent install + smoke 步实证，
    # NSIS 出包+装完之后翻在冒烟脚本）——入口重配置 UTF-8，与
    # check_ci_gate/run_gates/check_packaged_layout_gate 同刀。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="NSIS 静默安装 + 装后冒烟（CI 打包车道收口）")
    parser.add_argument("--installer", required=True, help="electron-builder NSIS 安装器路径")
    parser.add_argument("--dest", help="安装目标目录（缺省临时目录；冒烟结束卸载并清理）")
    parser.add_argument("--port", type=int, default=9117, help="内核冒烟端口（隔离面）")
    parser.add_argument("--install-timeout", type=int, default=600)
    parser.add_argument("--boot-timeout", type=int, default=120)
    args = parser.parse_args()

    if sys.platform != "win32":
        print(
            "[install-smoke] FAIL 仅 Windows 可执行（NSIS 安装器/内核 exe 均为 Windows 产物；"
            "由 env-gate.yml pack-windows 车道执法）"
        )
        return 2

    installer = Path(args.installer)
    if not installer.is_file():
        print(f"[install-smoke] FAIL 安装器不存在：{installer}")
        return 1
    dest = Path(args.dest).resolve() if args.dest else Path(tempfile.mkdtemp(prefix="agentos-install-smoke-"))

    try:
        try:
            run_installer(installer, dest, args.install_timeout)
            print(f"[install-smoke] PASS 静默安装 → {dest}")
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"[install-smoke] FAIL 静默安装：{exc}")
            return 1
        return verify_installed(dest, args)
    finally:
        # 成功/失败都收尾：冒烟安装位（目录 + HKCU 卸载项/快捷方式）不留驻
        cleanup_install(dest)


def verify_installed(dest: Path, args: argparse.Namespace) -> int:
    """五段验证主体（安装位已就绪）；调用方负责收尾清理。"""
    try:
        resources = find_resources(dest)
    except FileNotFoundError as exc:
        print(f"[install-smoke] FAIL {exc}")
        return 1

    # 布局单一真值复用（packaged layout gate 同一检查器，装机形态再跑一遍；
    # --installed：装后树不再挪位，包内绝对路径行合法，只拦越界/缺目标）
    layout = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "check_packaged_layout.py"),
            str(resources),
            "--installed",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if layout.returncode != 0:
        print(f"[install-smoke] FAIL 装后布局红：{layout.stdout} {layout.stderr}")
        return 1
    print("[install-smoke] PASS 装后布局（check_packaged_layout）")

    problems = missing_carry(resources)
    if problems:
        for p in problems:
            print(f"[install-smoke] FAIL 随包契约：{p}")
        return 1
    print(f"[install-smoke] PASS 随包契约 {len(CARRY_SPEC)} 项（内核/清单/venv/离线三件套/挂载件）")

    ok, detail = verify_deployed_manifest(resources)
    print(f"[install-smoke] {'PASS' if ok else 'FAIL'} 部署清单回读（本次发版是否真的落地）：{detail}")
    if not ok:
        return 1

    ok, detail, boot_workdir = boot_smoke(resources / "kernel" / "agentos-kernel.exe", args.port, args.boot_timeout)
    print(f"[install-smoke] {'PASS' if ok else 'FAIL'} 内核启动：{detail}")
    if not ok:
        tail = _kernel_log_tail(Path(tempfile.gettempdir()))
        if tail:
            print(f"[install-smoke] 内核日志尾：\n{tail}")
        return 1

    # 第四段：插件运行时健康（2026-10-29 装机实证 /health 绿 ≠ 插件活——
    # venv/SDK 缺失与 editable 越界只有这里能拦）。boot 日志失败签名 +
    # 随包 _host venv（SDK 导入 + editable 不得越出安装位）。
    boot_logs = sorted((boot_workdir / "logs").glob("*")) if (boot_workdir / "logs").is_dir() else []
    health_argv = [sys.executable, str(REPO_ROOT / "scripts" / "check_plugin_runtime_health.py")]
    for log in boot_logs:
        health_argv += ["--boot-log", str(log)]
    host_venv = resources / "plugins" / "shared" / "_host" / ".venv"
    if host_venv.is_dir():
        health_argv += ["--venv", str(host_venv), "--allow-editable-under", str(resources)]
    health = subprocess.run(
        health_argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if health.returncode != 0:
        print(f"[install-smoke] FAIL 插件运行时健康：\n{health.stdout}{health.stderr}")
        return 1
    print("[install-smoke] PASS 插件运行时健康（boot 日志签名 + _host venv + editable 根）")

    print(f"[install-smoke] 装后冒烟全绿（安装目录 {dest}，收尾卸载并清理）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
