$ErrorActionPreference = "SilentlyContinue"
$count = 0
$failed = 0

# Process all container directories and other directories under myproject
$dirs = @(
    "d:\myproject\container_08f57__wt_5fe0a81d",
    "d:\myproject\container_08f57__wt_f882c52c",
    "d:\myproject\f47156192960",
    "d:\myproject\container_036fa__wt_ad277471",
    "d:\myproject\container_036fa__wt_1c93d4fa",
    "d:\myproject\container_ed83f__wt_f54dca6f",
    "d:\myproject\container_08f57__wt_dc254945",
    "d:\myproject\container_9b4fe__wt_ae26b7f5",
    "d:\myproject\container_08f57__wt_24b0316b",
    "d:\myproject\container_e3bff__wt_6bcff720",
    "d:\myproject\container_15b50__wt_b2cad5a5"
)

foreach ($dir in $dirs) {
    if (Test-Path $dir) {
        Push-Location $dir
        Get-ChildItem -Path . -Recurse -Filter "nul" -Force -ErrorAction SilentlyContinue | ForEach-Object {
            $fullPath = $_.FullName
            $uncPath = "\\?\$fullPath"
            try {
                [System.IO.File]::Delete($uncPath)
                $count++
            } catch {
                $failed++
            }
        }
        Pop-Location
    }
}

Write-Host "Deleted: $count, Failed: $failed"
