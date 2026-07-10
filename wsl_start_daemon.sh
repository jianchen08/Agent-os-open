#!/usr/bin/env bash
# WSL native docker 模式：确保 dockerd 运行并就绪。
# 由 start_web_cn.bat 调用。把原 .bat 里的复杂单行 bash 抽成独立脚本，
# 目的是给所有遍历 /proc 的命令（pgrep/pkill/docker）都加 timeout，
# 避免 WSL2 内核被 D 状态死锁污染时无限挂起。
#
# 退出码约定：
#   0  dockerd 已运行且 /run/docker.sock 可响应（docker ps 成功）
#   7  操作超时（pgrep/pkill/docker 命令卡死，疑似内核 D 状态污染）
#      -> 由上层 start_web_cn.bat 自动 wsl --shutdown 重启内核后重试
set -uo pipefail

# 1. 探测 dockerd 是否已在运行。
#    关键：裸 pgrep -x dockerd 会遍历 /proc，撞上 D 状态进程会无限挂起，
#    必须用 timeout 包住。超时（rc=124）视为"状态不可探知"=疑似污染，返回 7。
if timeout 8 pgrep -x dockerd >/dev/null 2>&1; then
    : # dockerd 进程存在，继续校验 socket
elif [ $? -eq 124 ]; then
    echo "[WARN] pgrep dockerd 超时（疑似内核 D 状态污染）"
    exit 7
else
    # dockerd 未运行，尝试启动
    echo "[INFO] dockerd 未运行，启动中..."
    # 清理可能的残留进程（同样加 timeout）
    timeout 8 pkill -TERM dockerd 2>/dev/null
    [ $? -eq 124 ] && { echo "[WARN] pkill -TERM dockerd 超时"; exit 7; }
    sleep 2
    timeout 8 pkill -9 dockerd 2>/dev/null
    [ $? -eq 124 ] && { echo "[WARN] pkill -9 dockerd 超时"; exit 7; }
    rm -f /var/run/docker.pid
    # nohup 启动 dockerd（后台），等待 6s 让它初始化
    nohup dockerd >/tmp/dockerd.log 2>&1 &
    sleep 6
fi

# 2. 轮询等待 docker.sock 就绪且 daemon 可响应
#    每轮 docker ps 都套 timeout；一旦某轮超时即判污染返回 7，
#    不让循环退化成无限挂起。
for i in 1 2 3 4 5 6 7 8 9 10; do
    if timeout 8 docker ps >/dev/null 2>&1; then
        echo "[OK] dockerd 就绪"
        exit 0
    fi
    rc=$?
    if [ "$rc" -eq 124 ]; then
        echo "[WARN] docker ps 轮询超时（疑似内核 D 状态污染）"
        exit 7
    fi
    sleep 2
done

echo "[ERROR] dockerd 启动后 10 轮探测均不可用，诊断: tail -30 /tmp/dockerd.log"
exit 1
