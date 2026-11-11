<#
.SYNOPSIS
    注册 AIMS 的 Windows 计划任务：每日自动备份 + （可选）开机自启。

.DESCRIPTION
    备份任务是"无人值守也要有备份"的保障。默认每天 03:00 跑一次，保留 14 份；
    任务以 SYSTEM 运行，不需要用户登录，也不需要保存密码。

    -Remove 用来在卸载/迁移前清理干净（卸载脚本也会调用 schtasks 删除）。

.EXAMPLE
    .\Register-AimsTasks.ps1
    .\Register-AimsTasks.ps1 -Time 02:30 -Keep 30
    .\Register-AimsTasks.ps1 -Remove
#>
[CmdletBinding()]
param(
    [string]$Root,
    [string]$Time = '03:00',
    [int]$Keep = 14,
    [switch]$Remove
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    $taskName = 'AIMS Backup'

    if ($Remove) {
        & schtasks.exe /Delete /TN $taskName /F 2>&1 | Out-Null
        Write-AimsLog "已删除计划任务：$taskName" -Root $r
        exit 0
    }

    $script = Join-Path $PSScriptRoot 'backup.ps1'
    if (-not (Test-Path -LiteralPath $script)) { throw "找不到备份脚本：$script" }

    # powershell -File 会把参数原样传给脚本；用 SYSTEM 身份 + 最高权限，
    # 这样不需要在系统里保存任何账号口令。
    $action = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$script`" -Root `"$r`" -Reason scheduled -Keep $Keep"
    $taskArgs = @(
        '/Create', '/TN', $taskName, '/TR', $action,
        '/SC', 'DAILY', '/ST', $Time, '/RU', 'SYSTEM', '/RL', 'HIGHEST', '/F'
    )
    $out = & schtasks.exe @taskArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-AimsLog ("注册计划任务失败：" + ($out -join ' ')) -Level ERROR -Root $r
        exit 1
    }
    Write-AimsLog "已注册计划任务：$taskName（每日 $Time，保留 $Keep 份）" -Root $r
}
catch {
    Write-AimsLog ("注册计划任务失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
