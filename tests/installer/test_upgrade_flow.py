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
        """customInstall 末尾必须含 Defender 排除兜底写入（共享宏 + admin 运行期门控）。

        判据：customInstall 调用 _writeDefenderExclusions；该宏以**运行期**
        ${UAC_IsAdmin} 门控（2026-10-05 首版误用编译期 INSTALL_MODE_PER_ALL_USERS
        ——本仓 nsis.perMachine 未设，该宏永不定义，排除写入从未生效=死代码），
        nsExec 结果必须 Pop（NSIS 栈纪律）。
        """
        m = re.search(
            r"!macro\s+customInstall\b.*?!macroend",
            nsis_text,
            re.DOTALL,
        )
        assert m, "customInstall 宏未找到"
        body = m.group(0)
        assert "_writeDefenderExclusions" in body, (
            "customInstall 缺 _writeDefenderExclusions 兜底调用——交互首装经 UI "
            "选 all-users 提权后的路径无排除兜底"
        )

    def test_write_defender_exclusions_macro_contract(self, nsis_text: str) -> None:
        """_writeDefenderExclusions 宏：运行期 admin 门控 + Pop 栈纪律 + 三路径。

        判据（逐条，防回归到首版死代码形态）：
        1. 门控用 ${UAC_IsAdmin}（运行期），不用编译期 INSTALL_MODE_PER_ALL_USERS；
        2. nsExec::ExecToLog 后必须 Pop（栈失衡会连坐后续 NSIS 逻辑）；
        3. 排除路径覆盖 $INSTDIR + per-user 默认装位 + 用户数据根；
        4. -Force 幂等；失败 DetailPrint 不阻断。
        """
        m = re.search(
            r"!macro\s+_writeDefenderExclusions\b.*?!macroend",
            nsis_text,
            re.DOTALL,
        )
        assert m, "_writeDefenderExclusions 宏未找到"
        body = m.group(0)
        assert "${UAC_IsAdmin}" in body, (
            "_writeDefenderExclusions 门控必须是运行期 ${UAC_IsAdmin}；"
            "编译期 INSTALL_MODE_PER_ALL_USERS 在 perMachine=false 构建下永不定义"
        )
        assert "INSTALL_MODE_PER_ALL_USERS" not in body, (
            "_writeDefenderExclusions 不得用编译期宏做门控（首版死代码根因）"
        )
        assert "nsExec::ExecToLog" in body and re.search(r"Pop\s+\$", body), (
            "nsExec::ExecToLog 结果必须 Pop——栈失衡连坐后续 NSIS 逻辑"
        )
        assert "Add-MpPreference" in body and "-ExclusionPath" in body and "-Force" in body, (
            "Add-MpPreference 调用缺 -ExclusionPath 或 -Force（幂等性兜底）"
        )
        assert "$INSTDIR" in body and "$APPDATA\\agentos" in body, (
            "排除路径必须覆盖安装根与用户数据根"
        )
        assert "$TEMP\\ns*.tmp" in body, (
            "排除路径必须覆盖 NSIS 暂存区 $TEMP\\ns*.tmp——两段式解包（Nsis7z 解到 "
            "PLUGINSDIR 再 CopyFiles）暂存区在排除区外时，单次拷贝失败即触发模板"
            "重试环（删暂存+整包重解压，/SD IDRETRY 静默自动应答），实测放大成 31 分钟"
        )
        assert "DetailPrint" in body, (
            "失败必须 DetailPrint 留痕不阻断（与 env 播种同一静默语义）"
        )
        # NSIS $ 转义契约：PS 的 $_ 必须写 $$_——裸 $_ 被 NSIS 当变量解析触发
        # warning 6000（"unknown variable/constant _)"，builder 按 error 处理，
        # 2026-10-06 实锤；此前该行藏于编译期死代码内未被解析故未暴露）。
        # 仅查非注释行（`;` 注释行 makensis 不展开，注释里解释该坑本身即含裸 $_）
        code_lines = [l for l in body.splitlines() if not l.strip().startswith(";")]
        code_body = "\n".join(code_lines)
        assert "$$_" in code_body and not re.search(r"(?<!\$)\$_", code_body), (
            "宏内 PS $_ 未按 NSIS 语法转义为 $$_——裸 $_ 触发 makensis 6000"
        )

    def test_custom_init_writes_exclusion_before_file_copy(self, nsis_text: str) -> None:
        """customInit 必须在解包前写排除——copy 免扫的关键一手。

        模板展开序：.onInit（initMultiUser → customInit）→ Section
        （CHECK_APP_RUNNING → uninstallOldVersion → installApplicationFiles →
        customInstall）。排除写在 customInit = 卸载旧版与解包 copy 均免扫；
        写在 customInstall = copy 已被扫完（2026-10-05 实测 Setup 9 分钟
        CPU 3.2s 纯阻塞）。
        """
        m = re.search(
            r"!macro\s+customInit\b.*?!macroend",
            nsis_text,
            re.DOTALL,
        )
        assert m, "customInit 宏未找到"
        body = m.group(0)
        assert "_writeDefenderExclusions" in body, (
            "customInit 缺 _writeDefenderExclusions——排除写入落在 copy 之后，"
            "装机解包仍被 Defender 逐文件扫描（实测挂 9 分钟）"
        )
        # 顺序契约：customInit 宏体在文件中先于 customCheckAppRunning/customInstall 出现
        # （NSIS 宏定义顺序不影响展开，但同文件内先定义先读，防后续重排时把
        # 前置写又挪回尾部而无测试感知）
        assert nsis_text.index("!macro customInit") < nsis_text.index(
            "!macro customInstall"
        ), "customInit 定义应先于 customInstall（时机契约的静态可读性）"

    def test_defender_var_gated_out_of_uninstaller_build(self, nsis_text: str) -> None:
        """DefenderExecResult 声明必须带 BUILD_UNINSTALLER 门控（makensis 6001 防回归）。

        卸载器构建（BUILD_UNINSTALLER）不含 customInit/customInstall，宏不展开 →
        无条件声明的 Var 在卸载器脚本零引用 → warning 6001 → builder 按 error
        处理（2026-10-06 实锤首例：12 分钟构建死于该警告）。同 Var 声明里
        AgentOsAdminPassword 靠 customUnInstall 的 ReadRegStr 惰性引用、pid/
        IsPowerShellAvailable 靠 customCheckAppRunning（卸载器也展开）豁免，
        DefenderExecResult 是唯一仅安装器侧引用的 Var，必须门控。
        """
        assert re.search(
            r"!ifndef BUILD_UNINSTALLER\s*\nVar /GLOBAL DefenderExecResult\s*\n!endif",
            nsis_text,
        ), (
            "DefenderExecResult 声明缺 !ifndef BUILD_UNINSTALLER 门控——卸载器构建将因 "
            "warning 6001 被当 error 而失败"
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
