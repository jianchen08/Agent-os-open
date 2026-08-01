"""wsl_health_probe.sh 核心判定逻辑（classify_d_state）单测。

背景（WSL 冷启动误判死锁根因）：
start_web_cn.bat 把健康探针的一切非正常退出码（含外层 timeout 的 124/126/127）
都归类为"内核 D-state 死锁"并触发 wsl --shutdown。但这些码在冷启动期实为
WSL 刚 shutdown 重启后文件系统/IO 未就绪的瞬时故障。真正的死锁只有脚本主动
exit 8。本测试通过环境变量注入（AO_PROBE_FORCE_*）驱动脚本判定分支，确保：

1. 退出码约定稳定：0=健康 / 8=内核污染（D-state 死锁）。
2. 运行期阈值：>=1 持续 D 即判 8（严格）。
3. 启动期宽容：>=2 持续 D 才判 8（避免 systemd 初始化瞬时 D 误判）。
4. 无参调用不报错（回归旧 bug：start_web_cn.bat 曾误传 %WSL_DIR% 参数）。

不依赖真实 WSL / 真实 /proc，纯靠注入钩子跑 bash 子进程，CI 可跑。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# 探针脚本路径（项目根 / wsl_health_probe.sh）
PROBE_SCRIPT = Path(__file__).resolve().parent.parent / "wsl_health_probe.sh"


def _to_bash_path(p: Path) -> str:
    """Windows 路径转 bash 能识别的 POSIX 路径（自动适配 WSL/MINGW 两种 bash）。

    subprocess.run(["bash", "D:\\\\foo\\\\bar.sh"]) 会把反斜杠吃掉。且 Python
    解析到的 bash 可能是 WSL 的 Linux bash（路径 /mnt/d/...）或 Git 的 MINGW
    bash（路径 /d/...），二者挂载点不同。这里先探测 bash 的 uname，再选路径。
    """
    s = str(p).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        drive = s[0].lower()
        rest = s[2:]
    else:
        return s
    # 探测 bash 类型：WSL(Linux) 用 /mnt/d，MINGW 用 /d。
    # stderr 丢弃：WSL bash 会把 UTF-16 代理提示打到 stderr，text=True 解码会崩。
    try:
        uname = subprocess.run(
            ["bash", "-c", "uname"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=5,
        ).stdout.strip().lower()
    except Exception:
        uname = ""
    if "mingw" in uname or "msys" in uname:
        return f"/{drive}{rest}"
    # linux (WSL) 或未知：用 /mnt/d 形式（WSL 标准挂载点）
    return f"/mnt/{drive}{rest}"


# bash 视角下的脚本 POSIX 路径
PROBE_SCRIPT_BASH = _to_bash_path(PROBE_SCRIPT)


def _run_probe(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """以指定环境变量注入跑探针脚本，返回 CompletedProcess（含 returncode/stdout）。

    用 bytes 捕获再手动 UTF-8 解码：Windows 下 text=True 默认用 locale 编码
    （CP936/GBK）解码会因 bash 输出中的字节报 UnicodeDecodeError。脚本输出
    本身是 ASCII/UTF-8，stderr 可能含系统 locale 非 UTF-8 字节，用 errors 兜底。
    """
    env = os.environ.copy()
    env["AO_PROBE_SKIP_SLEEP"] = "1"  # 测试一律跳过 sleep 2，加速
    env["LC_ALL"] = "C"  # 固定 locale，避免系统 locale 污染脚本输出
    env["LANG"] = "C"
    # 关键：当 Python 解析到的 bash 是 WSL 的 Linux bash 时，自定义环境变量
    # 跨 Windows→Linux 边界默认不透传，必须经 WSLENV 声明（MINGW/Linux bash
    # 会忽略 WSLENV，故此设置对其它 bash 无害）。
    env["WSLENV"] = (
        "AO_PROBE_FORCE_DCOUNT/u:AO_PROBE_FORCE_BOOT_PHASE/u:"
        "AO_PROBE_FORCE_UPTIME/u:AO_PROBE_FORCE_IGNORED/u:AO_PROBE_SKIP_SLEEP/u"
    )
    env.update(env_overrides)
    # 用 bash 执行（与 start_web_cn.bat 的 `bash wsl_health_probe.sh` 调用一致）。
    # stderr 丢弃：WSL bash 会把 UTF-16 编码的代理提示打到 stderr，Python subprocess
    # 内部 reader 线程默认 UTF-8 解码会报 PytestUnhandledThreadExceptionWarning（噪音）。
    # 测试断言只依赖 returncode + stdout，stderr 无需内容。
    r = subprocess.run(
        ["bash", PROBE_SCRIPT_BASH],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    # bytes -> str，UTF-8 优先，解码失败用 replace 兜底（不影响断言）
    r.stdout = r.stdout.decode("utf-8", errors="replace")
    r.stderr = ""
    return r


# ---------------------------------------------------------------------------
# 1. 运行期（running）阈值：>=1 持续 D 即判死锁
# ---------------------------------------------------------------------------


def test_running_phase_zero_d_is_healthy():
    """运行期无持续 D 进程 → exit 0（健康）。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "0", "AO_PROBE_FORCE_BOOT_PHASE": "0", "AO_PROBE_FORCE_UPTIME": "120"})
    assert r.returncode == 0
    assert "[OK]" in r.stdout
    assert "running" in r.stdout


