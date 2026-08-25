"""启动脚本契约回归测试（start_web_cn.bat / start_web_02.bat / start_web_02.sh）。

背景（为什么这些测试重要）：
- 乱码问题：脚本内容必须为纯 ASCII（或 GBK 编码），任何代码页下都不会乱码
  （中文注释在 GBK 终端按错误代码页解码会破坏 REM 注释、把乱码当命令执行）。
- kernel 等待机制：轮询 /health 直到就绪（最多 60s），未就绪则报错退出、
  不启动前端（不得在 kernel 未就绪时启动前端导致 vite 代理 ECONNREFUSED）。

本测试为静态行为测试：验证脚本文件的可观察契约（无乱码字节、等待循环、未就绪退出路径）。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCRIPTS = {
    "start_web_02.bat": ROOT / "start_web_02.bat",
    "start_web_cn.bat": ROOT / "start_web_cn.bat",
    "start_web_02.sh": ROOT / "start_web_02.sh",
}


def _read_text(script_name: str) -> str:
    path = SCRIPTS[script_name]
    assert path.exists(), f"脚本不存在: {path}"
    return path.read_text(encoding="utf-8", errors="replace")


def _read_bytes(script_name: str) -> bytes:
    path = SCRIPTS[script_name]
    assert path.exists(), f"脚本不存在: {path}"
    return path.read_bytes()


# ---------------------------------------------------------------------------
# AC1: 无乱码 —— bat 脚本必须为纯 ASCII，任何代码页下都不会产生乱码命令
# ---------------------------------------------------------------------------

def test_ac1_bat_scripts_are_pure_ascii():
    """start_web_02.bat / start_web_cn.bat 必须不含任何非 ASCII 字节。

    一旦出现非 ASCII 字节（UTF-8 中文或 em dash），在 GBK 终端下
    cmd 解码错位就会把注释/echo 内容解析成命令（'�出' not recognized）。
    纯 ASCII 在任何代码页（936/65001）下解析结果一致。
    """
    # 乱码问题只影响 cmd 批处理（GBK 终端解析 UTF-8 字节流错位）。
    # .sh 是 bash 脚本，天然按 UTF-8 处理中文，不在此检查范围。
    for name in ("start_web_02.bat", "start_web_cn.bat"):
        raw = _read_bytes(name)
        non_ascii = [b for b in raw if b > 127]
        assert not non_ascii, (
            f"{name} 含 {len(non_ascii)} 个非 ASCII 字节，GBK 终端下会乱码。"
            f"请将脚本内容改为纯 ASCII（中文注释/破折号替换为英文/ASCII）。"
        )


# ---------------------------------------------------------------------------
# AC2: kernel 等待机制 —— 轮询 /health 直到就绪（最多 60s），未就绪则报错退出
# ---------------------------------------------------------------------------

def test_ac2_kernel_wait_loop_is_60_seconds():
    """start_web_02.bat / start_web_02.sh 的 kernel 等待循环必须为 60 次（最多 60s）。

    契约：kernel 未就绪时最多等 60s（不得 15s 就 "continuing anyway" 启动前端，
    导致后续 /api/v1/* 请求全部 ECONNREFUSED）。
    """
    bat = _read_text("start_web_02.bat")
    # 等待循环：for /l %%i in (1,1,60) do
    assert re.search(r"for\s+/l\s+%%i\s+in\s+\(1,1,60\)", bat, re.IGNORECASE), (
        "start_web_02.bat kernel 等待循环应为 60 次（(1,1,60)）"
    )
    # 等待循环必须轮询 /health 端点
    assert re.search(r"/health", bat), "start_web_02.bat 等待循环必须轮询 /health 端点"

    sh = _read_text("start_web_02.sh")
    assert re.search(r"seq\s+1\s+60", sh), "start_web_02.sh kernel 等待循环应为 60 次"
    assert re.search(r"/health", sh), "start_web_02.sh 等待循环必须轮询 /health 端点"


def test_ac3_kernel_not_ready_aborts_before_frontend():
    """kernel 未就绪时必须报错退出，不得继续启动前端。

    契约：未就绪 → 输出 [ERROR] → 退出（不得 "continuing anyway" 后启动
    前端，否则 vite 代理请求 kernel 时 ECONNREFUSED）。
    """
    bat = _read_text("start_web_02.bat")
    # 契约：不得存在 "continuing anyway" 放行路径
    assert "continuing anyway" not in bat, "'continuing anyway' 放行路径必须不存在"
    # 未就绪分支必须报错并退出
    assert re.search(r"\[ERROR\].*not ready", bat, re.IGNORECASE), (
        "kernel 未就绪时必须输出 [ERROR] 信息"
    )
    assert re.search(r"exit\s+/b\s+1", bat), "kernel 未就绪时必须 exit /b 1"

    # 退出必须发生在前端启动（npx vite）之前
    abort_pos = bat.find("not ready within 60s")
    frontend_pos = bat.find("npx --yes vite")
    assert abort_pos != -1 and frontend_pos != -1, "找不到退出分支或前端启动位置"
    assert abort_pos < frontend_pos, (
        "kernel 未就绪的退出逻辑必须位于前端启动之前（否则前端仍会被启动）"
    )

    sh = _read_text("start_web_02.sh")
    assert "seq 1 60" in sh, "start_web_02.sh kernel 等待应为 60s"
    # .sh 未就绪时也必须有报错退出（错误关键字支持中英文，与 AC 描述对齐）
    assert re.search(r"\[ERROR\].*(?:未能在|not ready|failed)", sh, re.IGNORECASE), (
        "start_web_02.sh kernel 未就绪时必须输出 [ERROR]（含'未能在'/'not ready'/'failed'关键字）"
    )
    assert re.search(r"exit\s+1", sh), "start_web_02.sh kernel 未就绪时必须 exit 1"


# ---------------------------------------------------------------------------
# AC4: 启动链路完整性 —— 清理旧进程 → 编译 → kernel 就绪 → 前端启动 → 输出地址
# ---------------------------------------------------------------------------

def test_ac4_startup_chain_order():
    """start_web_02.bat 启动链路顺序必须满足：
    清理旧进程 → 编译（可选）→ kernel 启动+等待就绪 → 前端启动 → 输出访问地址。
    """
    bat = _read_text("start_web_02.bat")
    positions = {
        "cleanup": bat.find("taskkill /F /T /IM agentos-kernel.exe"),
        "build": bat.find("cargo +stable build"),
        "kernel_start": bat.find('start "AgentOS Kernel"'),
        "kernel_wait": bat.find("Waiting for kernel"),
        "frontend": bat.find("npx --yes vite"),
        "output": bat.find("Open http://localhost"),
    }
    for name, pos in positions.items():
        assert pos != -1, f"start_web_02.bat 缺少启动链路环节: {name}"

    assert positions["cleanup"] < positions["build"] < positions["kernel_start"] \
        < positions["kernel_wait"] < positions["frontend"] < positions["output"], (
        "启动链路顺序错误，必须为：清理 → 编译 → kernel 启动 → 等待就绪 → 前端 → 输出地址"
    )
