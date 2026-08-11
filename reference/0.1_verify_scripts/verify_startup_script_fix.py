#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""启动脚本修复功能验证脚本（可复现）。

验证对象（工作区根目录）：
  - start_web_02.bat / start_web_cn.bat / start_web_02.sh
  - tests/test_startup_scripts_fix.py

覆盖场景：
  [用户旅程] 乱码消除 -> kernel等待机制 -> ECONNREFUSED链路 -> 启动链路顺序
  [补充1]    kernel 未就绪分支契约（ERROR -> 清理 -> exit，前端不启动）
  [补充2]    边界：.sh 语法(bash -n)、.bat CRLF 行尾、括号配平

用法：
  python3 docs/working/verify_startup_script_fix.py
退出码：全部 PASS 返回 0，任一 FAIL 返回 1。
"""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok))
    print(f"[{PASS if ok else FAIL}] {name}" + (f"  ({detail})" if detail else ""))


def read_text(name: str) -> str:
    p = ROOT / name
    assert p.exists(), f"脚本不存在: {p}"
    return p.read_text(encoding="utf-8", errors="replace")


def read_bytes(name: str) -> bytes:
    p = ROOT / name
    assert p.exists(), f"脚本不存在: {p}"
    return p.read_bytes()


print("=" * 64)
print("用户旅程 步骤1: 乱码消除 —— .bat 必须为纯 ASCII（任何代码页不乱码）")
print("=" * 64)
for name in ("start_web_02.bat", "start_web_cn.bat"):
    raw = read_bytes(name)
    non_ascii = [b for b in raw if b > 127]
    check(f"{name} 非 ASCII 字节数为 0", len(non_ascii) == 0, f"非ASCII字节={non_ascii[:10]}")

print()
print("=" * 64)
print("用户旅程 步骤2: kernel 等待机制 —— 轮询 /health 最多 60 次")
print("=" * 64)
bat = read_text("start_web_02.bat")
sh = read_text("start_web_02.sh")

m = re.search(r"for\s+/l\s+%%i\s+in\s+\((1,1,60)\)", bat, re.IGNORECASE)
check("bat kernel 等待循环 for /l %%i in (1,1,60)", bool(m), m.group(0) if m else "未找到")
check("bat 等待循环轮询 /health", "/health" in bat)
m2 = re.search(r"for\s+i\s+in\s+\$\(seq\s+1\s+60\)", sh)
check("sh kernel 等待循环 for i in $(seq 1 60)", bool(m2), m2.group(0) if m2 else "未找到")
check("sh 等待循环轮询 /health", "/health" in sh)
check("旧行为 'continuing anyway' 已移除", "continuing anyway" not in bat)

print()
print("=" * 64)
print("用户旅程 步骤3: ECONNREFUSED 链路 —— 未就绪退出必须在前端启动之前")
print("=" * 64)
abort_pos = bat.find("echo [ERROR] Kernel not ready within 60s")
frontend_pos = bat.find("npx --yes vite")
abort_line = bat[:abort_pos].count(chr(10)) + 1 if abort_pos != -1 else -1
vite_line = bat[:frontend_pos].count(chr(10)) + 1 if frontend_pos != -1 else -1
check("bat 未就绪 ERROR 位于 npx vite 之前",
      0 <= abort_pos < frontend_pos, f"ERROR行{abort_line} < vite行{vite_line}")

pat_exit_bat = re.compile(r"exit\s+/b\s+1")
start_i = bat.rfind('if "!KERNEL_READY!"=="0" (', 0, abort_pos)
end_i = bat.find("exit /b 1", start_i)
block = bat[start_i:end_i + len("exit /b 1")]
check("bat 未就绪分支含 [ERROR]", "[ERROR]" in block)
check("bat 未就绪分支含 exit /b 1", bool(pat_exit_bat.search(block)))
check("bat 未就绪分支含 taskkill 清理", "taskkill" in block)

sh_abort = sh.find("未能在 60 秒内就绪")
sh_frontend = sh.find("npx --yes vite")
sh_abort_line = sh[:sh_abort].count(chr(10)) + 1 if sh_abort != -1 else -1
sh_vite_line = sh[:sh_frontend].count(chr(10)) + 1 if sh_frontend != -1 else -1
check("sh 未就绪 ERROR 位于 npx vite 之前",
      0 <= sh_abort < sh_frontend, f"ERROR行{sh_abort_line} < vite行{sh_vite_line}")

pat_exit_sh = re.compile(r"exit\s+1")
sh_start = sh.find('if [ "$KERNEL_READY" = false ]')
sh_end = sh.find("exit 1", sh_start) + len("exit 1")
sh_block = sh[sh_start:sh_end]
check("sh 未就绪分支含 [ERROR]", "[ERROR]" in sh_block)
check("sh 未就绪分支含 exit 1", bool(pat_exit_sh.search(sh_block)))

print()
print("=" * 64)
print("用户旅程 步骤4: 启动链路顺序 —— 清理->编译->kernel->等待->前端->输出")
print("=" * 64)
positions = {
    "cleanup": bat.find("taskkill /F /T /IM agentos-kernel.exe"),
    "build": bat.find("cargo +stable build"),
    "kernel_start": bat.find('start "AgentOS Kernel"'),
    "kernel_wait": bat.find("Waiting for kernel"),
    "frontend": bat.find("npx --yes vite"),
    "output": bat.find("Open http://localhost"),
}
for k, v in positions.items():
    ln = bat[:v].count(chr(10)) + 1 if v != -1 else -1
    check(f"bat 链路环节存在: {k}", v != -1, f"行{ln}" if v != -1 else "未找到")
order_ok = (positions["cleanup"] < positions["build"] < positions["kernel_start"]
            < positions["kernel_wait"] < positions["frontend"] < positions["output"])
check("bat 链路顺序: 清理<编译<kernel启动<等待<前端<输出", order_ok)

print()
print("=" * 64)
print("补充场景1(错误输入): kernel 未就绪时前端不启动（剥离注释后 ERROR 与 vite 之间无可执行 npx 命令）")
print("=" * 64)


def strip_bat_comments(text: str) -> str:
    """剥离 bat REM 注释行与 :: 注释行，仅保留可执行行。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.upper().startswith("REM") or s.startswith("::"):
            continue
        out.append(line)
    return "\n".join(out)


