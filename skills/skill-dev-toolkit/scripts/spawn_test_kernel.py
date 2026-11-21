#!/usr/bin/env python3
"""scratch 内核实例管理（开发工具箱技能自带脚本）——新插件联调用内核，不拖累现役内核。

方案依据（e2e sanctioned 模式，.github/workflows/e2e.yml「Start kernel」步）：
    AGENTOS_DB_PATH=<scratch> AGENTOS_KERNEL_PORT=<port> agentos-kernel
    轮询 /health 至就绪。

三隔离保证（不拖累现役内核/装机内核）：
- 端口：默认自动挑空闲端口，硬拒绝 9100（dev 默认）/9101（装机默认）；
- 数据面：DB 落 scratch 目录文件，密码经环境变量注入本实例——不读不改主仓
  .env/config，零文件写在 scratch 之外；
- 生命周期：实例档案 instance.json 记录 pid/port，stop 精确杀本实例并验证端口释放。

用法：
  python .../spawn_test_kernel.py spawn [--workspace W] [--port N] [--password PW]
                                        [--timeout S] [--scratch-root D]
                                        [--kernel-bin EXE]
                                        [--instance-config YAML] [--config-root D]
                                        [--user-root D]
  python .../spawn_test_kernel.py stop   [--scratch-root D] [--run-id ID]
  python .../spawn_test_kernel.py status [--scratch-root D] [--run-id ID]

插件根（分级单副本，ADR 2026-11-07）：内核从 --instance-config 指向的
instance.yaml 读三根；--config-root 给 plugin_tiers.yaml；--user-root 给可写
用户数据面。三者缺省不注入（沿用 ambient 环境），verify_plugin.py 会全部生成。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import cast

RESERVED_PORTS = (9100, 9101)
DEFAULT_TIMEOUT_S = 120.0
POLL_INTERVAL_S = 0.5
DEFAULT_SCRATCH_ROOT = ".test_kernel"


class TestKernelError(Exception):
    """scratch 内核管理失败（二进制缺失/保留端口/实例不存在/就绪超时）。"""


def default_workspace() -> Path:
    """脚本位于 <workspace>/skills/skill-dev-toolkit/scripts/，上溯三级。"""
    return Path(__file__).resolve().parents[3]


def resolve_kernel_binary(workspace: Path) -> Path:
    """定位内核二进制（release 优先于 debug，Windows 带 .exe 后缀）。"""
    stem = "agentos-kernel"
    names = [f"{stem}.exe", stem] if os.name == "nt" else [stem, f"{stem}.exe"]
    for profile in ("release", "debug"):
        for name in names:
            candidate = workspace / "kernel" / "target" / profile / name
            if candidate.is_file():
                return candidate
    raise TestKernelError(
        "内核二进制缺失：先构建（kernel/ 下 cargo build --release --bin agentos-kernel；"
        "WSL 须挂 rust:1.88 容器）后重试"
    )


def validate_port(port: int) -> None:
    """9100=dev 默认、9101=装机默认，spawn 上去即撞现役内核，硬拒绝。"""
    if port in RESERVED_PORTS:
        raise TestKernelError(
            f"端口 {port} 为现役内核保留端口（dev/装机默认），scratch 实例禁用"
        )


def pick_free_port() -> int:
    """向 OS 要一个空闲 TCP 端口（绑定即释放，存在极小竞窗，撞上即 spawn 报错）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wslenv_with_path_conversion(env: dict[str, str], keys: tuple[str, ...]) -> None:
    """把路径类环境变量登记进 ``WSLENV`` 的 ``/p`` 转换列表（WSL 下必需）。

    插件 sidecar 是 **Windows Python**（``plugins/shared/_host/.venv/Scripts/python.exe``，
    PE32+），经 WSL interop 启动。WSL 只对登记在 ``WSLENV`` 且带 ``/p`` 标志的变量
    做路径转换；**未登记**的 POSIX 路径（``/mnt/d/...``）会被 Windows 侧进程丢弃，
    于是 ``agent_config_load`` 读不到这里指定的配置根、静默回落装机用户空间
    （``%APPDATA%\\agentos\\config``）——表现为「配置改了不生效」且无任何报错，
    曾导致四轮联调误判为产品缺陷。

    仅在 WSL 下需要：原生 Windows 无 ``WSLENV`` 语义（设了也无害），原生 Linux
    内核直接读 POSIX 路径。故此处保留既有 ``WSLENV`` 条目、按需追加 ``/p``。
    """
    if os.name == "nt":
        return
    existing = [item for item in env.get("WSLENV", "").split(":") if item]
    covered = {item.split("/", 1)[0] for item in existing}
    for key in keys:
        if key in env and key not in covered:
            existing.append(f"{key}/p")
    if existing:
        env["WSLENV"] = ":".join(existing)


