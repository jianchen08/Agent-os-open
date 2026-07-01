#!/usr/bin/env bash
# 启动 dockerd 并等待就绪（WSL native docker 模式）。
# 由 start_web_cn.bat 调用——把复杂的 bash 逻辑放在这里，
# 避免在 .bat 的双引号里嵌套 $(cat) & 等特殊字符被 CMD 错误解析。
#
# 幂等：dockerd 已在运行则直接返回。
# 退出码：0=就绪  1=启动失败
set -uo pipefail

# 若已就绪，直接返回
if [ -S /run/docker.sock ] && timeout 5 docker ps >/dev/null 2>&1; then
    exit 0
fi

# 清理可能残留的旧 dockerd（按 PID 文件定向 kill，不用 pkill 避免遍历 /proc 卡死）
if [ -f /var/run/docker.pid ]; then
    kill "$(cat /var/run/docker.pid 2>/dev/null)" 2>/dev/null
    sleep 2
fi
rm -f /var/run/docker.pid

# 后台启动 dockerd
nohup dockerd >/tmp/dockerd.log 2>&1 &
sleep 6

# 等待 docker 就绪
for i in 1 2 3 4 5 6 7 8 9 10; do
    [ -S /run/docker.sock ] && timeout 5 docker ps >/dev/null 2>&1 && exit 0
    sleep 2
done

echo "[ERROR] dockerd 启动失败，日志: tail -30 /tmp/dockerd.log" >&2
exit 1
