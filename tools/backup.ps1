<#
.SYNOPSIS
    一键备份 AIMS 数据（PostgreSQL + 配置 + 上传文件清单）。

.DESCRIPTION
    备份产物是一个自校验的 .dump 归档 + 同名 .json 元数据（版本、时间、大小、
    SHA-256），存放在数据目录 Backup\ 下；-Export 可以再复制一份到 U 盘或网络盘，
    满足"异地保存"的现场要求。

    数据库没启动时脚本会先尝试启动它，避免"备份失败因为库没开"这种低级故障。

.EXAMPLE
    .\backup.ps1
    .\backup.ps1 -Reason pre-maintenance -Export E:\aims-backup
#>
[CmdletBinding()]
param(
    [string]$Root,
    [string]$Reason = 'manual',
    [string]$Export,
    [int]$Keep = 0
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    Invoke-AimsDeploy -DeployArgs @('start') -Root $r -AllowFailure | Out-Null

    $deployArgs = @('backup', '--reason', $Reason)
    if ($Keep -gt 0) { $deployArgs += @('--keep', "$Keep") }
    if ($Export) {
        if (-not (Test-Path -LiteralPath $Export)) { New-Item -ItemType Directory -Path $Export -Force | Out-Null }
        $deployArgs += @('--export', $Export)
    }
    Invoke-AimsDeploy -DeployArgs $deployArgs -Root $r | Out-Null
    Write-AimsLog "备份完成（原因：$Reason）。" -Root $r
}
catch {
    Write-AimsLog ("备份失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
