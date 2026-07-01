#!/usr/bin/env bash
# =============================================================================
# system_dashboard.sh
# -----------------------------------------------------------------------------
# 一个一次性展示系统关键信息的 Bash 仪表盘。
#
# 功能模块：
#   1. 主机名 / 当前用户 / 内核版本
#   2. 系统运行时间 (uptime)
#   3. CPU 使用率 (top 5 进程)
#   4. 内存使用情况 (total / used / free / percentage)
#   5. 磁盘使用情况 (df -h, 重点显示 / 分区)
#   6. 网络 IP (hostname -I / macOS 兼容)
#   7. 当前日期时间 (ANSI 高亮)
#
# 用法：
#   ./system_dashboard.sh           # 启用 ANSI 颜色
#   ./system_dashboard.sh --no-color # 关闭颜色
#
# 兼容：Linux / macOS
# =============================================================================

set -u

# -----------------------------------------------------------------------------
# 0. 参数解析 & 颜色开关
# -----------------------------------------------------------------------------
USE_COLOR=1
for arg in "$@"; do
    case "$arg" in
        --no-color|--no-colour|-n)
            USE_COLOR=0
            ;;
        -h|--help)
            echo "用法: $0 [--no-color]"
            exit 0
            ;;
    esac
done

# -----------------------------------------------------------------------------
# 1. ANSI 颜色辅助
#    当 USE_COLOR=0 时,所有颜色函数输出空字符串,保证纯文本可读。
# -----------------------------------------------------------------------------
if [ "$USE_COLOR" -eq 1 ]; then
    C_RESET=$'\033[0m'
    C_BOLD=$'\033[1m'
    C_DIM=$'\033[2m'
    C_RED=$'\033[31m'
    C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'
    C_MAGENTA=$'\033[35m'
    C_CYAN=$'\033[36m'
    C_BG_BLUE=$'\033[44m'
    C_BG_MAGENTA=$'\033[45m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""
    C_RED=""; C_GREEN=""; C_YELLOW=""
    C_BLUE=""; C_MAGENTA=""; C_CYAN=""
    C_BG_BLUE=""; C_BG_MAGENTA=""
fi

# -----------------------------------------------------------------------------
# 2. 平台检测
# -----------------------------------------------------------------------------
detect_os() {
    case "$(uname -s)" in
        Linux*)  echo "linux" ;;
        Darwin*) echo "darwin" ;;
        *)       echo "unknown" ;;
    esac
}
OS_TYPE="$(detect_os)"

# -----------------------------------------------------------------------------
# 3. 通用输出工具
# -----------------------------------------------------------------------------

# 打印模块标题(粗体 + 青色,加上下划线分隔)
print_header() {
    local title="$1"
    printf "\n${C_BOLD}${C_CYAN}┌─ %s ─────────────────────────────────────────${C_RESET}\n" "$title"
}

# 打印 key : value 一行
print_kv() {
    local key="$1"; shift
    local val="$1"
    printf "${C_BOLD}${C_YELLOW}  %-18s${C_RESET}${C_DIM}:${C_RESET} %s\n" "$key" "$val"
}

# 段间分隔
print_separator() {
    printf "${C_DIM}%s${C_RESET}\n" "──────────────────────────────────────────────────────────────"
}

# 顶层大标题
print_banner() {
    printf "${C_BOLD}${C_BG_MAGENTA}                                                        ${C_RESET}\n"
    printf "${C_BOLD}${C_BG_MAGENTA}       🖥️   SYSTEM INFORMATION DASHBOARD   🖥️        ${C_RESET}\n"
    printf "${C_BOLD}${C_BG_MAGENTA}                                                        ${C_RESET}\n"
    printf "${C_DIM}  平台: %-8s   颜色: %s   PID: %s${C_RESET}\n" \
        "$OS_TYPE" \
        "$([ "$USE_COLOR" -eq 1 ] && echo "ON" || echo "OFF")" \
        "$$"
}

# -----------------------------------------------------------------------------
# 4. 模块函数
# -----------------------------------------------------------------------------

# 4.1 主机 / 内核 / 当前用户
show_host_info() {
    print_header "主机信息 / Host Info"
    print_kv "Hostname"  "$(hostname 2>/dev/null || echo 'N/A')"
    print_kv "User"      "${USER:-$(whoami 2>/dev/null || echo 'N/A')}"
    print_kv "Kernel"    "$(uname -r 2>/dev/null || echo 'N/A')"
    print_kv "OS"        "$(uname -srm 2>/dev/null || echo 'N/A')"
}

