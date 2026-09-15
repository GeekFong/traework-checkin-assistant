<#
.SYNOPSIS
    使用 PyInstaller 打包单文件 GUI exe。
.DESCRIPTION
    在仓库根执行，产物输出到 dist\TraeWorkCheckin.exe。
    仅构建期需要 pyinstaller：python -m pip install pyinstaller
#>

[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

Write-Host "==> 使用解释器: $Python" -ForegroundColor Cyan
& $Python --version
if ($LASTEXITCODE -ne 0) { throw "找不到 Python：$Python" }

Write-Host "==> 检查 PyInstaller ..." -ForegroundColor Cyan
& $Python -c "import PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "==> 安装 pyinstaller ..." -ForegroundColor Yellow
    & $Python -m pip install pyinstaller
}

Write-Host "==> 语法编译检查 ..." -ForegroundColor Cyan
& $Python -m compileall -q trae_checkin
if ($LASTEXITCODE -ne 0) { throw "编译检查失败" }

Write-Host "==> 清理旧产物 ..." -ForegroundColor Cyan
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue

Write-Host "==> PyInstaller 打包 ..." -ForegroundColor Cyan
& $Python -m PyInstaller --noconfirm --clean "TraeCheckin.spec"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败" }

$exe = Join-Path $repoRoot "dist\TraeWorkCheckin.exe"
if (Test-Path $exe) {
    $size = [math]::Round((Get-Item $exe).Length / 1MB, 2)
    Write-Host "==> 打包成功：$exe ($size MB)" -ForegroundColor Green
} else {
    throw "未找到产物 exe"
}