def test_running_phase_one_d_is_deadlock():
    """运行期 1 个持续 D 进程 → exit 8（死锁，严格阈值）。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "1", "AO_PROBE_FORCE_BOOT_PHASE": "0", "AO_PROBE_FORCE_UPTIME": "120"})
    assert r.returncode == 8
    assert "[WARN]" in r.stdout
    assert "kernel polluted" in r.stdout


# ---------------------------------------------------------------------------
# 2. 启动期（boot-phase）宽容：threshold=2
# ---------------------------------------------------------------------------


def test_boot_phase_one_d_is_healthy():
    """启动期 1 个持续 D（< 阈值 2）→ exit 0（宽容，避免 systemd 初始化误判）。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "1", "AO_PROBE_FORCE_BOOT_PHASE": "1", "AO_PROBE_FORCE_UPTIME": "30"})
    assert r.returncode == 0
    assert "boot-phase" in r.stdout


def test_boot_phase_two_d_is_deadlock():
    """启动期 2 个持续 D（>= 阈值 2）→ exit 8。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "2", "AO_PROBE_FORCE_BOOT_PHASE": "1", "AO_PROBE_FORCE_UPTIME": "30"})
    assert r.returncode == 8
    assert "boot-phase" in r.stdout


def test_boot_phase_three_d_is_deadlock():
    """启动期 3 个持续 D → exit 8（远超阈值）。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "3", "AO_PROBE_FORCE_BOOT_PHASE": "1", "AO_PROBE_FORCE_UPTIME": "10"})
    assert r.returncode == 8


# ---------------------------------------------------------------------------
# 3. 阈值边界：uptime 60s 是 boot/running 的分界
# ---------------------------------------------------------------------------


def test_uptime_boundary_59s_is_boot_phase():
    """uptime=59s (< 60) 仍属启动期，1 个 D 判 0。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "1", "AO_PROBE_FORCE_BOOT_PHASE": "1", "AO_PROBE_FORCE_UPTIME": "59"})
    assert r.returncode == 0
    assert "boot-phase" in r.stdout


def test_uptime_boundary_60s_is_running_phase():
    """uptime=120s 属运行期，1 个 D 判 8（严格）。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "1", "AO_PROBE_FORCE_BOOT_PHASE": "0", "AO_PROBE_FORCE_UPTIME": "120"})
    assert r.returncode == 8
    assert "running" in r.stdout


# ---------------------------------------------------------------------------
# 4. 日志格式稳定性（退出码 0/8 的消息格式不可变，调用方依赖解析）
# ---------------------------------------------------------------------------


def test_healthy_log_format_stable():
    """健康日志格式：[OK] WSL kernel healthy (persistent D-state: N) (phase, uptime=Xs)。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "0", "AO_PROBE_FORCE_BOOT_PHASE": "0", "AO_PROBE_FORCE_UPTIME": "120"})
    assert "[OK] WSL kernel healthy (persistent D-state: 0) (running, uptime=120s)" in r.stdout


def test_deadlock_log_format_stable():
    """死锁日志格式：[WARN] N persistent D-state processes (phase) -> kernel polluted。"""
    r = _run_probe({"AO_PROBE_FORCE_DCOUNT": "2", "AO_PROBE_FORCE_BOOT_PHASE": "1", "AO_PROBE_FORCE_UPTIME": "30"})
    assert "[WARN] 2 persistent D-state processes (boot-phase, uptime=30s) -> kernel polluted" in r.stdout


# ---------------------------------------------------------------------------
# 5. 回归：无参调用不报错（修复 start_web_cn.bat 旧 bug）
# ---------------------------------------------------------------------------


def test_no_arg_invocation_works():
    """无位置参数调用脚本不应报错（旧 start_web_cn.bat 曾误传 %WSL_DIR%）。

    注：脚本本就不消费 $1，但显式验证"带一个无害参数"也能正常运行，
    防止未来参数处理改动破坏 bat 的调用契约。set -u 下若误用 $1 会非 0 退出，
    故 returncode==0 即充分信号（无需检查 stderr 内容）。
    """
    env = os.environ.copy()
    env["AO_PROBE_SKIP_SLEEP"] = "1"
    env["AO_PROBE_FORCE_DCOUNT"] = "0"
    env["AO_PROBE_FORCE_BOOT_PHASE"] = "0"
    env["AO_PROBE_FORCE_UPTIME"] = "120"
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    env["WSLENV"] = (
        "AO_PROBE_FORCE_DCOUNT/u:AO_PROBE_FORCE_BOOT_PHASE/u:"
        "AO_PROBE_FORCE_UPTIME/u:AO_PROBE_SKIP_SLEEP/u"
    )
    # 模拟旧 bat 调用：脚本路径后跟一个参数（应被忽略）
    r = subprocess.run(
        ["bash", PROBE_SCRIPT_BASH, "/some/ignored/path"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    stdout = r.stdout.decode("utf-8", errors="replace")
    assert r.returncode == 0  # set -u 下误用未定义 $1 会非 0 退出
    assert "[OK]" in stdout


# ---------------------------------------------------------------------------
# 6. 脚本可执行性前置检查
# ---------------------------------------------------------------------------


def test_script_exists_and_is_bash():
    """探针脚本存在且有 bash shebang（防止路径/部署问题）。"""
    assert PROBE_SCRIPT.exists(), f"探针脚本不存在: {PROBE_SCRIPT}"
    first_line = PROBE_SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert "bash" in first_line, f"shebang 异常: {first_line}"
