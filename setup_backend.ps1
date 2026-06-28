# Windows Docker 后端选择与稳定性配置
#
# 背景:WSL2 后端有 ext4.vhdx 单文件锁、9p 文件代理崩溃等已知脆弱点,
#      频繁创建/删除容器会导致 daemon 假死、容器卡死。专业版/企业版
#      可切换到 Hyper-V 后端消除这两个脆弱点。
#
# 职责(部署期预防,与运行期恢复 restart_docker.ps1 正交):
#   1. 探测 Windows 版本能否用 Hyper-V
#   2. 专业版/企业版 → 启用 Hyper-V + 配置 Docker Desktop 用 Hyper-V 后端
#   3. 家庭版 → 只能用 WSL2,明确告知稳定性风险 + 优化建议
#
# 用法: powershell -File setup_backend.ps1 [-Force]
# 退出码:
#   0 = 后端已就绪(已是 Hyper-V,或家庭版用 WSL2 已配置优化)
#   1 = 需要重启才能启用 Hyper-V(install_cn.bat 据此提示用户)
#   2 = 用户取消 / 不可恢复错误

#Requires -Version 5.1

param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$msg) { Write-Host "[setup_backend] $msg" }
function Write-Warn2([string]$msg) { Write-Host "[setup_backend] [WARN] $msg" }
function Write-Err2([string]$msg) { Write-Host "[setup_backend] [ERROR] $msg" }

# 是否需要管理员权限
function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p = New-Object Security.Principal.WindowsPrincipal($id)
    return $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

# 探测 Windows SKU 能否用 Hyper-V
# 专业版=48, 企业版=49, 企业版LTSC=125, 教育版=121, 专业工作站=122
function Get-HyperVEligibility {
    $os = Get-CimInstance Win32_OperatingSystem
    $proSkus = @(48, 49, 121, 122, 125)
    if ($os.OperatingSystemSKU -in $proSkus) {
        return @{ Eligible = $true; Edition = $os.Caption }
    }
    return @{ Eligible = $false; Edition = $os.Caption }
}

# 探测 Hyper-V 功能是否已启用(不抛异常,需管理员)
function Test-HyperVEnabled {
    try {
        $f = Get-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -ErrorAction Stop
        return $f.State -eq 'Enabled'
    } catch {
        # 非管理员或功能不存在
        return $false
    }
}

# 读取 Docker Desktop 当前后端(True=WSL2, False=Hyper-V)
# settings-store.json 在新版,settings.json 在旧版
function Get-DockerBackend {
    $paths = @(
        "$env:APPDATA\Docker\settings-store.json",
        "$env:APPDATA\Docker\settings.json"
    )
    foreach ($p in $paths) {
        if (Test-Path $p) {
            try {
                $cfg = Get-Content $p -Raw -ErrorAction Stop | ConvertFrom-Json
                # wslEngineEnabled=true 表示用 WSL2 后端
                if ($cfg.PSObject.Properties.Name -contains 'wslEngineEnabled') {
                    return @{
                        IsWSL2 = [bool]$cfg.wslEngineEnabled
                        File = $p
                    }
                }
            } catch {
                Write-Warn2 "读取 Docker 配置失败,跳过后端检测: $p"
            }
        }
    }
    return $null  # Docker 未装或配置不存在
}

# 设置 Docker Desktop 后端(写配置文件,下次启动 Docker Desktop 生效)
function Set-DockerBackendHyperV {
    param([string]$SettingsFile)
    try {
        $cfg = Get-Content $SettingsFile -Raw -ErrorAction Stop | ConvertFrom-Json
        $cfg.wslEngineEnabled = $false
        $cfg | ConvertTo-Json -Depth 20 | Set-Content $SettingsFile -Encoding UTF8
        Write-Step "已配置 Docker Desktop 使用 Hyper-V 后端: $SettingsFile"
        return $true
    } catch {
        Write-Warn2 "写入 Docker 后端配置失败: $($_.Exception.Message)"
        return $false
    }
}

# ===========================================================================
# 主流程
# ===========================================================================

# --- 0. 非 Windows 直接退出(供跨平台脚本统一调用) ---
if ($PSVersionTable.Platform -and $PSVersionTable.Platform -ne 'Win32NT') {
    Write-Step "非 Windows 系统,跳过后端选择(原生 docker 无后端之争)。"
    exit 0
}

# --- 1. 检查 Docker 是否已安装(未装则由 install_cn.bat 的 winget 环节处理) ---
$dockerExe = Get-Command docker -ErrorAction SilentlyContinue
$ddPath = 'C:\Program Files\Docker\Docker\Docker Desktop.exe'
$dockerInstalled = $dockerExe -or (Test-Path $ddPath)

