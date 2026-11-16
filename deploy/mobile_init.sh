#!/bin/bash
# AgentOS 移动端首启装配（deploy/build_mobile.sh rootfs 种子随包，proot 内执行）。
#
# 【已退役 2026-11-01】venv 装配已打包期预烤进种子（materialize_seed.py 直接
# 产出 opt/agentos/plugin-venvs/<plugin_id>/ 装配后布局，start_stack.sh 经
# AGENTOS_PLUGIN_VENVS_DIR 指向）；设备端不再执行本脚本，文件保留回滚兼容。
#
# deploy/init.sh 的离线等价物：零网络，纯本地从预 sync 快照装配 venv。
# 禁现场 uv sync（移动网络不可靠，构建期已按坑⑳平台钉死装齐）。
#
# 输入（种子随包内容物）：
#   /opt/agentos/venvs/<dir>/lib/python3.12/site-packages   预 sync 快照（按插件目录名）
#   /opt/agentos/python                                      PBS cpython 3.12 基座
# 输出（内核 venv 登记处布局，ADR 2026-09-28）：
#   <AGENTOS_USER_ROOT>/plugin-venvs/<plugin_id>/{bin/python, pyvenv.cfg,
#                                                 lib/python3.12/site-packages}
# 键位对账（R5 MuMu 实测坑㉜）：内核 spawn 期用户空间登记处按 **plugin manifest
# id** 取键（find_user_space_venv_interpreter(GROUP_HOST_DIR / manifest.id)），
# id 与插件目录名可不同（llm_core 目录 → pipeline_llm_core id、bash → bash_tool），
# 必须经 plugin.json 映射，目录名直用会 spawn 期 VENV_INTERPRETER_MISSING。
# 幂等：bin/python 已在且快照已搬走=已装配，跳过（重入安全）。

set -euo pipefail

AGENTOS_ROOT=/opt/agentos
PY_BASE="$AGENTOS_ROOT/python/bin/python3.12"
VENV_SRC="$AGENTOS_ROOT/venvs"
USER_ROOT="${AGENTOS_USER_ROOT:-$AGENTOS_ROOT/user_root}"
VENV_DST="$USER_ROOT/plugin-venvs"

# 存在性用 -e 不用 -x：proot 内 access(X_OK) 探测被破坏（恒 EACCES，真机
# 实测 2026-11-01），-x 会把完好的基座误报为缺失；可执行性由 exec 时自证。
if [ ! -e "$PY_BASE" ]; then
    echo "[mobile-init][ERROR] 基座解释器缺失: $PY_BASE（种子不完整?）" >&2
    exit 1
fi

# 目录名 → plugin manifest id（无 plugin.json 或解析失败回退目录名）
resolve_key() {
    mf="$AGENTOS_ROOT/plugins/shared/$1/plugin.json"
    if [ -f "$mf" ] && command -v python3 >/dev/null 2>&1; then
        python3 -c 'import json,sys
try:
    print(json.load(open(sys.argv[1], encoding="utf-8")).get("id") or "")
except Exception:
    print("")' "$mf" 2>/dev/null || true
    return 0
    fi
    echo ""
}

count=0
failed=""
for d in "$VENV_SRC"/*/; do
    name="$(basename "$d")"
    sp="$d/lib/python3.12/site-packages"
    key="$(resolve_key "$name")"
    [ -n "$key" ] || key="$name"
    vdir="$VENV_DST/$key"
    # 已装配判定=bin/python 与 site-packages **双条件**：只查 bin 会把
    # 「mkdir/软链成、搬运前崩」的半装配态永久跳过（R5 MuMu 实测坑㉟：
    # cohost 无 SDK，group:light 全灭）
    if [ -f "$vdir/bin/python" ] && [ -d "$vdir/lib/python3.12/site-packages" ]; then
        echo "[mobile-init] $key 已装配，跳过"
        count=$((count + 1))
        continue
    fi
    if [ ! -d "$sp" ]; then
        # 源缺失：若目标 site-packages 已被并发实例装配到位，视为成功（幂等收敛，
        # 双实例竞态下后到者不再 fail-fast 拖崩整链——R5 MuMu 实测坑㉝）
        if [ -d "$vdir/lib/python3.12/site-packages" ]; then
            echo "[mobile-init] $key 已由并发装配，视为成功"
            count=$((count + 1))
            continue
        fi
        echo "[mobile-init][ERROR] $name 快照 site-packages 缺失（种子不完整?）" >&2
        failed="$failed $name"
        continue
    fi
    mkdir -p "$vdir/bin" "$vdir/lib/python3.12"
    ln -sfn "$PY_BASE" "$vdir/bin/python"
    ln -sfn "$PY_BASE" "$vdir/bin/python3"
    # site-packages 搬运：同文件系统 rename 原子直达；失败回退 cp（错误可见不吞）
    if ! mv "$sp" "$vdir/lib/python3.12/site-packages" 2>/tmp/mi_mv_err; then
        echo "[mobile-init] $name mv 回退 cp（$(cat /tmp/mi_mv_err 2>/dev/null)）"
        mkdir -p "$vdir/lib/python3.12/site-packages"
        cp -a "$sp/." "$vdir/lib/python3.12/site-packages/"
        rm -rf "$sp"
    fi
    # pyvenv.cfg：CPython getpath 据此定位 venv 身份（home=基座目录）
    printf 'home = %s\nversion = 3.12.15\ninclude-system-site-packages = false\nexecutable = %s\n' \
        "$AGENTOS_ROOT/python" "$PY_BASE" > "$vdir/pyvenv.cfg"
    echo "[mobile-init] venv 装配: $name -> $key"
    count=$((count + 1))
done

if [ "$count" -eq 0 ]; then
    echo "[mobile-init][ERROR] 零 venv 装配（$VENV_SRC 快照缺失?）" >&2
    exit 1
fi
if [ -n "$failed" ]; then
    echo "[mobile-init][ERROR] 装配失败:$failed" >&2
    exit 1
fi
mkdir -p "$USER_ROOT"
date -u +"assembled=%FT%TZ venvs=$count" > "$USER_ROOT/.mobile_init_done"
echo "[mobile-init] 完成: $count venv"
