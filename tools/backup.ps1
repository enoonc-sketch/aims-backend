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
    [int]$Keep = 0,
    # 从开始菜单快捷方式调用时用 -Notify：全程无控制台窗口，结束后用原生弹窗
    # 告诉用户成功还是失败。命令行/计划任务调用不要加，避免弹窗卡住自动化。
    [switch]$Notify
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
$backupFile = $null
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

    $backupDir = Join-Path (Get-AimsDataRoot -Root $r) 'Backup'
    $latest = Get-ChildItem -LiteralPath $backupDir -Filter '*.dump' -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) { $backupFile = $latest.FullName }
}
catch {
    Write-AimsLog ("备份失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}

if ($Notify) {
    # 隐藏窗口执行时，这是用户唯一能看到的结果反馈；不能"跑完就算成功"。
    try {
        $shell = New-Object -ComObject Wscript.Shell
        if ($exit -eq 0) {
            $msg = "备份已完成。" + [Environment]::NewLine
            if ($backupFile) { $msg += [Environment]::NewLine + "文件：" + $backupFile }
            $msg += [Environment]::NewLine + [Environment]::NewLine + "如需异地保存，可用备份脚本的 -Export 参数复制到 U 盘或网络盘。"
            $shell.Popup($msg, 0, 'AIMS 备份', 64) | Out-Null
        } else {
            $msg = "备份失败，数据未被破坏。" + [Environment]::NewLine + [Environment]::NewLine +
                   "请打开「AIMS 诊断报告」或查看数据目录下的 Logs\backup*.log 获取详细原因。"
            $shell.Popup($msg, 0, 'AIMS 备份', 16) | Out-Null
        }
    } catch {
        # 弹窗失败不能影响退出码（例如无桌面会话时）
    }
}

exit $exit
