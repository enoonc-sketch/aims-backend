<#
.SYNOPSIS
    依赖检查：构建机（-ForBuild）与目标机（默认）两套规则。

.DESCRIPTION
    构建机检查的是"能不能打包"：Python 版本、pip 包、Inno Setup、磁盘、架构。
    目标机检查的是"装完能不能跑"：Windows 版本、架构、VC 运行库、端口、权限、
    杀毒软件干扰、磁盘空间 —— 这些正是现场装不起来的高频原因，所以要在安装
    之前就报出来，而不是等用户双击图标黑屏。

    退出码：0 全部通过（允许有 WARN）；1 有 FAIL。
#>
[CmdletBinding()]
param(
    [switch]$ForBuild,
    [ValidateSet('x64', 'arm64')][string]$Arch = 'x64',
    [string]$Root,
    [switch]$Json
)

$script:results = @()

function Add-Result {
    param([string]$Name, [string]$Status, [string]$Detail, [string]$Fix = '')
    $script:results += [pscustomobject]@{ 项目 = $Name; 状态 = $Status; 详情 = $Detail; 处理 = $Fix }
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# --------------------------------------------------------------------------- #
if ($ForBuild) {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
    if ($py) {
        $ver = (& $py.Source -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null)
        if ($ver -in @('3.10', '3.11', '3.12')) {
            Add-Result 'Python' 'OK' "$($py.Source) $ver"
        } else {
            Add-Result 'Python' 'FAIL' "$ver（llama-cpp-python 只在 3.10-3.12 有预编译轮子）" '安装 Python 3.11 或 3.12 并用 -Python 指定'
        }
        $pkgs = @('fastapi', 'uvicorn', 'sqlalchemy', 'psycopg2', 'pandas', 'numpy')
        foreach ($p in $pkgs) {
            & $py.Source -c "import $p" 2>$null
            if ($LASTEXITCODE -eq 0) { Add-Result "依赖 $p" 'OK' '已安装' }
            else { Add-Result "依赖 $p" 'WARN' '未安装' 'build_x64.ps1 会自动安装 requirements-exe.txt' }
        }
    } else {
        Add-Result 'Python' 'FAIL' '未找到 python/py' '安装 Python 3.11 x64 并勾选 Add to PATH'
    }

    $iscc = @(
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
    if ($iscc) { Add-Result 'Inno Setup' 'OK' $iscc }
    else { Add-Result 'Inno Setup' 'FAIL' '未找到 ISCC.exe' '安装 Inno Setup 6（含中文语言文件）' }

    $repo = Split-Path -Parent $PSScriptRoot
    foreach ($f in @('frontend.html', 'requirements-exe.txt', 'build_exe.spec', 'installer\AIMS.iss', 'cores\vendor\xlsx.full.min.js')) {
        $p = Join-Path $repo $f
        if (Test-Path -LiteralPath $p) { Add-Result "源文件 $f" 'OK' '存在' }
        else { Add-Result "源文件 $f" 'FAIL' '缺失' '恢复该文件后重新构建' }
    }

    $free = (Get-PSDrive -Name ((Get-Item $repo).PSDrive.Name)).Free / 1GB
    if ($free -ge 12) { Add-Result '磁盘空间' 'OK' ("{0:N1} GB 可用" -f $free) }
    else { Add-Result '磁盘空间' 'FAIL' ("{0:N1} GB 可用" -f $free) '构建至少需要 12 GB（两份产物 + 安装包）' }
}
else {
    # ---------------- 目标机 ----------------
    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]$os.BuildNumber
    if ($build -ge 22000) { Add-Result 'Windows 版本' 'OK' "$($os.Caption) $($os.Version)（Windows 11）" }
    elseif ($build -ge 17763) { Add-Result 'Windows 版本' 'OK' "$($os.Caption) $($os.Version)（Windows 10 1809+）" }
    else { Add-Result 'Windows 版本' 'FAIL' "$($os.Caption) $($os.Version)" '需要 Windows 10 1809 及以上 / Windows 11' }

    $archEnv = $env:PROCESSOR_ARCHITECTURE
    $isArm = $archEnv -eq 'ARM64'
    if ($Arch -eq 'arm64' -and -not $isArm) {
        Add-Result '系统架构' 'FAIL' "$archEnv" '这是 ARM64 安装包，请使用 x64 安装包'
    } elseif ($Arch -eq 'x64' -and $isArm) {
        Add-Result '系统架构' 'WARN' "$archEnv（可运行 x64 程序）" '建议改用 ARM64 安装包'
    } else {
        Add-Result '系统架构' 'OK' $archEnv
    }

    if (Test-IsAdmin) {
        Add-Result '管理员权限' 'OK' '已提权'
    } else {
        Add-Result '管理员权限' 'WARN' '当前不是管理员' '安装程序会自行请求提权（UAC）'
    }

    # VC++ 运行库：Python 3.11+ 与 PostgreSQL 都依赖它
    $vc = Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\vcruntime140_1.dll')
    if ($vc) { Add-Result 'VC++ 运行库' 'OK' 'vcruntime140_1.dll 存在' }
    else { Add-Result 'VC++ 运行库' 'WARN' '缺少 vcruntime140_1.dll' '安装包会自带，若启动报 0xc000007b 请安装 VC++ 2015-2022 x64 运行库' }

    foreach ($port in @(8000, 55432)) {
        $used = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
        if ($used) {
            Add-Result "端口 $port" 'WARN' "已被 PID $($used[0].OwningProcess) 占用" '安装程序会自动选择其它端口（PG）或提示已知冲突'
        } else {
            Add-Result "端口 $port" 'OK' '空闲'
        }
    }

    $free = (Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':'))).Free / 1GB
    if ($free -ge 8) { Add-Result '磁盘空间' 'OK' ("{0:N1} GB 可用" -f $free) }
    else { Add-Result '磁盘空间' 'FAIL' ("{0:N1} GB 可用" -f $free) '至少需要 8 GB（程序 3 GB + 数据库/备份空间）' }

    $defender = Get-MpComputerStatus -ErrorAction SilentlyContinue
    if ($defender -and $defender.RealTimeProtectionEnabled) {
        Add-Result '实时防护' 'WARN' 'Windows Defender 实时防护开启' '首次启动较慢属正常；如被拦截请把安装目录加入排除项'
    } else {
        Add-Result '实时防护' 'OK' '未检测到阻碍'
    }
}

# --------------------------------------------------------------------------- #
$fail = @($script:results | Where-Object { $_.状态 -eq 'FAIL' }).Count
$warn = @($script:results | Where-Object { $_.状态 -eq 'WARN' }).Count

if ($Json) {
    $script:results | ConvertTo-Json -Depth 3
} else {
    Write-Host ""
    Write-Host "================= 依赖检查 =================" -ForegroundColor Cyan
    $script:results | Format-Table -AutoSize -Wrap
    Write-Host ("结论：" + $(if ($fail -eq 0) { "通过" } else { "未通过" }) + "（FAIL $fail 项，WARN $warn 项）") `
        -ForegroundColor $(if ($fail -eq 0) { 'Green' } else { 'Red' })
}
if ($fail -gt 0) { exit 1 }
exit 0
