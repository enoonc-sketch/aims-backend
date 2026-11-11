<#
.SYNOPSIS
    AIMS 构建脚本公共层（被 build_x64.ps1 / build_arm64.ps1 / package_release.ps1 引用）。

.DESCRIPTION
    构建机允许联网、允许装 Python/Node/Inno Setup；**目标机什么都不允许装**。
    所以这里的每一步都在把"构建机的便利"转成"目标机的零依赖"：

        Python 环境  →  PyInstaller 打包的独立 EXE（内嵌解释器与全部三方库）
        前端资源      →  frontend.html（单文件，JS 全部内联，无 CDN）
        PostgreSQL    →  vendor\pgsql（官方 zip 发行版，随包分发）
        pgvector      →  vendor\pgsql\lib\vector.dll + share\extension\vector*
        安装程序      →  Inno Setup 编译出的单文件 Setup.exe

    所有第三方下载都固定版本 + 记录 SHA-256（vendor.lock.json），
    重复构建得到同一套二进制；hash 不匹配就中止，不做"大概能跑"的构建。
#>

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:VendorDir = Join-Path $script:RepoRoot 'vendor'
$script:DistDir = Join-Path $script:RepoRoot 'dist'
$script:BuildDir = Join-Path $script:RepoRoot 'build_out'
$script:LockFile = Join-Path $PSScriptRoot 'vendor.lock.json'

# --------------------------------------------------------------------------- #
function Write-Step {
    param([string]$Message, [string]$Color = 'Cyan')
    Write-Host ""
    Write-Host ("=== " + $Message + " ===") -ForegroundColor $Color
}

function Get-AimsVersion {
    <# 版本号唯一来源：app/deploy/__init__.py 的 APP_VERSION（避免三处手改不一致） #>
    param([string]$RepoRoot = $script:RepoRoot)
    $init = Join-Path $RepoRoot 'app\deploy\__init__.py'
    if (-not (Test-Path -LiteralPath $init)) { throw "找不到版本文件：$init" }
    $m = Select-String -LiteralPath $init -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if (-not $m) { throw "无法从 $init 解析 APP_VERSION" }
    return $m.Matches[0].Groups[1].Value
}

function Resolve-AimsPython {
    <# 构建用的解释器：优先当前 venv，其次 py -3.12/3.11（llama-cpp-python 轮子覆盖最好） #>
    param([string]$Preferred = '')
    if ($Preferred) { return $Preferred }
    if ($env:VIRTUAL_ENV) {
        $cand = Join-Path $env:VIRTUAL_ENV 'Scripts\python.exe'
        if (Test-Path -LiteralPath $cand) { return $cand }
    }
    foreach ($v in @('3.12', '3.11', '3.10')) {
        $out = & py -$v -c "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return $out.Trim() }
    }
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($py) { return $py.Source }
    throw "找不到 Python。构建机需要 Python 3.10-3.12（目标机不需要）。"
}

function Test-NativeCommand {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-Checked {
    <# 执行外部命令，非 0 退出码直接抛错（构建失败必须立刻停，不能带病打包） #>
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$Arguments = @(),
        [string]$What = '',
        [string]$WorkDir = ''
    )
    Write-Host ("  > " + $Exe + " " + ($Arguments -join ' ')) -ForegroundColor DarkGray
    $p = @{ FilePath = $Exe; ArgumentList = $Arguments; NoNewWindow = $true; PassThru = $true }
    if ($WorkDir) { $p['WorkingDirectory'] = $WorkDir }
    $proc = Start-Process @p
    $proc.WaitForExit()
    if ($proc.ExitCode -ne 0) {
        throw ("$What 失败（退出码 " + $proc.ExitCode + "）：" + $Exe + " " + ($Arguments -join ' '))
    }
}

# --------------------------------------------------------------------------- #
function Read-VendorLock {
    if (-not (Test-Path -LiteralPath $script:LockFile)) { return @{} }
    return (Get-Content -LiteralPath $script:LockFile -Raw | ConvertFrom-Json)
}

function Assert-FileHash {
    <# 校验下载文件；第一次构建时用 -Update 记录，之后只允许一致 #>
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Key,
        [switch]$Update
    )
    $actual = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
    $lock = Read-VendorLock
    $expected = $null
    if ($lock -and $lock.PSObject.Properties.Name -contains $Key) { $expected = $lock.$Key }
    if ($Update -or -not $expected) {
        Write-Host "  记录 $Key = $actual" -ForegroundColor DarkGray
        $obj = @{}
        if ($lock) { foreach ($p in $lock.PSObject.Properties) { $obj[$p.Name] = $p.Value } }
        $obj[$Key] = $actual
        ($obj | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $script:LockFile -Encoding UTF8
        return $actual
    }
    if ($expected -ne $actual) {
        throw "校验失败：$Key 期望 $expected，实际 $actual。文件可能被替换或下载损坏，已中止构建。"
    }
    Write-Host "  校验通过 $Key" -ForegroundColor DarkGray
    return $actual
}

