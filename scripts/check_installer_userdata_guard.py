#!/usr/bin/env python3
"""安装器用户数据/凭据保留 + 免管理员安装闸。

用户裁定（2026-11-08）：安装/更新操作不得清理用户数据与凭据事实源——只要
用户数据未被显式清除，升级后不得要求重新登录。用户裁定（2026-11-12）：
安装应用不得使用管理员账户。本闸对安装器与打包配置做机械检查（违规即红，
fail-closed）：

1. 口令播种不可删：NSIS 自定义脚本不得删除/改写注册表口令播种
   ``HKCU\\Environment\\AGENTOS_ADMIN_PASSWORD``。2026-10-22 / 2026-11-06 /
   2026-11-08 三轮「升级即重登」的根因：卸载器删 env → NSIS 升级流程先跑旧
   卸载器 → 重装播种全新随机口令 → 内核按 env 校验不匹配即重置口令并吊销
   全部会话 → 已发令牌全灭。
2. 用户数据面不可清：安装器不得 ``RMDir $APPDATA``；electron-builder nsis
   配置不得开启 ``deleteAppDataOnUninstall``。
3. 播种保留语义在位（正面断言）：customInstall 必须保留「已存在则原样保留」
   守卫（ReadRegStr + 空值守卫），防口令轮换以「每次强制重播种」形态回潮。
4. 免管理员安装：electron-builder nsis 配置的 ``perMachine`` 必须为
   false/缺省。true = 模板 RequestExecutionLevel admin（manifest
   requireAdministrator）+ 装到 Program Files，双击/静默安装都必须提权，
   与用户裁定「安装应用不得使用管理员账户」冲突
   （ADR 2026-11-12-installer-per-user-no-admin）。

触面登记（新增安装器/打包配置破坏面时在此扩清单；扩面 = 闸变更走
gate-change 流程）：
- electron/nsis/installer-custom.nsh（安装器自定义宏集）
- package.json（electron-builder nsis 配置块）

退出码：0=通过；1=存在违规；2=触面文件缺失（fail-closed，不许静默跳过）。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER_NSH = ROOT / "electron" / "nsis" / "installer-custom.nsh"
PACKAGE_JSON = ROOT / "package.json"

ENV_VAR = "AGENTOS_ADMIN_PASSWORD"
# 播种保留守卫的正面断言锚（customInstall 的 ReadRegStr + 空值守卫）。
PRESERVE_READ_ANCHOR = f'ReadRegStr $AgentOsAdminPassword HKCU "Environment" "{ENV_VAR}"'
PRESERVE_GUARD_ANCHOR = '${If} $AgentOsAdminPassword == ""'


def check_installer_nsh(text: str) -> list[str]:
    """安装器自定义脚本保留契约检查。注释行（; 头）不执法。"""
    violations: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if stripped.startswith(";"):
            continue
        if ENV_VAR in raw and (
            "DeleteRegValue" in raw or "DeleteRegKey" in raw
        ):
            violations.append(
                f"electron/nsis/installer-custom.nsh:{lineno}: "
                f"删除口令播种 {ENV_VAR}——NSIS 升级先跑旧卸载器，删除即升级轮换"
                "口令（内核重置口令+吊销全部会话=用户被打回登录框）；"
                "ADR 2026-11-08-installer-credential-retention 禁止"
            )
        if re.search(r"RMDir\s+/r\s+.*\$APPDATA", raw, re.IGNORECASE):
            violations.append(
                f"electron/nsis/installer-custom.nsh:{lineno}: "
                "RMDir /r 指向 $APPDATA——安装器清除用户数据面被禁（用户裁定 "
                "2026-11-08：安装/更新不得清理用户数据）"
            )
    # 正面断言：播种保留守卫必须在位（防轮换以「每次强制重播种」回潮）
    if PRESERVE_READ_ANCHOR not in text:
        violations.append(
            "electron/nsis/installer-custom.nsh: 播种保留守卫缺失——"
            f"{PRESERVE_READ_ANCHOR}（ReadRegStr 先读后种）不在脚本中，"
            "「已存在则原样保留（升级不换口令）」契约失守"
        )
    if PRESERVE_GUARD_ANCHOR not in text:
        violations.append(
            "electron/nsis/installer-custom.nsh: 播种空值守卫缺失——"
            f"{PRESERVE_GUARD_ANCHOR} 不在脚本中，播种可能退化为无条件重写"
            "（等效升级轮换口令）"
        )
    return violations


def check_package_json(data: dict) -> list[str]:
    """electron-builder 配置的用户数据保留 + 免管理员安装检查。"""
    violations: list[str] = []
    nsis = (data.get("build") or {}).get("nsis") or {}
    if nsis.get("deleteAppDataOnUninstall"):
        violations.append(
            "package.json build.nsis.deleteAppDataOnUninstall=true——卸载即清"
            "应用数据目录（认证身份/会话镜像/窗口状态全灭），违反保留契约"
        )
    if nsis.get("perMachine"):
        violations.append(
            "package.json build.nsis.perMachine=true——安装器以管理员启动"
            "（RequestExecutionLevel admin + Program Files），双击/静默安装都要"
            "提权，违反「安装应用不得使用管理员账户」"
            "（ADR 2026-11-12-installer-per-user-no-admin）"
        )
    return violations


def main() -> int:
    violations: list[str] = []
    if not INSTALLER_NSH.is_file():
        print(f"[installer-userdata-guard] 触面缺失（fail-closed）: {INSTALLER_NSH}")
        return 2
    if not PACKAGE_JSON.is_file():
        print(f"[installer-userdata-guard] 触面缺失（fail-closed）: {PACKAGE_JSON}")
        return 2
    violations.extend(check_installer_nsh(INSTALLER_NSH.read_text(encoding="utf-8")))
    violations.extend(check_package_json(json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))))
    if violations:
        print("[installer-userdata-guard] 安装器契约违规（用户数据保留 / 免管理员安装）：")
        for v in violations:
            print(f"  - {v}")
        print(
            "结论：安装器破坏面命中——安装/更新不得清理用户数据与凭据事实源"
            "（ADR 2026-11-08-installer-credential-retention），且安装不得使用"
            "管理员账户（ADR 2026-11-12-installer-per-user-no-admin）。修复动作："
            "移除删除/改写面、恢复播种保留守卫、perMachine 归 false。"
        )
        return 1
    print("[installer-userdata-guard] PASS（口令播种保留 + 用户数据面零清除 + per-user 免管理员）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