if (-not $dockerInstalled) {
    Write-Step "Docker 未安装,后端选择将在 Docker 安装后由本脚本再次运行。"
    exit 0
}

# --- 2. 探测 Hyper-V 资格 ---
$elig = Get-HyperVEligibility
Write-Step "当前系统: $($elig.Edition)"

if (-not $elig.Eligible) {
    # 家庭版:只能 WSL2,给优化建议
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host "  注意:此 Windows 版本只能使用 WSL2 后端" -ForegroundColor Yellow
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host "WSL2 后端存在已知稳定性问题(频繁创建/删除容器时可能卡死)。" -ForegroundColor Yellow
    Write-Host "优化建议:" -ForegroundColor Yellow
    Write-Host "  1. Windows Defender 排除 vhdx 文件扫描(头号隐形杀手)"
    Write-Host "     路径: %LOCALAPPDATA%\Packages\*ext4.vhdx"
    Write-Host "  2. Docker Desktop 内存设为 8G+(Settings → Resources)"
    Write-Host "  3. 长期方案:升级到 Windows 专业版以启用 Hyper-V 后端"
    Write-Host "========================================" -ForegroundColor Yellow
    Write-Host ""
    Write-Step "家庭版 WSL2 配置检查完成。"
    exit 0
}

# --- 3. 专业版+:优先 Hyper-V ---
Write-Step "此版本支持 Hyper-V 后端,检查是否已启用..."

if (-not $(Test-Admin)) {
    Write-Warn2 "启用/切换 Hyper-V 后端需要管理员权限。"
    Write-Warn2 "请以管理员身份运行 install_cn.bat,或手动在 Docker Desktop"
    Write-Warn2 "Settings → General → 取消勾 'Use the WSL 2 based engine'。"
    # 不阻断:Hyper-V 未启用时退回 WSL2 仍可运行,只是有卡死风险
    exit 0
}

$hvEnabled = Test-HyperVEnabled
if (-not $hvEnabled) {
    Write-Step "启用 Hyper-V 功能(需要重启生效)..."
    try {
        # -NoRestart: 不立即重启,交由 install_cn.bat 提示用户
        Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V-All -NoRestart -ErrorAction Stop | Out-Null
        Write-Step "Hyper-V 功能已启用(待重启)。"
    } catch {
        Write-Err2 "启用 Hyper-V 失败: $($_.Exception.Message)"
        Write-Warn2 "将退回 WSL2 后端继续部署(有卡死风险)。"
        exit 0
    }
    # Hyper-V 刚启用需要重启才能用,标记需要重启
    $script:NeedReboot = $true
} else {
    Write-Step "Hyper-V 功能已启用。"
    $script:NeedReboot = $false
}

# --- 4. 切换 Docker Desktop 后端为 Hyper-V ---
$current = Get-DockerBackend
if ($current) {
    if (-not $current.IsWSL2) {
        Write-Step "Docker Desktop 已配置为 Hyper-V 后端,无需切换。"
        # 即使是 Hyper-V,刚启用功能也需要重启
        if ($script:NeedReboot) {
            Write-Host "[setup_backend] 需要重启使 Hyper-V 生效。" -ForegroundColor Yellow
            exit 1
        }
        exit 0
    }
    # 当前是 WSL2,切换到 Hyper-V
    if ($script:NeedReboot) {
        Write-Step "重启前先写入 Hyper-V 后端配置,重启后自动生效。"
    } else {
        Write-Step "切换 Docker Desktop 后端: WSL2 -> Hyper-V..."
    }
    Set-DockerBackendHyperV -SettingsFile $current.File | Out-Null
} else {
    Write-Warn2 "未找到 Docker Desktop 配置文件,可能尚未首次启动。"
    Write-Warn2 "请启动一次 Docker Desktop 完成初始化后重新运行本脚本。"
}

# --- 5. 若启用了 Hyper-V 功能,需要重启 ---
if ($script:NeedReboot) {
    Write-Host ""
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "  需要重启 Windows 使 Hyper-V 后端生效" -ForegroundColor Cyan
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host "Hyper-V 后端配置已写入,重启后 Docker Desktop 将自动使用 Hyper-V。"
    Write-Host "重启后请重新运行 install_cn.bat 完成部署。"
    Write-Host "========================================" -ForegroundColor Cyan
    exit 1  # 退出码 1 = 需要重启
}

Write-Step "后端配置完成(Hyper-V)。"
exit 0