# 4.2 系统运行时间
show_uptime() {
    print_header "运行时间 / Uptime"
    local up
    up="$(uptime 2>/dev/null || echo 'N/A')"
    printf "  ${C_GREEN}%s${C_RESET}\n" "$up"
}

# 4.3 CPU Top 5 进程(按 CPU 占用排序)
show_cpu_top() {
    print_header "CPU 使用率 Top 5 / Top Processes"
    printf "${C_DIM}  %-8s %-6s %-6s %s${C_RESET}\n" "PID" "CPU%" "MEM%" "COMMAND"

    if [ "$OS_TYPE" = "darwin" ]; then
        # macOS: ps -A -o pid,pcpu,pmem,comm | sort -k2 -nr | head -n 6
        ps -A -o pid=,pcpu=,pmem=,comm= 2>/dev/null \
            | sort -k2 -nr \
            | head -n 5 \
            | while read -r pid cpu mem cmd; do
                printf "  ${C_BOLD}${C_MAGENTA}%-8s${C_RESET} ${C_RED}%-6s${C_RESET} ${C_YELLOW}%-6s${C_RESET} %s\n" \
                    "$pid" "$cpu" "$mem" "$cmd"
            done
    else
        # Linux: ps -eo pid,pcpu,pmem,comm --sort=-pcpu
        ps -eo pid=,pcpu=,pmem=,comm= --sort=-pcpu 2>/dev/null \
            | head -n 5 \
            | while read -r pid cpu mem cmd; do
                printf "  ${C_BOLD}${C_MAGENTA}%-8s${C_RESET} ${C_RED}%-6s${C_RESET} ${C_YELLOW}%-6s${C_RESET} %s\n" \
                    "$pid" "$cpu" "$mem" "$cmd"
            done
    fi
}

# 4.4 内存(Linux 走 /proc/meminfo,macOS 走 vm_stat + sysctl)
show_memory() {
    print_header "内存使用 / Memory"
    if [ "$OS_TYPE" = "linux" ] && [ -r /proc/meminfo ]; then
        local total used free shared buff_cache available percent
        total=$(awk '/^MemTotal:/     {print $2}' /proc/meminfo)
        avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
        # used = total - available(更准确),若没有 available 则回退到 total - free - buffers - cached
        if [ -n "$avail" ]; then
            used=$((total - avail))
        else
            free=$(awk '/^MemFree:/    {print $2}' /proc/meminfo)
            b=$(awk '/^Buffers:/    {print $2}' /proc/meminfo)
            c=$(awk '/^Cached:/     {print $2}' /proc/meminfo)
            used=$((total - free - b - c))
        fi
        free_kb=$(awk '/^MemFree:/     {print $2}' /proc/meminfo)
        # 百分比
        if [ "$total" -gt 0 ]; then
            percent=$(awk -v u="$used" -v t="$total" 'BEGIN{printf "%.1f", (u/t)*100}')
        else
            percent="0.0"
        fi
        # 转 MB 显示
        awk -v t="$total" -v u="$used" -v f="$free_kb" -v p="$percent" \
            'BEGIN{
                printf "  Total:      %8.2f MB\n", t/1024;
                printf "  Used:       %8.2f MB\n", u/1024;
                printf "  Free:       %8.2f MB\n", f/1024;
                printf "  Usage:      %8s %%\n",   p;
            }' \
            | while IFS= read -r line; do
                if echo "$line" | grep -q "Usage:"; then
                    printf "  ${C_BOLD}%s${C_RESET}\n" "$line"
                else
                    printf "  %s\n" "$line"
                fi
            done
    elif [ "$OS_TYPE" = "darwin" ]; then
        # macOS: 用 vm_stat + sysctl hw.memsize 算 used / free
        local page_size total_bytes free_bytes used_bytes percent
        page_size=$(vm_stat 2>/dev/null | awk '/page size of/ {print $8}')
        total_bytes=$(sysctl -n hw.memsize 2>/dev/null)
        # free = (free + inactive + speculative) * page_size
        local free_pages inactive_pages spec_pages
        free_pages=$(vm_stat 2>/dev/null | awk '/Pages free:/     {gsub(/\./,"",$3); print $3}')
        inactive_pages=$(vm_stat 2>/dev/null | awk '/Pages inactive:/ {gsub(/\./,"",$3); print $3}')
        spec_pages=$(vm_stat 2>/dev/null | awk '/Pages speculative:/ {gsub(/\./,"",$3); print $3}')
        free_bytes=$(awk -v fs="$free_pages" -v is="$inactive_pages" -v ss="$spec_pages" -v ps="$page_size" \
                     'BEGIN{print (fs+is+ss)*ps}')
        used_bytes=$(awk -v t="$total_bytes" -v f="$free_bytes" 'BEGIN{print t-f}')
        percent=$(awk -v u="$used_bytes" -v t="$total_bytes" 'BEGIN{printf "%.1f", (u/t)*100}')

        awk -v t="$total_bytes" -v u="$used_bytes" -v f="$free_bytes" -v p="$percent" \
            'BEGIN{
                printf "  Total:      %8.2f MB\n", t/1024/1024;
                printf "  Used:       %8.2f MB\n", u/1024/1024;
                printf "  Free:       %8.2f MB\n", f/1024/1024;
                printf "  Usage:      %8s %%\n",   p;
            }' \
            | while IFS= read -r line; do
                if echo "$line" | grep -q "Usage:"; then
                    printf "  ${C_BOLD}%s${C_RESET}\n" "$line"
                else
                    printf "  %s\n" "$line"
                fi
            done
    else
        printf "  ${C_DIM}(内存信息在当前平台不可用)${C_RESET}\n"
    fi
}

