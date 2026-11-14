<#
.SYNOPSIS
    构建 Windows x64 离线安装包（可重复执行）。

.DESCRIPTION
    一条命令从源码到可交付的 AIMS-<版本>-x64-Setup.exe：

        依赖检查 → 建立构建环境 → 获取内置 PostgreSQL/pgvector →
        打包启动器与后端 → 冒烟测试 → 编译安装程序 → 输出构建报告

    构建机需要：Windows 10/11 x64、Python 3.10-3.12、Inno Setup 6、联网。
    目标机需要：什么都没装过的干净 Windows —— 这正是打包的目的。

.EXAMPLE
    .\build_x64.ps1
    .\build_x64.ps1 -SkipVendor -PgVersion 16.4-1 -UpdateLock
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$PgVersion = '16.4-1',
    [switch]$SkipVendor,        # 复用已有 vendor\pgsql
    [switch]$SkipAi,            # 不装 llama-cpp-python（AI 降级为规则引擎）
    [switch]$UpdateLock,        # 首次构建：记录下载文件 SHA-256
    [switch]$AllowPgVectorBuild # 允许从源码编译 pgvector（需要 VS C++ 工具链）
)

. (Join-Path $PSScriptRoot 'build_common.ps1')

$started = Get-Date
$version = Get-AimsVersion
Write-Step "AIMS $version / x64 离线安装包构建" 'Green'
Write-Host "仓库：$script:RepoRoot"

# --------------------------------------------------------------------------- #
Write-Step "1/8 依赖检查"
& (Join-Path $PSScriptRoot 'dependency_check.ps1') -ForBuild -Arch x64
if ($LASTEXITCODE -ne 0) { throw "依赖检查未通过，构建中止。" }

Write-Step "2/8 准备构建环境"
$Python = Resolve-AimsPython -Preferred $Python
Write-Host "Python：$Python"
& $Python -c "import sys;print(sys.version)"
$req = Join-Path $script:RepoRoot 'requirements-exe.txt'
Invoke-Checked -Exe $Python -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', 'wheel', 'setuptools') -What 'pip 升级'
Invoke-Checked -Exe $Python -Arguments @('-m', 'pip', 'install', '-r', $req) -What '安装后端依赖'
Invoke-Checked -Exe $Python -Arguments @('-m', 'pip', 'install', 'pyinstaller>=6.6') -What '安装打包工具'
if (-not $SkipAi) {
    # llama-cpp-python 只在有匹配轮子时安装；失败不阻断（AI 会自动降级并在自检里报告）
    & $Python -m pip install --only-binary=:all: --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu 'llama-cpp-python==0.3.2'
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ! llama-cpp-python 未安装：本次构建的 AI 问答将使用规则引擎（功能降级，会在自检中如实报告）" -ForegroundColor Yellow
    }
}

# --------------------------------------------------------------------------- #
Write-Step "3/8 内置 PostgreSQL 与扩展"
$pgHome = Join-Path $script:VendorDir 'pgsql'
if (-not $SkipVendor) {
    $pgHome = Get-VendorPostgres -Version $PgVersion -Arch x64 -UpdateLock:$UpdateLock
} else {
    Write-Host "  跳过下载，复用 $pgHome"
}
if (-not (Test-Path -LiteralPath (Join-Path $pgHome 'bin\pg_ctl.exe'))) {
    throw "vendor\pgsql 不完整（缺少 bin\pg_ctl.exe）。请去掉 -SkipVendor 重新构建。"
}
$hasVector = Install-PgVector -PgHome $pgHome -AllowBuild:$AllowPgVectorBuild
$env:AIMS_BUILD_PGVECTOR = if ($hasVector) { '1' } else { '0' }
Write-Host ("  pgvector：" + $(if ($hasVector) { '已内置' } else { '未内置（EMBEDDING_STORAGE=json 回退，功能不缺失）' }))

# --------------------------------------------------------------------------- #
Write-Step "4/8 前端与规则包"
$frontend = Join-Path $script:RepoRoot 'frontend.html'
if (-not (Test-Path -LiteralPath $frontend)) { throw "缺少 frontend.html" }
$vendorJs = Join-Path $script:RepoRoot 'cores\vendor'
foreach ($f in @('xlsx.full.min.js', 'chart.umd.min.js', 'chartjs-adapter-date-fns.bundle.min.js')) {
    if (-not (Test-Path -LiteralPath (Join-Path $vendorJs $f))) {
        throw "缺少本地前端库 cores\vendor\$f（离线安装包不允许走 CDN）"
    }
}
if (Select-String -LiteralPath $frontend -Pattern 'src="https?://' -Quiet) {
    Write-Host "  ! frontend.html 里有外链 script src，请确认已内联（否则离线环境下会白屏）" -ForegroundColor Yellow
}

