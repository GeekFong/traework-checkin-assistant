<#
.SYNOPSIS
    调用 Inno Setup 编译器生成 Windows 安装包。
.DESCRIPTION
    前置条件：
      1. 已执行 scripts\build_exe.ps1，dist 下存在单文件 exe；
      2. 已安装 Inno Setup 6+（默认路径自动探测，也可用 -ISCC 指定）。
    产物：installer\Output\TraeWorkCheckin-Setup-v1.1.0.exe
#>

[CmdletBinding()]
param(
    [string]$ISCC = ""
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$distExe = Join-Path $repoRoot "dist\TraeWorkCheckin.exe"
$iss = Join-Path $repoRoot "installer\installer.iss"

if (-not (Test-Path $distExe)) {
    throw "未找到 $distExe，请先运行 scripts\build_exe.ps1"
}

if (-not $ISCC) {
    $candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { $ISCC = $c; break }
    }
}

if (-not $ISCC -or -not (Test-Path $ISCC)) {
    throw "未找到 ISCC.exe，请安装 Inno Setup 6+ 或用 -ISCC 指定编译器路径"
}

Write-Host "==> 使用编译器: $ISCC" -ForegroundColor Cyan
& $ISCC $iss
if ($LASTEXITCODE -ne 0) { throw "Inno Setup 编译失败" }

$out = Join-Path $repoRoot "installer\Output"
Write-Host "==> 安装包已生成到：$out" -ForegroundColor Green
Get-ChildItem $out -Filter *.exe | ForEach-Object {
    $size = [math]::Round($_.Length / 1MB, 2)
    Write-Host ("    {0} ({1} MB)" -f $_.Name, $size)
}