def strip_sh_comments(text: str) -> str:
    """剥离 sh # 注释行，仅保留可执行行。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


bat_exec = strip_bat_comments(bat[abort_pos:frontend_pos])
sh_exec = strip_sh_comments(sh[sh_abort:sh_frontend])
check("bat ERROR 与 vite 之间无前端启动命令(可执行行)",
      "npx" not in bat_exec, f"可执行行中含npx: {'npx' in bat_exec}")
check("sh ERROR 与 vite 之间无前端启动命令(可执行行)",
      "npx" not in sh_exec, f"可执行行中含npx: {'npx' in sh_exec}")
# exit 语句是 ERROR 分支的终止点：确认 ERROR 分支块内 exit 出现在 npx 之前且为分支收尾
bat_branch = bat[start_i:end_i + len("exit /b 1")]
check("bat ERROR 分支内 exit /b 1 为终止语句(位于分支尾部)",
      "exit /b 1" in bat_branch and bat_branch.rstrip().endswith("exit /b 1"))
sh_branch = sh[sh_start:sh_end]
check("sh ERROR 分支内 exit 1 为终止语句(位于分支尾部)",
      "exit 1" in sh_branch and sh_branch.rstrip().endswith("exit 1"))

print()
print("=" * 64)
print("补充场景2(边界): .sh 语法 / .bat CRLF / 括号配平")
print("=" * 64)
r = subprocess.run(["bash", "-n", str(ROOT / "start_web_02.sh")],
                   capture_output=True, text=True)
check("start_web_02.sh bash -n 语法检查", r.returncode == 0, r.stderr.strip() or "OK")
for name in ("start_web_02.bat", "start_web_cn.bat"):
    raw = read_bytes(name)
    crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
    check(f"{name} 行尾全为 CRLF", crlf == lf, f"CRLF={crlf}, LF={lf}")
    text = read_text(name)
    lines = [l for l in text.splitlines() if not l.strip().upper().startswith("REM")]
    joined = "\n".join(lines)
    opens, closes = joined.count("("), joined.count(")")
    check(f"{name} 括号配平(去REM后)", opens == closes, f"(={opens}, )={closes}")

print()
fails = [n for n, ok in results if not ok]
total = len(results)
print(f"结果: {total - len(fails)}/{total} PASS" + (f", FAIL: {fails}" if fails else ""))
sys.exit(1 if fails else 0)
