$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "============================================"
Write-Host "  Project Code Statistics Report"
Write-Host "============================================"
Write-Host ""

# Python Statistics
Write-Host "========== Python Backend =========="
$pyFiles = Get-ChildItem -Path "d:\Jianguoyun\Agent os\src","d:\Jianguoyun\Agent os\tests" -Filter "*.py" -Recurse
$totalPyFiles = $pyFiles.Count
$totalPyLines = 0
$totalPyFuncs = 0
$totalPyClasses = 0

foreach ($f in $pyFiles) {
    $content = Get-Content $f.FullName -ErrorAction SilentlyContinue
    $totalPyLines += $content.Count
    $totalPyFuncs += ($content | Where-Object { $_ -match "^\s*def\s+\w+" }).Count
    $totalPyClasses += ($content | Where-Object { $_ -match "^\s*class\s+\w+" }).Count
}

Write-Host "  Files:    $totalPyFiles"
Write-Host "  Lines:    $totalPyLines"
Write-Host "  Functions:$totalPyFuncs"
Write-Host "  Classes:  $totalPyClasses"
Write-Host ""

# Python by module
Write-Host "---------- Python by Module ----------"
$modules = @{}
foreach ($f in $pyFiles) {
    $parts = $f.FullName.Split("\")
    $srcIdx = [Array]::IndexOf($parts, "src")
    $testsIdx = [Array]::IndexOf($parts, "tests")
    if ($srcIdx -ge 0) {
        $mod = $parts[$srcIdx + 1]
    } elseif ($testsIdx -ge 0) {
        $mod = "tests"
    } else {
        $mod = "root"
    }
    if (-not $modules.ContainsKey($mod)) {
        $modules[$mod] = @{Files=0; Lines=0; Funcs=0; Classes=0}
    }
    $content = Get-Content $f.FullName -ErrorAction SilentlyContinue
    $modules[$mod].Files += 1
    $modules[$mod].Lines += $content.Count
    $modules[$mod].Funcs += ($content | Where-Object { $_ -match "^\s*def\s+\w+" }).Count
    $modules[$mod].Classes += ($content | Where-Object { $_ -match "^\s*class\s+\w+" }).Count
}

Write-Host ("{0,-25} {1,5} {2,7} {3,6} {4,5}" -f "Module", "Files", "Lines", "Funcs", "Class")
Write-Host ("-" * 55)
$sorted = $modules.GetEnumerator() | Sort-Object { $_.Value.Lines } -Descending
foreach ($entry in $sorted) {
    $k = $entry.Key
    $v = $entry.Value
    Write-Host ("{0,-25} {1,5} {2,7} {3,6} {4,5}" -f $k, $v.Files, $v.Lines, $v.Funcs, $v.Classes)
}
Write-Host ""

# TypeScript Statistics
Write-Host "========== TypeScript Frontend =========="
$tsFiles = Get-ChildItem -Path "d:\Jianguoyun\Agent os\frontend\src","d:\Jianguoyun\Agent os\frontend\e2e" -Filter "*.ts" -Recurse
$tsxFiles = Get-ChildItem -Path "d:\Jianguoyun\Agent os\frontend\src" -Filter "*.tsx" -Recurse
$allTsFiles = $tsFiles + $tsxFiles
$totalTsFiles = $allTsFiles.Count
$totalTsLines = 0
$totalTsFuncs = 0

foreach ($f in $allTsFiles) {
    $content = Get-Content $f.FullName -ErrorAction SilentlyContinue
    $totalTsLines += $content.Count
    $totalTsFuncs += ($content | Where-Object { $_ -match "function\s+\w+" }).Count
}

Write-Host "  Files:    $totalTsFiles (.ts: $($tsFiles.Count), .tsx: $($tsxFiles.Count))"
Write-Host "  Lines:    $totalTsLines"
Write-Host "  Functions:$totalTsFuncs (declared)"
Write-Host ""

# Big functions (>50 lines) in Python
Write-Host "========== Python Big Functions (>50 lines) =========="
$bigFuncs = @()
foreach ($f in $pyFiles) {
    $lines = Get-Content $f.FullName -ErrorAction SilentlyContinue
    if ($lines -eq $null) { continue }
    
    $funcName = ""
    $funcStart = 0
    $lineNum = 0
    
    foreach ($line in $lines) {
        $lineNum++
        if ($line -match "^(\s*)def\s+(\w+)") {
            if ($funcName -ne "" -and ($lineNum - 1 - $funcStart) -gt 50) {
                $relPath = $f.FullName.Replace("d:\Jianguoyun\Agent os\", "")
                $obj = New-Object PSObject -Property @{
                    FName = $funcName
                    FLines = ($lineNum - 1 - $funcStart)
                    FFile = $relPath
                    FStart = $funcStart
                }
                $bigFuncs += $obj
            }
            $funcName = $Matches[2]
            $funcStart = $lineNum
        }
    }
    if ($funcName -ne "" -and ($lineNum - $funcStart) -gt 50) {
        $relPath = $f.FullName.Replace("d:\Jianguoyun\Agent os\", "")
        $obj = New-Object PSObject -Property @{
            FName = $funcName
            FLines = ($lineNum - $funcStart)
            FFile = $relPath
            FStart = $funcStart
        }
        $bigFuncs += $obj
    }
}

$bigFuncs = $bigFuncs | Sort-Object FLines -Descending
Write-Host "  Found $($bigFuncs.Count) functions with >50 lines:"
Write-Host ""
Write-Host ("{0,-35} {1,6} {2}" -f "Function", "Lines", "Location")
Write-Host ("-" * 85)
foreach ($bf in $bigFuncs) {
    Write-Host ("{0,-35} {1,6} {2}:L{3}" -f $bf.FName, $bf.FLines, $bf.FFile, $bf.FStart)
}
Write-Host ""

# Summary
Write-Host "============================================"
Write-Host "  SUMMARY"
Write-Host "============================================"
$totalFiles = $totalPyFiles + $totalTsFiles
$totalLines = $totalPyLines + $totalTsLines
$totalFunctions = $totalPyFuncs + $totalTsFuncs
Write-Host "  Total Files:     $totalFiles (Python: $totalPyFiles, TS: $totalTsFiles)"
Write-Host "  Total Lines:     $totalLines (Python: $totalPyLines, TS: $totalTsLines)"
Write-Host "  Total Functions: $totalFunctions (Python: $totalPyFuncs, TS: $totalTsFuncs)"
Write-Host "  Python Classes:  $totalPyClasses"
Write-Host "============================================"
