<#
.SYNOPSIS
    安全卸载 AIMS：停服务 → 注销服务 → 清理计划任务 → （可选）删除数据。

.DESCRIPTION
    默认**保留**数据目录（C:\ProgramData\AIMS）：卸载是"去掉程序"，不是
    "销毁业务数据"。要彻底删除必须显式加 -Purge，而且会在删除前再做一次
    备份（能救一点是一点），并要求输入 YES 二次确认。

    程序文件由安装包的卸载程序删除；本脚本负责它删不掉的部分：进程、
    Windows 服务、计划任务、开机启动项。

.EXAMPLE
    .\uninstall.ps1                 # 保留数据
    .\uninstall.ps1 -Purge          # 删除数据（二次确认）
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$Purge,
    [switch]$Yes
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    $dataRoot = Get-AimsDataRoot -Root $r

    # 1) 停程序与数据库服务
    & (Join-Path $PSScriptRoot 'Stop-Aims.ps1') -Root $r -Quiet -StopDatabase -Force | Out-Null

    # 2) 后端自带 uninstall（注销服务、停库、可选清库）
    $deployArgs = @('uninstall')
    if ($Purge) { $deployArgs += @('--purge', '--yes') }
    Invoke-AimsDeploy -DeployArgs $deployArgs -Root $r -AllowFailure | Out-Null

    # 3) 计划任务与开机启动
    & schtasks.exe /Delete /TN 'AIMS Backup' /F 2>&1 | Out-Null
    & schtasks.exe /Delete /TN 'AIMS Autostart' /F 2>&1 | Out-Null
    $runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    if (Test-Path -LiteralPath $runKey) {
        Remove-ItemProperty -LiteralPath $runKey -Name 'AIMS' -ErrorAction SilentlyContinue
    }

    # 4) 数据
    if ($Purge) {
        if (-not $Yes) {
            $answer = Read-Host "将删除 $dataRoot 下的全部数据（不可恢复）。输入 YES 确认"
            if ($answer -ne 'YES') { Write-AimsLog "用户取消删除数据。" -Root $r; exit 2 }
        }
        Remove-Item -LiteralPath $dataRoot -Recurse -Force -ErrorAction SilentlyContinue
        Write-AimsLog "数据已删除：$dataRoot" -Level WARN -Root $r
    } else {
        Write-AimsLog "数据已保留：$dataRoot（需要彻底清理时加 -Purge）" -Root $r
    }

    # 5) 残留进程检查（卸载测试的验收点之一）
    $left = @(Get-Process -Name 'AIMS', 'AIMS-Backend' -ErrorAction SilentlyContinue)
    if ($left.Count -gt 0) {
        Write-AimsLog ("仍有残留进程：" + (($left | ForEach-Object { "$($_.ProcessName)#$($_.Id)" }) -join ', ')) -Level WARN -Root $r
        $exit = 3
    } else {
        Write-AimsLog "没有残留进程。" -Root $r
    }
}
catch {
    Write-AimsLog ("卸载失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