def build_kernel_env(port: int, db_path: Path, password: str, base: dict[str, str] | None = None) -> dict[str, str]:
    """环境变量注入三件套 + WSL 路径转换（只在副本上改，不改调用方进程环境）。"""
    env = dict(os.environ if base is None else base)
    env["AGENTOS_KERNEL_PORT"] = str(port)
    env["AGENTOS_DB_PATH"] = str(db_path)
    env["AGENTOS_ADMIN_PASSWORD"] = password
    _wslenv_with_path_conversion(env, ("AGENTOS_DB_PATH",))
    return env


def sidecar_python(workspace: Path) -> Path | None:
    """宿主 sidecar 解释器路径（不存在则 None——车道无插件 sidecar 即无法自检）。"""
    candidate = workspace / "plugins" / "shared" / "_host" / ".venv" / "Scripts" / "python.exe"
    return candidate if candidate.is_file() else None


def _as_posix_form(path_text: str) -> str:
    """把 Windows 盘符路径折算成 WSL 的 POSIX 形态（其余仅归一分隔符与尾斜杠）。

    sidecar 是 Windows 进程，回答的是 ``D:`` 开头的路径；指定值是 ``/mnt/d/...``
    形态。两者指同一目录时字面不等，故比较前统一折算。这里**不做真实文件系统解析**
    ——该路径在 sidecar 侧是 Windows 盘、在内核侧是 /mnt 映射，两侧 resolve 结果
    不可直接比。
    """
    text = str(path_text).strip().replace("\\", "/").rstrip("/")
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        drive = text[0].lower()
        rest = text[2:].lstrip("/")
        return f"/mnt/{drive}/{rest}".rstrip("/") if rest else f"/mnt/{drive}"
    return text


