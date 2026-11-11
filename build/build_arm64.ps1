<#
.SYNOPSIS
    构建 Windows ARM64 安装包。

.DESCRIPTION
    先说清楚事实，不要含糊（需求书第八阶段第 8 条：不允许未经验证就宣称兼容 ARM64）：

    · **PostgreSQL 官方没有 Windows ARM64 原生发行版**（截至 PostgreSQL 16/17，
      官方 Windows 二进制只提供 x64）。pgvector 同理，没有官方 ARM64 预编译包。
    · 但 **Windows 11 ARM64 可以运行 x64 程序**（Prism 仿真），包括 x64 的
      PostgreSQL 服务。所以 ARM64 机器上"能装、能跑"是成立的，只是数据库进程
      走的是仿真层，性能比原生低约 10-25%（对 AIMS 这种单机、低并发场景影响
      很小，瓶颈本来就在 AI 推理和磁盘）。

    因此本脚本有两种模式：

      -Native      只有在 vendor\pgsql-arm64 存在（客户/集成商自行编译的
                   ARM64 PostgreSQL）时才允许，产物名 AIMS-<ver>-arm64-Setup.exe
      -Emulation   默认模式。使用 x64 的 PostgreSQL，产物名
                   AIMS-<ver>-arm64-emulation-Setup.exe，安装时会在报告里注明
                   "数据库以 x64 仿真运行"，并要求在真机上完成验收测试

    两种模式都要求：Python 打包出的 EXE 与解释器架构一致（在 ARM64 机器上
    用 ARM64 版 Python 构建），否则 EXE 本身又变成仿真执行，得不偿失。

.EXAMPLE
    .\build_arm64.ps1                     # 生成仿真版安装包（默认）
    .\build_arm64.ps1 -Native             # 有 ARM64 PostgreSQL 时的原生包
#>
[CmdletBinding()]
param(
    [string]$Python,
    [string]$PgVersion = '16.4-1',
    [switch]$Native,
    [switch]$SkipAi,
    [switch]$UpdateLock
)

. (Join-Path $PSScriptRoot 'build_common.ps1')

$version = Get-AimsVersion
$arch = 'arm64'
Write-Step "AIMS $version / $arch 安装包构建" 'Green'

# --------------------------------------------------------------------------- #
Write-Step "1/7 依赖与架构检查"
& (Join-Path $PSScriptRoot 'dependency_check.ps1') -ForBuild -Arch $arch
$Python = Resolve-AimsPython -Preferred $Python
$pyArch = (& $Python -c "import platform;print(platform.machine())").Trim()
Write-Host "构建 Python 架构：$pyArch"
if ($Native -and $pyArch -notmatch 'ARM64') {
    throw "当前构建机是 $pyArch，无法产出 ARM64 原生 EXE。请在 Windows 11 ARM64 机器上（或 ARM64 版 Python 环境里）构建。"
}
if (-not $Native -and $pyArch -notmatch 'ARM64') {
    Write-Host "  ! 当前是 x64 构建机：生成的 EXE 会在 ARM64 上以仿真方式运行。" -ForegroundColor Yellow
    Write-Host "    建议在 ARM64 机器上重跑本脚本，得到真正的 ARM64 可执行文件。" -ForegroundColor Yellow
}

# --------------------------------------------------------------------------- #
Write-Step "2/7 内置 PostgreSQL（ARM64 策略）"
$pgHome = Join-Path $script:VendorDir 'pgsql-arm64'
if ($Native) {
    if (-not (Test-Path -LiteralPath (Join-Path $pgHome 'bin\pg_ctl.exe'))) {
        throw @"
未找到 ARM64 版 PostgreSQL：$pgHome\bin\pg_ctl.exe

获取方式（任选其一）：
  a) 使用 MSYS2 / vcpkg 交叉编译 PostgreSQL 16 到 aarch64-windows
  b) 向 PostgreSQL 商业支持方索取 ARM64 Windows 构建
  c) 采用 -Emulation 模式（x64 数据库 + Windows 11 ARM64 仿真），功能一致

