<#
.SYNOPSIS
    查看 AIMS 运行状态与诊断结果（现场排障的第一条命令）。

.DESCRIPTION
    输出三块内容：
      1. 部署状态（版本、端口、服务、备份数量、升级历史）
      2. 逐项自检（安装文件、数据目录、磁盘、配置、密钥、PostgreSQL、
         pgvector、端口、AI 模型、备份）
      3. 最近日志尾巴（启动器 / 后端 / PostgreSQL）

    加 -Json 输出机器可读结果，便于巡检脚本汇总。

.EXAMPLE
    .\Get-AimsStatus.ps1
    .\Get-AimsStatus.ps1 -Json | Out-File status.json
#>
[CmdletBinding()]
param(
    [string]$Root,
    [int]$ApiPort = 0,
    [switch]$Json,
    [int]$LogLines = 15
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    $dataRoot = Get-AimsDataRoot -Root $r
    if ($ApiPort -eq 0) { $ApiPort = Get-AimsApiPort -Root $r }

    if ($Json) {
        Invoke-AimsDeploy -DeployArgs @('status', '--json') -Root $r -AllowFailure | Out-Null
        Invoke-AimsDeploy -DeployArgs @('doctor', '--api-port', "$ApiPort", '--json', (Join-Path $dataRoot 'Logs\doctor.json')) -Root $r -AllowFailure | Out-Null
        Get-Content -LiteralPath (Join-Path $dataRoot 'Logs\doctor.json') -Raw -ErrorAction SilentlyContinue
        exit 0
    }

    Write-Host ""
    Write-Host "=========== AIMS 状态 ===========" -ForegroundColor Cyan
    Invoke-AimsDeploy -DeployArgs @('status') -Root $r -AllowFailure | Out-Null

    Write-Host ""
    Write-Host "=========== 自检 ================" -ForegroundColor Cyan
    $code = Invoke-AimsDeploy -DeployArgs @('doctor', '--api-port', "$ApiPort") -Root $r -AllowFailure

    Write-Host ""
    Write-Host "=========== 日志 ================" -ForegroundColor Cyan
    foreach ($name in @('launcher.log', 'backend.log', 'provision.log', 'ops.log')) {
        $path = Join-Path $dataRoot "Logs\$name"
        if (Test-Path -LiteralPath $path) {
            Write-Host "--- $name（最后 $LogLines 行）---" -ForegroundColor DarkGray
            Get-Content -LiteralPath $path -Tail $LogLines
            Write-Host ""
        }
    }
    $pgLogs = Join-Path $dataRoot 'Database\pg_log'
    if (Test-Path -LiteralPath $pgLogs) {
        $last = Get-ChildItem -LiteralPath $pgLogs -Filter '*.log' | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($last) {
            Write-Host "--- $($last.Name)（最后 $LogLines 行）---" -ForegroundColor DarkGray
            Get-Content -LiteralPath $last.FullName -Tail $LogLines
        }
    }

    if ($code -ne 0) { $exit = $code }
}
catch {
    Write-AimsLog ("读取状态失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
