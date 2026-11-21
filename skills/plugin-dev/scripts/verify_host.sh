#!/usr/bin/env bash
# 隔离环境（WSL）命令转发入口：把宿主验证脚本经 WSL interop 跑起来。
#
# 用法（在任务工作空间根执行）：
#   bash skills/plugin-dev/scripts/verify_host.sh <plugin 相对路径> [frontend] [kernel_bin_win]
#
# 参数：
#   frontend      跑前端浏览器车道（Playwright 静态伺服 + 反代 + 截图）
#   kernel_bin_win 可选，宿主 Windows 路径（装机包内内核 exe；缺省 dev 仓内产物）
#
# 机制（ADR 2026-11-10 实测 + e2e 经验）：
#   - WSL interop 直接执行 Windows 进程，不需要 broker/端口；
#   - 路径先 wslpath -w 转换，整条命令落盘成 .bat 再执行（命令行内联路径会被
#     interop 引号双重复原，e2e 实测坑）；
#   - **退出码不可信**：本机实测 `cmd.exe /c exit 7` 从 WSL 侧看恒为 0，靠 $?
#     会假绿——宿主 bat 把真实退出码写进 <workspace>\.agentos_verify_rc，本脚本
#     读该文件回传；文件缺失/非法按失败（2）处理。
set -euo pipefail

plugin_rel="${1:?用法: verify_host.sh <plugin 相对路径> [frontend] [kernel_bin_win]}"
mode="${2:-}"
kernel_win="${3:-}"

ws_win="$(wslpath -w "$PWD")"
script_win="$ws_win\\skills\\plugin-dev\\scripts\\verify_host.py"
bat="$PWD/.agentos_verify_host.bat"
rc_file="$PWD/.agentos_verify_rc"
py="${AGENTOS_HOST_PYTHON:-python}"

trap 'rm -f "$bat" "$rc_file"' EXIT
rm -f "$rc_file"

{
    echo "@echo off"
    echo "chcp 65001 >nul"
    echo "set \"PYTHONUTF8=1\""
    echo "set \"PYTHONIOENCODING=utf-8\""
    echo "cd /d \"$ws_win\""
    if [ "$mode" = "frontend" ] && [ -n "$kernel_win" ]; then
        echo "\"$py\" \"$script_win\" \"$plugin_rel\" --workspace \"$ws_win\" --frontend --kernel-bin \"$kernel_win\""
    elif [ "$mode" = "frontend" ]; then
        echo "\"$py\" \"$script_win\" \"$plugin_rel\" --workspace \"$ws_win\" --frontend"
    elif [ -n "$kernel_win" ]; then
        echo "\"$py\" \"$script_win\" \"$plugin_rel\" --workspace \"$ws_win\" --kernel-bin \"$kernel_win\""
    else
        echo "\"$py\" \"$script_win\" \"$plugin_rel\" --workspace \"$ws_win\""
    fi
    echo "set \"RC=%ERRORLEVEL%\""
    echo "> \"$ws_win\\.agentos_verify_rc\" echo %RC%"
    echo "exit /b %RC%"
} > "$bat"

cmd.exe /c "$(wslpath -w "$bat")" || true

if [ ! -f "$rc_file" ]; then
    echo "verify_host: 宿主未回传退出码（$rc_file 缺失）——按失败处理" >&2
    exit 2
fi
rc="$(tr -d '[:space:]' < "$rc_file")"
case "$rc" in
    ''|*[!0-9]*) echo "verify_host: 非法退出码 '$rc'——按失败处理" >&2; exit 2 ;;
esac
exit "$rc"