function Get-VendorPostgres {
    <#
      下载并解压 PostgreSQL Windows 二进制（EnterpriseDB 官方 zip 发行版）。
      结果放到 vendor\pgsql（内含 bin/lib/share），安装包直接带上它。
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Version,   # 例如 16.4-1
        [string]$Arch = 'x64',
        [switch]$UpdateLock
    )
    if ($Arch -ne 'x64') {
        throw "PostgreSQL 官方没有 Windows $Arch 原生发行版。ARM64 目标请使用 -AllowEmulation（x64 版在 Windows 11 ARM64 上通过仿真运行），或自行提供 vendor\pgsql-$Arch。"
    }
    $target = Join-Path $script:VendorDir 'pgsql'
    $marker = Join-Path $target 'bin\pg_ctl.exe'
    if (Test-Path -LiteralPath $marker) {
        Write-Host "  已存在 vendor\pgsql，跳过下载（删除该目录可强制重新获取）" -ForegroundColor DarkGray
        return $target
    }
    $zipName = "postgresql-$Version-windows-x64-binaries.zip"
    $url = "https://get.enterprisedb.com/postgresql/$zipName"
    $cache = Join-Path $script:VendorDir '_cache'
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $zip = Join-Path $cache $zipName
    if (-not (Test-Path -LiteralPath $zip)) {
        Write-Host "  下载 $url" -ForegroundColor DarkGray
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 1800
    }
    Assert-FileHash -Path $zip -Key "postgresql-$Version-x64" -Update:$UpdateLock | Out-Null

    Write-Host "  解压到 $target" -ForegroundColor DarkGray
    $tmp = Join-Path $script:VendorDir '_extract_pg'
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
    Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
    $inner = Join-Path $tmp 'pgsql'
    if (-not (Test-Path -LiteralPath $inner)) { throw "压缩包结构异常：没有 pgsql 目录" }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
    Move-Item -LiteralPath $inner -Destination $target
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue

    # 精简：去掉用不到的部分，安装包能小 200MB+（保留 bin/lib/share）
    foreach ($drop in @('pgAdmin 4', 'doc', 'include', 'symbols', 'StackBuilder')) {
        $p = Join-Path $target $drop
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue }
    }
    return $target
}