def assert_config_root_visible(
    workspace: Path, env: dict[str, str], config_root: str, timeout_s: float = 20.0
) -> None:
    """自检：配置根必须能被**插件 sidecar 实际读到**，否则显式红。

    内核日志里的 ``Config root:`` 只证明内核自己解析对了，**不能**证明插件读到了同一份
    ——sidecar 是独立进程（WSL 下还是 Windows 进程），环境传递断在哪只有问它自己。
    这里用 sidecar 自己的解释器按同一 env 打印它看到的 ``AGENTOS_CONFIG_ROOT`` 并比对；
    不一致即抛 TestKernelError，把「配置改了不生效」从静默漂移变成显式失败。

    sidecar 解释器不在时跳过（原生 Linux 车道无 Windows venv，无需该转换）。
    """
    python_exe = sidecar_python(workspace)
    if python_exe is None:
        return
    probe = "import os; print(os.environ.get('AGENTOS_CONFIG_ROOT',''))"
    try:
        result = subprocess.run(
            [str(python_exe), "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TestKernelError(f"配置根自检无法执行 sidecar 解释器: {exc}") from exc
    seen = (result.stdout or "").strip()
    if not seen:
        raise TestKernelError(
            "配置根自检失败：sidecar 侧 AGENTOS_CONFIG_ROOT 为空 —— "
            "POSIX 路径未穿过 WSL interop（检查 WSLENV 登记 /p）"
        )
    if _as_posix_form(seen) != _as_posix_form(config_root):
        raise TestKernelError(
            "配置根自检失败：sidecar 读到的配置根与指定值不一致\n"
            f"  指定: {config_root}\n  sidecar 实际: {seen}\n"
            "（典型成因：POSIX 路径未登记 WSLENV=/p，Windows 侧丢弃后回落装机用户空间）"
        )


def probe_health(port: int, timeout_s: float = 1.0) -> bool:
    """GET /health 无认证（e2e 同口径），可达即就绪。

    必须绕开环境代理：开发机常设 ``HTTP_PROXY``，而 ``127.0.0.1`` 未必在
    ``no_proxy`` 里——裸 ``urlopen`` 会把本地探活请求交给代理，代理连不上内网
    回环就返回 502，于是「内核已正常监听但 spawn 永远等不到就绪」，超时后才报
    「内核就绪超时」，误导排查方向。故显式用无代理 opener。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/health", timeout=timeout_s):
            return True
    except (urllib.error.URLError, OSError):
        return False


def await_ready(port: int, timeout_s: float, sleeper: Callable[[float], None] = time.sleep) -> None:
    """轮询 /health 至就绪，超时 TestKernelError。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if probe_health(port):
            return
        sleeper(POLL_INTERVAL_S)
    raise TestKernelError(f"内核就绪超时（{timeout_s}s，port {port}）")


def _scratch_root(workspace: Path, scratch_root: str | None) -> Path:
    return Path(scratch_root) if scratch_root else workspace / DEFAULT_SCRATCH_ROOT


def _write_instance(path: Path, record: dict[str, object]) -> None:
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_instance(scratch_root: Path, run_id: str | None) -> tuple[Path, dict[str, object]]:
    if run_id:
        instance_path = scratch_root / run_id / "instance.json"
    else:
        runs = sorted(
            (p for p in scratch_root.glob("*/instance.json")),
            key=lambda p: p.stat().st_mtime,
        )
        if not runs:
            raise TestKernelError(f"scratch 目录无实例档案: {scratch_root}")
        instance_path = runs[-1]
    if not instance_path.is_file():
        raise TestKernelError(f"实例档案不存在: {instance_path}")
    return instance_path, json.loads(instance_path.read_text(encoding="utf-8"))


def spawn(
    workspace: Path,
    port: int | None = None,
    password: str | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    scratch_root: str | None = None,
    kernel_bin: str | None = None,
    env_extra: dict[str, str] | None = None,
    instance_config: str | None = None,
    config_root: str | None = None,
    user_root: str | None = None,
) -> dict[str, object]:
    """拉起 scratch 内核实例并等待就绪，返回实例档案。

    kernel_bin 直指内核 exe（装机自举验证车道：resources/kernel/ 包内二进制，
    ADR 2026-11-03-plugin-dev-skill-and-staging-verify-pipeline §D3）；缺省按
    workspace 推导仓内 cargo 产物。

    插件根（ADR 2026-11-07-plugin-tiered-single-copy-model）：内核只认实例启动
    配置的三根（零推导零回落），本函数用三个具名参数交付——
    instance_config → AGENTOS_INSTANCE_CONFIG（配置文件路径载体）、
    config_root → AGENTOS_CONFIG_ROOT（plugin_tiers.yaml 等配置根）、
    user_root → AGENTOS_USER_ROOT（可写用户数据面，模式播种/venv 登记）。
    env_extra 追加其余环境变量（显式调用方最后赢）。
    """
    if kernel_bin:
        binary = Path(kernel_bin)
        if not binary.is_file():
            raise TestKernelError(f"--kernel-bin 指向的内核 exe 不存在: {binary}")
    else:
        binary = resolve_kernel_binary(workspace)
    if port is None:
        port = pick_free_port()
    validate_port(port)
    admin_password = password or uuid.uuid4().hex[:12]
    run_dir = _scratch_root(workspace, scratch_root) / f"run_{port}_{datetime.now():%Y%m%d_%H%M%S}"
    run_dir.mkdir(parents=True)
    db_path = run_dir / "kernel.db"
    log_path = run_dir / "kernel.log"
    env = build_kernel_env(port, db_path, admin_password)
    explicit_env: dict[str, str] = {}
    if instance_config:
        explicit_env["AGENTOS_INSTANCE_CONFIG"] = instance_config
    if config_root:
        explicit_env["AGENTOS_CONFIG_ROOT"] = config_root
    if user_root:
        explicit_env["AGENTOS_USER_ROOT"] = user_root
    if env_extra:
        explicit_env.update(env_extra)
    if explicit_env:
        # 在完整进程环境之上追加（build_kernel_env 的 base 是底座替换语义，
        # 直传会把 PATH/SYSTEMROOT 砍掉→子进程 winsock 初始化失败 os error 10106，
        # snippet_box 跟进实测教训）
        env.update(explicit_env)
        # 三根全是 POSIX 路径，必须登记 /p 才能穿过 WSL interop 到达 Windows
        # sidecar（见 _wslenv_with_path_conversion）。漏登记不报错——插件只会
        # 静默回落装机用户空间配置，现象是「配置改了不生效」（联调误判成因）。
        _wslenv_with_path_conversion(
            env,
            ("AGENTOS_INSTANCE_CONFIG", "AGENTOS_CONFIG_ROOT", "AGENTOS_USER_ROOT"),
        )
    # 配置根自检（在拉起前）：内核自己解析对 ≠ 插件读得到。断在环境传递上时
    # 只有问 sidecar 本体才知道，这里把静默漂移变成显式红，避免再误判为产品缺陷。
    if config_root:
        assert_config_root_visible(workspace, env, config_root)
    with log_path.open("ab") as log_file:
        process = subprocess.Popen(
            [str(binary)],
            cwd=str(workspace),
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            # POSIX 独立进程组：stop 整组终止，插件的 sidecar 子进程不留孤儿。
            start_new_session=os.name != "nt",
        )
    record: dict[str, object] = {
        "run_id": run_dir.name,
        "pid": process.pid,
        "port": port,
        "base_url": f"http://127.0.0.1:{port}",
        "db": str(db_path),
        "log": str(log_path),
        "admin_password": admin_password,
        "workspace": str(workspace),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    try:
        await_ready(port, timeout_s)
    except TestKernelError as exc:
        process.terminate()
        raise TestKernelError(f"{exc}（内核日志: {log_path}）") from exc
    _write_instance(run_dir / "instance.json", record)
    return record


def stop(scratch_dir: Path, run_id: str | None = None) -> dict[str, object]:
    """终止实例（Windows taskkill /T 树杀 / POSIX 进程组 SIGTERM），并验证端口释放。"""
    instance_path, record = _load_instance(scratch_dir, run_id)
    if record.get("stopped"):
        return record
    pid = cast(int, record["pid"])
    port = cast(int, record["port"])
    if os.name == "nt":
        result = subprocess.run(
            # /T = 连子进程（插件 sidecar）一起杀：只杀内核会留下 sidecar 占住
            # 装载视图 junction 与内核日志，scratch 清理必失败。
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            text=True,
            encoding="utf-8", errors="replace",
            check=False,
        )  # taskkill 输出 GBK 中文（如"错误"），text 默认 utf-8 解码崩 reader thread
           # （snippet_box 实测缺陷④实锤），replace 显式吞不可解码字节
        if result.returncode != 0 and probe_health(port, timeout_s=0.5):
            raise TestKernelError(f"taskkill 失败且端口 {port} 仍可达: {result.stderr.strip()}")
        # taskkill 失败但端口已释放 = 实例先前已退出，按已停处理
    else:
        try:
            # spawn 用 start_new_session 建了独立进程组：整组 SIGTERM 收 sidecar。
            os.killpg(pid, 15)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and probe_health(port, timeout_s=0.5):
        time.sleep(0.2)
    if probe_health(port, timeout_s=0.5):
        raise TestKernelError(f"进程已杀但端口 {port} 仍可达，须人工排查")
    record["stopped"] = True
    record["stopped_at"] = datetime.now().isoformat(timespec="seconds")
    _write_instance(instance_path, record)
    return record


def status(scratch_dir: Path, run_id: str | None = None) -> dict[str, object]:
    """实例档案 + 实时健康/pid 探针。"""
    _, record = _load_instance(scratch_dir, run_id)
    port = cast(int, record["port"])
    record["health_ok"] = probe_health(port)
    return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="scratch 内核实例管理")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("spawn", "stop", "status"):
        child = sub.add_parser(name)
        child.add_argument("--workspace", default=None, help="仓/工作空间根（默认按脚本位置推导）")
        child.add_argument("--scratch-root", default=None, help="scratch 根目录（默认 <workspace>/.test_kernel）")
        if name == "spawn":
            child.add_argument("--port", type=int, default=None, help="端口（默认自动挑空闲）")
            child.add_argument("--password", default=None, help="admin 口令（默认随机生成）")
            child.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="就绪超时秒数")
            child.add_argument("--kernel-bin", default=None, help="内核 exe 直指（装机自举验证：包内 resources/kernel/ 二进制；缺省按 workspace 推导 cargo 产物）")
            child.add_argument("--instance-config", default=None, help="实例启动配置文件路径（AGENTOS_INSTANCE_CONFIG；分级单副本三根）")
            child.add_argument("--config-root", default=None, help="配置根（AGENTOS_CONFIG_ROOT；plugin_tiers.yaml 等所在）")
            child.add_argument("--user-root", default=None, help="可写用户数据根（AGENTOS_USER_ROOT；模式播种/venv 登记）")
        else:
            child.add_argument("--run-id", default=None, help="实例 run_id（默认取最近一次）")
    args = parser.parse_args(argv)
    workspace = Path(args.workspace) if args.workspace else default_workspace()
    scratch_dir = _scratch_root(workspace, args.scratch_root)
    try:
        if args.command == "spawn":
            record = spawn(
                workspace, port=args.port, password=args.password,
                timeout_s=args.timeout, scratch_root=args.scratch_root,
                kernel_bin=args.kernel_bin, instance_config=args.instance_config,
                config_root=args.config_root, user_root=args.user_root,
            )
        elif args.command == "stop":
            record = stop(scratch_dir, args.run_id)
        else:
            record = status(scratch_dir, args.run_id)
    except TestKernelError as exc:
        print(f"scratch 内核失败: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