# --------------------------------------------------------------------------- #
Write-Step "5/8 打包启动器与后端（PyInstaller）"
if (Test-Path -LiteralPath $script:DistDir) { Remove-Item -LiteralPath $script:DistDir -Recurse -Force }
New-Item -ItemType Directory -Path $script:DistDir -Force | Out-Null
Build-Launcher -Python $Python -OutDir (Join-Path $script:DistDir 'launcher') -Version $version
Build-Backend -Python $Python -OutDir (Join-Path $script:DistDir 'backend')
$backendExe = Join-Path $script:DistDir 'backend\AIMS-Backend\AIMS-Backend.exe'
if (-not (Test-Path -LiteralPath $backendExe)) { throw "后端产物缺失：$backendExe" }

# 代码签名（P0）：主程序先签，再让 ISCC 把它们打进安装包；安装包本身在第 7 步之后签。
# 未配置 AIMS_SIGN_CERT 时只告警不中断（内部测试构建）；正式发版请设 AIMS_SIGN_REQUIRED=1，
# 缺配置即失败，避免"忘记签名就发出去了"。
$launcherBuiltExe = Join-Path $script:DistDir 'launcher\AIMS.exe'
Sign-AimsFile -Path $backendExe | Out-Null
if (Test-Path -LiteralPath $launcherBuiltExe) { Sign-AimsFile -Path $launcherBuiltExe | Out-Null }

# --------------------------------------------------------------------------- #
Write-Step "6/8 冒烟测试（构建产物必须能跑起来）"
Invoke-Checked -Exe $backendExe -Arguments @('deploy', 'version') -What '后端 EXE 自检'
$launcherExe = Join-Path $script:DistDir 'launcher\AIMS.exe'
if (-not (Test-Path -LiteralPath $launcherExe)) { throw "启动器产物缺失：$launcherExe" }
Invoke-Checked -Exe $launcherExe -Arguments @('--version') -What '启动器 EXE 自检'
& $Python -m pytest -q (Join-Path $script:RepoRoot 'tests') --maxfail=1
if ($LASTEXITCODE -ne 0) { Write-Host "  ! 单元测试未全部通过，请先修好再发版" -ForegroundColor Yellow }

# --------------------------------------------------------------------------- #
Write-Step "7/8 编译安装程序（Inno Setup）"
$setup = Build-Installer -Version $version -Arch x64
# 签安装包（此时包内 EXE 已签名）。签名后才会计算校验和，顺序不能反。
Sign-AimsFile -Path $setup | Out-Null
Write-Host "  产物：$setup" -ForegroundColor Green

# --------------------------------------------------------------------------- #
Write-Step "8/8 构建报告"
$report = [ordered]@{
    version            = $version
    arch               = 'x64'
    built_at           = (Get-Date).ToString('s')
    duration_minutes   = [math]::Round(((Get-Date) - $started).TotalMinutes, 1)
    python             = (& $Python -c "import sys;print(sys.version.split()[0])")
    installer          = $setup
    installer_size_mb  = [math]::Round((Get-Item -LiteralPath $setup).Length / 1MB, 1)
    postgres_version   = $PgVersion
    pgvector           = $hasVector
    launcher_size_mb   = Get-PackageSizeMB -Path (Join-Path $script:DistDir 'launcher')
    backend_size_mb    = Get-PackageSizeMB -Path (Join-Path $script:DistDir 'backend')
    llama_cpp          = -not $SkipAi
    sha256             = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLower()
    code_signing       = Get-SignatureInfo -Path $setup
    backend_signature  = Get-SignatureInfo -Path $backendExe
}
New-Item -ItemType Directory -Path $script:BuildDir -Force | Out-Null
$reportPath = Join-Path $script:BuildDir "build-report-x64.json"
($report | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Host ($report | ConvertTo-Json -Depth 4)
Write-Host ""
Write-Host "构建完成：$setup" -ForegroundColor Green
Write-Host "构建报告：$reportPath"
Write-Host ""
Write-Host "下一步（必须在干净 Windows 上做，不能省）：" -ForegroundColor Cyan
Write-Host "  1. 拷贝到一台未装 Python/Node/Docker/PostgreSQL 的 Windows 11，断网"
Write-Host "  2. 双击 $([System.IO.Path]::GetFileName($setup)) 安装"
Write-Host "  3. 用 tools\Windows-Acceptance.ps1 跑自动化验收，产出测试报告"
exit 0