function Install-PgVector {
    <#
      pgvector 扩展：官方只提供源码，没有 Windows 预编译包。
      三种来源，按可用性依次尝试：
        1. vendor\pgvector\ 下已放好的预编译文件（企业内网常见做法）
        2. 本机有 MSVC + nmake，则从源码编译（需要 pg_config 在 PATH）
        3. 都没有 → 返回 $false，本次构建会把 EMBEDDING_STORAGE 设为 json，
           知识库改用"应用层余弦相似度"，功能不缺失、只是少一个索引加速。
    #>
    param([Parameter(Mandatory = $true)][string]$PgHome, [switch]$AllowBuild)
    $share = Join-Path $PgHome 'share\extension'
    $lib = Join-Path $PgHome 'lib'
    if ((Test-Path -LiteralPath (Join-Path $share 'vector.control')) -and
        (Test-Path -LiteralPath (Join-Path $lib 'vector.dll'))) {
        Write-Host "  pgvector 已就位" -ForegroundColor DarkGray
        return $true
    }
    $pre = Join-Path $script:VendorDir 'pgvector'
    if (Test-Path -LiteralPath $pre) {
        Write-Host "  使用 vendor\pgvector 预编译文件" -ForegroundColor DarkGray
        Copy-Item -LiteralPath (Join-Path $pre 'vector.dll') -Destination $lib -Force -ErrorAction SilentlyContinue
        Get-ChildItem -LiteralPath $pre -Filter 'vector*' -File | Where-Object { $_.Extension -in @('.sql', '.control') } |
            ForEach-Object { Copy-Item -LiteralPath $_.FullName -Destination $share -Force }
        return ((Test-Path -LiteralPath (Join-Path $share 'vector.control')))
    }
    if (-not $AllowBuild) {
        Write-Host "  未提供 pgvector（预编译文件缺失，且未允许编译）" -ForegroundColor Yellow
        return $false
    }
    $src = Join-Path $script:VendorDir '_cache\pgvector'
    if (-not (Test-Path -LiteralPath $src)) {
        $zip = Join-Path $script:VendorDir '_cache\pgvector.zip'
        Invoke-WebRequest -Uri 'https://github.com/pgvector/pgvector/archive/refs/tags/v0.8.0.zip' -OutFile $zip -UseBasicParsing -TimeoutSec 600
        Expand-Archive -LiteralPath $zip -DestinationPath (Split-Path -Parent $src) -Force
        Move-Item -LiteralPath (Join-Path (Split-Path -Parent $src) 'pgvector-0.8.0') -Destination $src
    }
    $pgConfig = Join-Path $PgHome 'bin\pg_config.exe'
    if (-not (Test-Path -LiteralPath $pgConfig)) { Write-Host "  缺少 pg_config.exe，跳过编译" -ForegroundColor Yellow; return $false }
    if (-not (Test-NativeCommand 'nmake')) {
        Write-Host "  未找到 nmake（需要 Visual Studio C++ 生成工具），跳过编译" -ForegroundColor Yellow
        return $false
    }
    Write-Host "  从源码编译 pgvector（可能需要几分钟）" -ForegroundColor DarkGray
    $env:PGROOT = $PgHome
    Invoke-Checked -Exe 'cmd.exe' -Arguments @('/c', "nmake /F Makefile.win PG_CONFIG=`"$pgConfig`" && nmake /F Makefile.win install PG_CONFIG=`"$pgConfig`"") -WorkDir (Join-Path $src 'src') -What "pgvector 编译"
    return ((Test-Path -LiteralPath (Join-Path $share 'vector.control')))
}

# --------------------------------------------------------------------------- #
function Build-Launcher {
    <# AIMS.exe：托盘启动器（onedir，启动快，便于替换单个 DLL） #>
    param([string]$Python, [string]$OutDir, [string]$Version)
    Invoke-Checked -Exe $Python -Arguments @('-m', 'PyInstaller', '--noconfirm', '--clean',
        '--distpath', $OutDir, '--workpath', (Join-Path $script:BuildDir 'pyi-launcher'),
        '--name', 'AIMS', (Join-Path $script:RepoRoot 'aims_launcher.py'),
        '--icon', (Join-Path $script:RepoRoot 'assets\aims.ico'),
        '--windowed', '--noupx',
        '--hidden-import', 'pystray._win32',
        '--hidden-import', 'PIL.ImageDraw',
        '--collect-submodules', 'app.launcher',
        # AIMS Desktop 原生窗口（pywebview → Windows 上经 pythonnet 驱动 WebView2）
        '--collect-submodules', 'webview',
        '--hidden-import', 'pythonnet',
        '--hidden-import', 'clr_loader') -What '启动器打包'
}

function Build-Backend {
    <# AIMS-Backend.exe：FastAPI 后端（onedir，含全部三方库与 llama.cpp） #>
    param([string]$Python, [string]$OutDir)
    $spec = Join-Path $script:RepoRoot 'build_exe.spec'
    if (-not (Test-Path -LiteralPath $spec)) { throw "缺少 build_exe.spec" }
    Invoke-Checked -Exe $Python -Arguments @('-m', 'PyInstaller', '--noconfirm', '--clean',
        '--distpath', $OutDir, '--workpath', (Join-Path $script:BuildDir 'pyi-backend'), $spec) -What '后端打包'
}

function Build-Installer {
    param(
        [Parameter(Mandatory = $true)][string]$Version,
        [ValidateSet('x64', 'arm64')][string]$Arch = 'x64',
        [string]$Iscc
    )
    if (-not $Iscc) {
        $candidates = @(
            (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
            (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
        )
        foreach ($c in $candidates) { if ($c -and (Test-Path -LiteralPath $c)) { $Iscc = $c; break } }
    }
    if (-not $Iscc -or -not (Test-Path -LiteralPath $Iscc)) {
        throw "找不到 ISCC.exe。请安装 Inno Setup 6（https://jrsoftware.org/isdl.php）后重试。"
    }
    $iss = Join-Path $script:RepoRoot 'installer\AIMS.iss'
    Invoke-Checked -Exe $Iscc -Arguments @("/DAppVersion=$Version", "/DBuildArch=$Arch", $iss) -What '编译安装程序'
    $out = Get-ChildItem -LiteralPath (Join-Path $script:RepoRoot 'installer_output') -Filter "AIMS-$Version-$Arch-Setup.exe" |
           Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $out) { throw "安装程序编译完成但找不到产物：installer_output\AIMS-$Version-$Arch-Setup.exe" }
    return $out.FullName
}

function Get-PackageSizeMB {
    param([string]$Path)
    return [math]::Round((Get-ChildItem -LiteralPath $Path -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
}
