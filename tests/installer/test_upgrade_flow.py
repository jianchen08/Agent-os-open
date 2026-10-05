"""NSIS 装机版升级流程回归门禁（ADR 2026-10-05-nsis-upgrade-flow-and-defender-exclusion）。

可观察契约（静态分析层面）：

1. `electron/nsis/installer-custom.nsh` customCheckAppRunning 末尾必须含升级
   MessageBox 反馈——用户双击 Setup.exe 后 ≤ 1s 内可见提示；
2. customInstall 末尾必须含 perMachine Defender 排除路径写入
   （`Add-MpPreference -ExclusionPath $INSTDIR,$APPDATA\agentos`）；
3. customUnInstall 不能主动删 `$APPDATA\agentos`（--delete-app-data 已由
   模板 uninstaller.nsh 的 $isDeleteAppData 守卫，本宏不重复）；
4. `package.json` `build.nsis.artifactName` 必须含 `${productName}-Setup`
   前缀（修 release/latest.yml 路径错配）；
5. `package.json` `build.extraResources` 不应再有 `frontend/dist`（与 `files`
   重复 19 MB）；
6. `package.json` `scripts.electron:build` 链尾必须包含两个 gate
   （`check_packaged_layout.py` + `packtest_smoke.py`）。

本文件不进 build gate——pytest 本机单元跑即可。`scripts/run_gates.py --mode
all` 必跑本模块。

不在 runner 模块内做实际 NSIS 编译/包装版 installer run：那需要 NSIS +
msi 旧版 + 用户空间隔离 fixture，超出单元测试范畴；本模块验证契约**字符串
层**已生效，机器判据+语义对位的全链路冒烟走 packtest_smoke.py 与
post_install_smoke.py（ADR §6.2/§6.3）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
NSIS_CUSTOM = REPO / "electron" / "nsis" / "installer-custom.nsh"
PACKAGE_JSON = REPO / "package.json"


@pytest.fixture(scope="module")
def nsis_text() -> str:
    return NSIS_CUSTOM.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def package_json() -> dict:
    return json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))


class TestUpgradeFlowNsiCustom:
    """NSIS 自定义宏契约——升级流程回归门禁。"""

    def test_custom_check_app_running_has_upgrade_messagebox(self, nsis_text: str) -> None:
        """customCheckAppRunning 末尾必须含升级 MessageBox。

        判据（持续约束）：检测到既有装机位时弹 MB_ICONINFORMATION|MB_TOPMOST
        的"灵汐助手 即将开始升级..."提示——防止后续 NSIS 模板升级把钩子挪走
        或改 silent 默认误吞。
        """
        m = re.search(
            r"!macro\s+customCheckAppRunning\b.*?!macroend",
            nsis_text,
            re.DOTALL,
        )
        assert m, "customCheckAppRunning 宏未找到"
        body = m.group(0)
        assert "${isUpdated}" not in body or "MessageBox" in body, (
            "customCheckAppRunning 含 ${isUpdated} 守卫但缺 MessageBox——"
            "升级路径将依然零反馈"
        )
        assert "MessageBox" in body and "MB_TOPMOST" in body, (
            "customCheckAppRunning 缺 MB_TOPMOST MessageBox 反馈（被卡了 4096 "
            "子窗口之后才显示属 UX 退化）"
        )
        assert "即将开始升级" in body, (
            "customCheckAppRunning 缺'即将开始升级'反馈文本——文案漂移"
        )

    def test_custom_install_has_defender_exclusion(self, nsis_text: str) -> None:
        """customInstall 末尾必须含 perMachine Defender 排除路径写入。

        判据：perMachine 安装（`!ifdef INSTALL_MODE_PER_ALL_USERS`）下
        `nsExec::ExecToLog powershell.exe Add-MpPreference -ExclusionPath
        $INSTDIR,$APPDATA\\agentos`——默认轻失败不阻断，命中两条关键排除
        路径（安装根 + 用户数据根）。
        """
        m = re.search(
            r"!macro\s+customInstall\b.*?!macroend",
            nsis_text,
            re.DOTALL,
        )
        assert m, "customInstall 宏未找到"
        body = m.group(0)
        assert "Add-MpPreference" in body, (
            "customInstall 缺 Defender 排除路径写入——Defender 实时扫描将再次"
            "成为装机删盘的瓶颈（实测 MsMpEng 163% CPU / 67 线程）"
        )
        assert "ExclusionPath" in body and "-Force" in body, (
            "Add-MpPreference 调用缺 -ExclusionPath 或 -Force（幂等性兜底）"
        )
        assert "INSTALL_MODE_PER_ALL_USERS" in body, (
            "Defender 排除写入缺 INSTALL_MODE_PER_ALL_USERS 守卫——perUser "
            "装机将因 HKLM 写权限被拒而走错路径"
        )
        assert "nsExec::ExecToLog" in body, (
            "Defender 排除走 ExecToLog 而非 Exec——失败静默不阻断安装（与 "
            "AGENTOS_ADMIN_PASSWORD 播种同一失败语义）"
        )

    def test_custom_uninstall_does_not_touch_user_root(self, nsis_text: str) -> None:
        """customUnInstall 不应主动删 $APPDATA\agentos。

        判据：customUnInstall 仅清 HKCU 环境变量；RMDir /r $APPDATA\agentos
        走模板 uninstaller.nsh 的 $isDeleteAppData 守卫，本宏不重复——双重
        删除路径是 %APPDATA% 触及退化的温床（与 ADR 2026-10-03-packaged-
        user-data-first 的'用户数据为准'契约不严）。
        """
        m = re.search(
            r"!macro\s+customUnInstall\b.*?!macroend",
            nsis_text,
            re.DOTALL,
        )
        assert m, "customUnInstall 宏未找到"
        body = m.group(0)
        assert "RMDir" not in body or "$APPDATA" not in body, (
            "customUnInstall 主动 RMDir $APPDATA\\agentos——与模板守卫冲突；"
            "若需清用户数据请走 --delete-app-data 显式路径"
        )
        assert "AGENTOS_ADMIN_PASSWORD" in body, (
            "customUnInstall 缺 AGENTOS_ADMIN_PASSWORD 环境变量清理——重装"
            "不会重新播种密码学随机口令"
        )

    def test_nsis_custom_check_app_running_uses_tasklist_branch(self, nsis_text: str) -> None:
        """customCheckAppRunning 必须强制 tasklist 分支（IsPowerShellAvailable=1）。

        模板语义：`$IsPowerShellAvailable` `0 = PowerShell 可用, 1 = 不可用`，
        后者走 tasklist 分支（cm /C 包装、延迟有界），是 BUG-12 修复点。
        若误写成 0，会再次命中 WMI Get-CimInstance 已知挂死路径（实测
        3.3s→40-60s 抖动）。
        """
        m = re.search(
            r"!macro\s+customCheckAppRunning\b.*?!macroend",
            nsis_text,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        match = re.search(r"StrCpy\s+\$IsPowerShellAvailable\s+(\d)", body)
        assert match, "customCheckAppRunning 缺 StrCpy $IsPowerShellAvailable"
        value = match.group(1)
        assert value == "1", (
            f"customCheckAppRunning 设 $IsPowerShellAvailable={value}（应为 1 即"
            f"PowerShell 不可用强制走 tasklist 分支）；改为 0 将命中 WMI 挂死路径"
        )


class TestPackageJsonInstallerContract:
    """package.json 装机版契约——升级流程回归门禁。"""

    def test_nsis_artifact_name_uses_product_template(self, package_json: dict) -> None:
        """build.nsis.artifactName 必须用 productName 模板变量。

        判据：缺失会让 electron-builder 默认拼出 agent-os-setup-${version}.exe
        ——与磁盘上的'灵汐助手 Setup ${version}.exe'不匹配，auto-update 协议
        latest.yml 找不到匹配文件即报 fallback 错。
        """
        nsis_cfg = package_json["build"]["nsis"]
        assert "artifactName" in nsis_cfg, "build.nsis.artifactName 缺失"
        artifact = nsis_cfg["artifactName"]
        assert "${productName}" in artifact, (
            f"build.nsis.artifactName={artifact!r} 未含 ${{productName}}——磁盘与"
            f"latest.yml 路径错配"
        )
        assert "Setup" in artifact, (
            f"build.nsis.artifactName={artifact!r} 未含 'Setup'——产物命名退化"
        )

    def test_extra_resources_dedupes_frontend_dist(self, package_json: dict) -> None:
        """build.extraResources 不应有 frontend/dist（与 build.files 重复 19 MB）。

        `files` 已经把 frontend/dist/**/* 摄入 asar；额外再列 extraResources
        会让 win-unpacked 多出 19 MB 副本——装机递归删盘时多扫一遍。
        """
        extras = package_json["build"]["extraResources"]
        assert isinstance(extras, list)
        dupes = [r for r in extras if r.get("from") == "frontend/dist"]
        assert not dupes, (
            f"build.extraResources 仍含 {dupes}，与 'files' 重复 19 MB；"
            f"已入 asar 不必再入 resources"
        )

    def test_electron_build_chain_includes_smoke_gates(self, package_json: dict) -> None:
        """scripts.electron:build 链尾必须含两个 fail-fast gate。

        判据：缺位会让 packtest_smoke 与 check_packaged_layout 成为"手跑脚本"
        ——实际 CI 链路兜不住产物回归（packtest_smoke 验证 CDP 加载链，
        check_packaged_layout 验证安装位结构）。
        """
        script = package_json["scripts"]["electron:build"]
        assert "check_packaged_layout.py" in script, (
            "scripts.electron:build 缺 check_packaged_layout.py gate——"
            "win-unpacked 结构退化无 fail-fast 兜底"
        )
        assert "packtest_smoke.py" in script, (
            "scripts.electron:build 缺 packtest_smoke.py gate——CDP 加载链"
            "（app:// 协议 + React 挂载 + SPA 深链 + 代理路由）退化无兜底"
        )
        assert script.endswith("packtest_smoke.py --exe release/win-unpacked/灵汐助手.exe --cdp-port 9223 --timeout 420"), (
            "scripts.electron:build 链尾未锚定到具体 packtest_smoke 参数；"
            "timeout=420 与 9223 端口为 KERNEL_HEALTH_TIMEOUT_MS 同源"
        )