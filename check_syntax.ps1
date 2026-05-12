# Check Python syntax
$ErrorActionPreference = "Continue"
$errors = @()

Get-ChildItem -Path "src" -Recurse -Filter "*.py" | ForEach-Object {
    $file = $_.FullName
    try {
        $content = Get-Content $file -Raw -Encoding UTF8
        [System.Management.Automation.Language.Parser]::ParseInput($content, [ref]$null, [ref]$null) | Out-Null
        Write-Host "[OK] $file" -ForegroundColor Green
    }
    catch {
        Write-Host "[ERROR] $file : $_" -ForegroundColor Red
        $errors += $file
    }
}

if ($errors.Count -eq 0) {
    Write-Host "`n=== All syntax checks passed ===" -ForegroundColor Green
} else {
    Write-Host "`n=== Found $($errors.Count) files with syntax errors ===" -ForegroundColor Red
    $errors | ForEach-Object { Write-Host $_ }
}