在没有 ARM64 数据库的前提下，本脚本不会生成名为 ARM64 原生 的安装包。
"@
    }
    $env:AIMS_PG_HOME_FOR_BUILD = $pgHome
} else {
    if (-not (Test-Path -LiteralPath (Join-Path $pgHome 'bin\pg_ctl.exe'))) {
        Write-Host "  仿真模式：复用 x64 的 vendor\pgsql" -ForegroundColor DarkGray
        $pgHome = Get-VendorPostgres -Version $PgVersion -Arch x64 -UpdateLock:$UpdateLock
    }
    # 安装包统一从 vendor\pgsql 取文件；仿真模式下它就是 x64 版本
    $target = Join-Path $script:VendorDir 'pgsql'
    if ($pgHome -ne $target -and -not (Test-Path -LiteralPath (Join-Path $target 'bin\pg_ctl.exe'))) {
        New-Item -ItemType Directory -Path $target -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $pgHome '*') -Destination $target -Recurse -Force
    }
}

# --------------------------------------------------------------------------- #
Write-Step "3/7 准备依赖"
$req = Join-Path $script:RepoRoot 'requirements-exe.txt'
Invoke-Checked -Exe $Python -Arguments @('-m', 'pip', 'install', '--upgrade', 'pip', 'wheel', 'setuptools') -What 'pip 升级'
Invoke-Checked -Exe $Python -Arguments @('-m', 'pip', 'install', '-r', $req) -What '安装后端依赖'
Invoke-Checked -Exe $Python -Arguments @('-m', 'pip', 'install', 'pyinstaller>=6.6', 'pystray>=0.19.5', 'pillow>=10.3.0') -What '安装打包工具'
if (-not $SkipAi) {
    & $Python -m pip install --only-binary=:all: 'llama-cpp-python==0.3.2'
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ! ARM64 上没有匹配的 llama-cpp-python 轮子：本次构建的 AI 问答使用规则引擎（会在自检中如实报告）" -ForegroundColor Yellow
    }
}

# --------------------------------------------------------------------------- #
Write-Step "4/7 打包"
if (Test-Path -LiteralPath $script:DistDir) { Remove-Item -LiteralPath $script:DistDir -Recurse -Force }
New-Item -ItemType Directory -Path $script:DistDir -Force | Out-Null
Build-Launcher -Python $Python -OutDir (Join-Path $script:DistDir 'launcher') -Version $version
Build-Backend -Python $Python -OutDir (Join-Path $script:DistDir 'backend')

Write-Step "5/7 冒烟测试"
$backendExe = Join-Path $script:DistDir 'backend\AIMS-Backend\AIMS-Backend.exe'
Invoke-Checked -Exe $backendExe -Arguments @('deploy', 'version') -What '后端 EXE 自检'
Invoke-Checked -Exe (Join-Path $script:DistDir 'launcher\AIMS.exe') -Arguments @('--version') -What '启动器 EXE 自检'

# --------------------------------------------------------------------------- #
Write-Step "6/7 编译安装程序"
if ($Native) {
    $setup = Build-Installer -Version $version -Arch arm64
    $label = 'arm64（原生）'
} else {
    # 仿真包沿用 arm64 的安装判定（ArchitecturesAllowed=arm64），但文件名标注 emulation
    $setup = Build-Installer -Version $version -Arch arm64
    $renamed = Join-Path (Split-Path -Parent $setup) ([System.IO.Path]::GetFileNameWithoutExtension($setup) -replace '-arm64-Setup$', '-arm64-emulation-Setup') + '.exe'
    Move-Item -LiteralPath $setup -Destination $renamed -Force
    $setup = $renamed
    $label = 'arm64（数据库 x64 仿真）'
}
Write-Host "  产物：$setup（$label）" -ForegroundColor Green

# --------------------------------------------------------------------------- #
Write-Step "7/7 构建报告"
$report = [ordered]@{
    version          = $version
    arch             = $arch
    build_python_arch = $pyArch
    native           = [bool]$Native
    postgres         = $(if ($Native) { 'arm64 原生' } else { 'x64（Windows 11 ARM64 仿真运行）' })
    built_at         = (Get-Date).ToString('s')
    installer        = $setup
    installer_size_mb = [math]::Round((Get-Item -LiteralPath $setup).Length / 1MB, 1)
    sha256           = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash.ToLower()
    verification_required = '必须在 Windows 11 ARM64 真机上完成安装/启动/功能/卸载测试后才可对外声明兼容'
}
New-Item -ItemType Directory -Path $script:BuildDir -Force | Out-Null
$reportPath = Join-Path $script:BuildDir "build-report-arm64$(if($Native){''}else{'-emulation'}).json"
($report | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Host ($report | ConvertTo-Json -Depth 4)
exit 0