# 4.5 磁盘 (df -h,重点 / 分区)
show_disk() {
    print_header "磁盘使用 / Disk Usage"
    if [ "$OS_TYPE" = "darwin" ]; then
        df -h | head -n 1 | while IFS= read -r line; do
            printf "  ${C_DIM}%s${C_RESET}\n" "$line"
        done
        df -h | tail -n +2 | while IFS= read -r line; do
            if echo "$line" | grep -qE " /$"; then
                printf "  ${C_BOLD}${C_GREEN}%-60s${C_RESET}\n" "$line"
            else
                printf "  %s\n" "$line"
            fi
        done
    else
        # Linux: 优先显示 / 分区
        df -h --output=source,size,used,avail,pcent,target 2>/dev/null \
            | head -n 1 \
            | while IFS= read -r line; do
                printf "  ${C_DIM}%s${C_RESET}\n" "$line"
            done
        df -h --output=source,size,used,avail,pcent,target 2>/dev/null \
            | tail -n +2 \
            | while IFS= read -r line; do
                if echo "$line" | awk '{print $6}' | grep -qE "^/$"; then
                    printf "  ${C_BOLD}${C_GREEN}%-60s${C_RESET}\n" "$line"
                else
                    printf "  %s\n" "$line"
                fi
            done
    fi
}

# 4.6 网络 IP(Linux: hostname -I;macOS: ipconfig getifaddr en0 兜底)
show_network() {
    print_header "网络 IP / Network"
    local ip
    if [ "$OS_TYPE" = "darwin" ]; then
        ip=$(ipconfig getifaddr en0 2>/dev/null \
            || hostname -I 2>/dev/null \
            || ifconfig 2>/dev/null | awk '/inet /{print $2; exit}' \
            || echo "N/A")
    else
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
        if [ -z "$ip" ]; then
            ip=$(ip -4 addr show 2>/dev/null | awk '/inet /{print $2}' | head -n 1 | cut -d/ -f1)
        fi
    fi
    [ -z "$ip" ] && ip="N/A"
    print_kv "Primary IP" "$ip"
    print_kv "FQDN"       "$(hostname -f 2>/dev/null || hostname)"
}

# 4.7 日期时间(ANSI 高亮)
show_datetime() {
    print_header "当前日期时间 / Date & Time"
    local now
    now=$(date "+%Y-%m-%d %H:%M:%S %Z (%A)")
    printf "  ${C_BOLD}${C_BG_BLUE}${C_BOLD}  %s  ${C_RESET}\n" "$now"
    printf "  ${C_DIM}Epoch: %s${C_RESET}\n" "$(date +%s)"
}

# -----------------------------------------------------------------------------
# 5. 主流程
# -----------------------------------------------------------------------------
main() {
    print_banner
    print_separator

    show_host_info
    show_uptime
    show_cpu_top
    show_memory
    show_disk
    show_network
    show_datetime

    print_separator
    printf "${C_DIM}  ✅ Dashboard generated at %s${C_RESET}\n" "$(date '+%H:%M:%S')"
    printf "\n"
}

main "$@"
