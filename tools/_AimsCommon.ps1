<#
.SYNOPSIS
    AIMS 运维脚本公共函数（被 tools 下其它脚本 dot-source 引用）。

.DESCRIPTION
    这一层只做三件事：定位安装（程序目录 + 数据目录 + 后端可执行文件）、
    统一调用后端自带的 `deploy` 子命令、把所有操作写进同一个运维日志。
    真正的业务逻辑在 Python 侧（app/deploy），脚本里不重复实现。

    约定：所有函数在失败时抛出带中文说明的异常，脚本顶层捕获后写日志并以
    非 0 退出码结束 —— 计划任务和安装程序都依赖退出码判断成败。
#>

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

function Get-AimsRoot {
    <# 程序目录：本脚本位于 <root>\tools\，因此回退一级 #>
    param([string]$Root)
    if ($Root) { return (Resolve-Path -LiteralPath $Root).Path }
    if ($env:AIMS_ROOT) { return $env:AIMS_ROOT }
    $here = Split-Path -Parent $PSScriptRoot
    return $here
}

function Get-AimsDataRoot {
    <# 数据目录：产品安装为 C:\ProgramData\AIMS；绿色运行为 <root>\aims_data #>
    param([string]$Root)
    if ($env:AIMS_DATA_ROOT) { return $env:AIMS_DATA_ROOT }
    $r = Get-AimsRoot -Root $Root
    $programData = Join-Path $env:ProgramData 'AIMS'
    if (Test-Path -LiteralPath (Join-Path $programData 'Config')) { return $programData }
    if (Test-Path -LiteralPath (Join-Path $programData 'Database')) { return $programData }
    $legacy = Join-Path $r 'aims_data'
    if (Test-Path -LiteralPath $legacy) { return $legacy }
    return $programData
}

function Get-AimsBackend {
    <# 后端可执行文件：安装版是 AIMS-Backend.exe，源码版退回 python run_standalone.py #>
    param([string]$Root)
    $r = Get-AimsRoot -Root $Root
    $candidates = @(
        (Join-Path $r 'AIMS-Backend\AIMS-Backend.exe'),
        (Join-Path $r 'AIMS-Backend.exe')
    )
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c) { return @{ Path = $c; Args = @() } }
    }
    $script = Join-Path $r 'run_standalone.py'
    if (Test-Path -LiteralPath $script) {
        $python = (Get-Command python -ErrorAction SilentlyContinue)
        if (-not $python) { throw "找不到 AIMS-Backend.exe，也没有可用的 python 解释器：$r" }
        return @{ Path = $python.Source; Args = @($script) }
    }
    throw "安装不完整：在 $r 下找不到 AIMS-Backend.exe。请重新运行安装程序（覆盖安装不会影响数据）。"
}

function Test-AimsElevated {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Write-AimsLog {
    param(
        [Parameter(Mandatory = $true)][string]$Message,
        [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO',
        [string]$Root
    )
    $stamp = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    $line = "$stamp [$Level] $Message"
    Write-Host $line
    try {
        $dataRoot = Get-AimsDataRoot -Root $Root
        $logDir = Join-Path $dataRoot 'Logs'
        if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        Add-Content -LiteralPath (Join-Path $logDir 'ops.log') -Value $line -Encoding UTF8
    } catch {
        # 日志写不进去不能反过来把运维操作弄失败
    }
}

function Invoke-AimsDeploy {
    <#
      调用 `AIMS-Backend.exe deploy <参数>`，把 stdout/stderr 原样带到控制台，
      返回退出码。加 -PassThruArgs 传数组更安全（避免引号被二次解释）。
    #>
    param(
        [Parameter(Mandatory = $true)][string[]]$DeployArgs,
        [string]$Root,
        [switch]$AllowFailure
    )
    $backend = Get-AimsBackend -Root $Root
    $r = Get-AimsRoot -Root $Root
    $argv = @()
    $argv += $backend.Args
    $argv += @('deploy', '--root', $r)
    $argv += $DeployArgs

    Write-AimsLog ("执行: " + $backend.Path + " " + ($argv -join ' ')) -Root $Root
    & $backend.Path @argv 2>&1 | ForEach-Object { Write-Host $_ }
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    if ($code -ne 0 -and -not $AllowFailure) {
        throw ("deploy " + ($DeployArgs -join ' ') + " 失败，退出码 $code。日志：" + (Join-Path (Get-AimsDataRoot -Root $Root) 'Logs'))
    }
    return $code
}

function Wait-AimsHttp {
    <# 轮询 /health 直到 200 或超时；返回 $true/$false #>
    param(
        [int]$Port = 8000,
        [int]$TimeoutSeconds = 120,
        [string]$Root
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    $url = "http://127.0.0.1:$Port/health"
    while ((Get-Date) -lt $deadline) {
        try {
            $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 3 -ErrorAction Stop
            if ($resp.StatusCode -eq 200) {
                Write-AimsLog "应用已就绪：$url" -Root $Root
                return $true
            }
        } catch {
            Start-Sleep -Milliseconds 800
        }
    }
    Write-AimsLog "等待应用就绪超时（$TimeoutSeconds 秒）：$url" -Level WARN -Root $Root
    return $false
}

function Get-AimsApiPort {
    param([string]$Root)
    $dataRoot = Get-AimsDataRoot -Root $Root
    $envFile = Join-Path $dataRoot 'Config\aims.env'
    if (Test-Path -LiteralPath $envFile) {
        $line = Select-String -LiteralPath $envFile -Pattern '^\s*PORT\s*=\s*(\d+)' | Select-Object -First 1
        if ($line) { return [int]$line.Matches[0].Groups[1].Value }
    }
    return 8000
}
