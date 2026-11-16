#!/usr/bin/env python
"""部署环境检测门（用户裁定 2026-10-28：CI 必含部署环境的检测与测试）。

三组机械检查（各自非零退出 = 红；平台缺工具按下方口径 FAIL/SKIP）：

  1. shell 语法   deploy/**/*.sh 逐个 `bash -n`（Git Bash/Linux 均可跑，
                  拦语法级回归；部署脚本群含 release_all/init/build 等）。
  2. systemd 单元 deploy/**/*.service 逐个 `systemd-analyze verify`——
                  单元引用的 ExecStart/EnvironmentFile/WorkingDirectory 落在
                  /opt 等部署机路径，本机不存在必误红：按单元声明在临时
                  --root 沙箱补桩文件/目录后校验（语法+依赖声明真实性）。
  3. nginx 配置   deploy/nginx.conf 是 server 块片段，不能直接 `nginx -t`：
                  组装 events+http 全量 harness（include 原片段）后校验，
                  拦指令级回归（CSP/proxy_pass/静态 root 等部署面改动）。

平台口径（fail-closed，不静默降级）：
  - Linux（CI/部署机）：bash/systemd-analyze/nginx 缺任一 = FAIL（环境缺陷
    本身就是本门要拦的东西）；nginx 缺失给出 apt 安装指引。
  - Windows（开发机）：systemd/nginx 属 Linux 专属 → 显式 SKIP 并说明由
    env-gate.yml deploy-env 车道（ubuntu）执法；bash 缺失 = FAIL（本仓
    开发基线含 Git Bash，见 AGENTS.md）。

用法：
  python scripts/check_deploy_env.py [--deploy-root <目录>] [--json]
退出码：0 = 全 PASS/SKIP；1 = 任一 FAIL。
CI 接线：env-gate.yml deploy-env job 经 `run_gates.py --filter deploy-env`。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_DIRS = ("deploy", "deploy/multi_device", "deploy/remote")
UNIT_EXEC_RE = re.compile(r"^(ExecStart(?:Pre|Post)?|ExecReload|ExecStop)=(.*)$", re.M)
UNIT_ENVFILE_RE = re.compile(r"^EnvironmentFile=(.*)$", re.M)
UNIT_WORKDIR_RE = re.compile(r"^WorkingDirectory=(.*)$", re.M)


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | SKIP | FAIL
    detail: str = ""


@dataclass
class UnitSpec:
    """systemd 单元的路径声明面（verify 沙箱补桩清单）。"""

    execs: list[str] = field(default_factory=list)
    env_files: list[str] = field(default_factory=list)
    work_dirs: list[str] = field(default_factory=list)


# ── 1. shell 语法 ────────────────────────────────────────────────────────
def collect_shell_scripts(deploy_root: Path) -> list[Path]:
    scripts: list[Path] = []
    for rel in DEPLOY_DIRS:
        d = deploy_root / rel
        if d.is_dir():
            scripts.extend(sorted(p for p in d.glob("*.sh") if p.is_file()))
    return scripts


def check_shell_syntax(scripts: list[Path], bash: str | None) -> CheckResult:
    if bash is None:
        if sys.platform == "win32":
            return CheckResult("shell-syntax", "FAIL", "bash 不可用（本仓开发基线含 Git Bash）")
        return CheckResult("shell-syntax", "FAIL", "bash 不可用（Linux 部署面必需）")
    failures: list[str] = []
    for script in scripts:
        proc = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, encoding="utf-8", check=False)
        if proc.returncode != 0:
            failures.append(f"{script.name}: {proc.stderr.strip().splitlines()[-1] if proc.stderr else '非零退出'}")
    if failures:
        return CheckResult("shell-syntax", "FAIL", f"{len(failures)} 个脚本语法红：{'; '.join(failures)}")
    return CheckResult("shell-syntax", "PASS", f"{len(scripts)} 个脚本 bash -n 通过")


# ── 2. systemd 单元 ──────────────────────────────────────────────────────
def parse_unit(text: str) -> UnitSpec:
    spec = UnitSpec()
    for _, value in UNIT_EXEC_RE.findall(text):
        token = _first_token(value.strip())
        token = token.lstrip("+-!@:")  # systemd 前缀修饰符（-/+/!/@）不参与路径
        if token.startswith("/"):
            spec.execs.append(token)
    for value in UNIT_ENVFILE_RE.findall(text):
        path = value.strip()
        if not path.startswith("-"):  # '-' 前缀 = 可缺省，仍补桩但不强制
            spec.env_files.append(path)
        else:
            spec.env_files.append(path[1:])
    for value in UNIT_WORKDIR_RE.findall(text):
        path = value.strip().strip("\"'")
        if path.startswith("/"):
            spec.work_dirs.append(path)
    return spec


def _first_token(value: str) -> str:
    """可执行路径 token：引号包裹的含空格路径优先，否则取首个空白前段。"""
    if value.startswith('"') or value.startswith("'"):
        quote = value[0]
        end = value.find(quote, 1)
        if end != -1:
            return value[1:end]
    parts = value.split()
    return parts[0] if parts else ""


def _stub_unit_sandbox(root: Path, spec: UnitSpec) -> None:
    for path in spec.execs:
        target = root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)
    for path in spec.env_files:
        target = root / path.lstrip("/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")
    for path in spec.work_dirs:
        (root / path.lstrip("/")).mkdir(parents=True, exist_ok=True)


def check_systemd_units(units: list[Path], tool: str | None, workdir: Path) -> CheckResult:
    if tool is None:
        if sys.platform == "win32":
            return CheckResult("systemd-verify", "SKIP", "本机无 systemd-analyze（由 CI ubuntu 车道执法）")
        return CheckResult("systemd-verify", "FAIL", "Linux 上 systemd-analyze 缺失（部署环境缺陷）")
    failures: list[str] = []
    checked = 0
    for unit in units:
        spec = parse_unit(unit.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="agentos-unit-verify-", dir=workdir) as td:
            root = Path(td) / "root"
            (root / "etc/systemd/system").mkdir(parents=True)
            # 默认依赖桩：systemd ≥255 的 verify 对 DefaultDependencies=yes 的
            # 单元解析隐式 Requires=sysinit.target，--root 沙箱内无系统单元树
            # 即红「Unit sysinit.target not found」（ubuntu-24.04 runner 实证
            # 2026-11-05）。补最小合法 target——本门执法面是用户单元的语法与
            # 路径声明真实性，不覆盖 systemd 自身单元树。
            sysinit = root / "usr/lib/systemd/system/sysinit.target"
            sysinit.parent.mkdir(parents=True, exist_ok=True)
            sysinit.write_text("[Unit]\nDescription=System Initialization (verify sandbox stub)\n", encoding="utf-8")
            _stub_unit_sandbox(root, spec)
            staged = root / "etc/systemd/system" / unit.name
            staged.write_text(unit.read_text(encoding="utf-8"), encoding="utf-8")
            proc = subprocess.run(
                [tool, "verify", "--root", str(root), str(staged)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
            )
        checked += 1
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip().splitlines()
            failures.append(f"{unit.name}: {tail[-1] if tail else '非零退出'}")
    if failures:
        return CheckResult("systemd-verify", "FAIL", f"{len(failures)} 个单元红：{'; '.join(failures)}")
    return CheckResult("systemd-verify", "PASS", f"{checked} 个单元 verify 通过（沙箱补桩）")


# ── 3. nginx 配置 ────────────────────────────────────────────────────────
def nginx_harness_text(fragment_path: Path, pid_path: Path, error_log_path: Path) -> str:
    """server 片段 → 全量配置 harness（events+http 包裹，改写后片段 include 进 http）。

    路径一律正斜杠：nginx 配置解析把反斜杠当转义序列（Windows 路径
    `deploy\\nginx.conf` 的 \\n 会吃成换行——实测坑），正斜杠双平台通吃。
    """
    return (
        f"pid {pid_path.as_posix()};\n"
        f"error_log {error_log_path.as_posix()};\n"
        "events { worker_connections 16; }\n"
        "http {\n"
        "    # access_log 显式 off：不设时 nginx 开编译内建默认路径（apt 版\n"
        "    # /var/log/nginx/access.log，root 属主），非 root 运行者 open\n"
        "    # EACCES → emerg 假红（CI ubuntu runner 实证 2026-11-05）。\n"
        "    access_log off;\n"
        f"    include {fragment_path.resolve().as_posix()};\n"
        "}\n"
    )


# listen 端口改写（端口数字原位替换，地址/flags 保留）。
LISTEN_PORT_RE = re.compile(r"^(\s*listen\s+)(?:[^:;]*:)?(\d+)(\s*;.*$)", re.M)
# 非 root 下 nginx -t 会真实 bind listen 口，特权口（<1024）EACCES → emerg
# 假红（CI ubuntu runner 实证 2026-11-05）。本门执法面=语法与指令级，
# 端口值不是执法对象——统一改写到非特权口。
VERIFY_LISTEN_PORT = "18443"


def _stage_listen_neutered_fragment(conf_path: Path, staged: Path) -> None:
    text = conf_path.read_text(encoding="utf-8")
    staged.write_text(
        LISTEN_PORT_RE.sub(rf"\g<1>{VERIFY_LISTEN_PORT}\g<3>", text),
        encoding="utf-8",
    )


def check_nginx_conf(conf: Path, tool: str | None, workdir: Path) -> CheckResult:
    if sys.platform == "win32":
        # Windows nginx -t 会实体 bind listen 端口（实测 10013 禁止访问），
        # 语义不等于 Linux 语法校验 → 恒由 CI ubuntu 车道执法，本机不判
        return CheckResult("nginx-config", "SKIP", "Windows 版 nginx -t 会实体绑端口（由 CI ubuntu 车道执法）")
    if tool is None:
        return CheckResult(
            "nginx-config",
            "FAIL",
            "Linux 上 nginx 缺失（部署环境缺陷；apt 安装指引：sudo apt-get install -y nginx）",
        )
    with tempfile.TemporaryDirectory(prefix="agentos-nginx-t-", dir=workdir) as td:
        prefix = Path(td)
        (prefix / "logs").mkdir()  # 配置解析前默认 error_log 落 <prefix>/logs/
        # -t 除语法外还会实体化运行时 temp 目录树，缺目录即 emerg 假红
        for sub in ("client_body_temp", "proxy_temp", "fastcgi_temp", "uwsgi_temp", "scgi_temp"):
            (prefix / "temp" / sub).mkdir(parents=True)
        fragment = prefix / "server-fragment.conf"
        _stage_listen_neutered_fragment(conf, fragment)
        harness = prefix / "nginx.conf"
        harness.write_text(
            nginx_harness_text(fragment, prefix / "logs" / "nginx.pid", prefix / "logs" / "error.log"),
            encoding="utf-8",
        )
        proc = subprocess.run(
            [tool, "-t", "-c", str(harness), "-p", f"{prefix.as_posix()}/"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()
        # nginx 末行恒是通用 "test failed"，真实 emerg 在其前一行——取
        # emerg/alert 行优先，缺失再退回末两行（2026-11-05 排障实证）。
        diag = [ln for ln in tail if "[emerg]" in ln or "[alert]" in ln]
        detail = "; ".join(diag[-1:]) if diag else " | ".join(tail[-2:])
        return CheckResult("nginx-config", "FAIL", detail or "nginx -t 非零退出")
    return CheckResult("nginx-config", "PASS", (proc.stderr or proc.stdout).strip().splitlines()[-1])


# ── 入口 ─────────────────────────────────────────────────────────────────
def main() -> int:
    parser = argparse.ArgumentParser(description="部署环境检测门（bash -n + systemd verify + nginx -t）")
    parser.add_argument("--deploy-root", help="deploy 目录所在根（缺省仓根）")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args()

    root = Path(args.deploy_root).resolve() if args.deploy_root else REPO_ROOT
    deploy_root = root / "deploy"
    if not deploy_root.is_dir():
        print(f"[deploy-env] FAIL 找不到 {deploy_root}")
        return 1

    scripts = collect_shell_scripts(root)
    units = sorted(p for rel in DEPLOY_DIRS for p in (root / rel).glob("*.service") if p.is_file())
    nginx_conf = deploy_root / "nginx.conf"

    results = [
        check_shell_syntax(scripts, shutil.which("bash")),
        check_systemd_units(units, shutil.which("systemd-analyze"), Path(tempfile.gettempdir())),
        check_nginx_conf(nginx_conf, shutil.which("nginx"), Path(tempfile.gettempdir()))
        if nginx_conf.is_file()
        else CheckResult("nginx-config", "FAIL", f"缺失 {nginx_conf}"),
    ]

    if args.json:
        print(json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"[deploy-env] {r.status} {r.name}: {r.detail}")
    failed = [r for r in results if r.status == "FAIL"]
    if failed:
        print(f"[deploy-env] 红 {len(failed)} 项：{', '.join(r.name for r in failed)}")
        return 1
    print("[deploy-env] 部署环境检测全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
