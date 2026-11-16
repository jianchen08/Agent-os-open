#!/bin/bash
# AgentOS 移动端栈启动（proot 内前台执行；进程树挂 app 侧前台服务保活）。
#
# 职责单一：export 内核所需 env → exec 内核（前台）。种子无 .env 文件——
# 口令/令牌/端口由壳（KernelHostPlugin）经 proot env 注入，随 SharedPreferences
# 持久化，重启恢复同值（restart_recovery 会话连续性前提）。
# 注意：不设 AGENTOS_PLUGIN_VENV_AUTOPROVISION——venv 已在打包期以装配后布局
# 预烤进种子（materialize_seed.py 烘焙，登记处=/opt/agentos/plugin-venvs），
# 设备端零装配，autoprovision 的 uv sync 在设备网络下不可靠也不需要。

set -euo pipefail

export AGENTOS_BIND=127.0.0.1
export AGENTOS_KERNEL_PORT="${AGENTOS_KERNEL_PORT:-9300}"
export AGENTOS_PLUGINS_DIR=/opt/agentos/plugins/shared
export AGENTOS_CONFIG_ROOT=/opt/agentos/config
export AGENTOS_USER_ROOT=/opt/agentos/user_root
# venv 登记处=种子预烤位（kernel user_plugin_venvs_dir 的 env 覆盖键）；
# user_root/plugin-venvs 是旧装机装配残留位，升级装后自然废弃。
export AGENTOS_PLUGIN_VENVS_DIR=/opt/agentos/plugin-venvs
export AGENTOS_DB_PATH="$AGENTOS_USER_ROOT/agentos_kernel.db"
export PATH="/opt/agentos/python/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin:$PATH"

cd /opt/agentos
mkdir -p logs
exec /opt/agentos/agentos-kernel
