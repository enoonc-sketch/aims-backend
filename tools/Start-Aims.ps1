<#
.SYNOPSIS
    启动 AIMS（数据库 → 后端 → 主界面）。托盘版请直接双击桌面图标。

.DESCRIPTION
    正常使用时用户只需要点桌面快捷方式；这个脚本给"没有桌面的场景"用：
    远程桌面批量部署、计划任务、出问题后手工拉起排查。
    加 -NoWindow 时不打开界面，仅确保服务在跑。

.EXAMPLE
    .\Start-Aims.ps1
    .\Start-Aims.ps1 -NoWindow -WaitSeconds 180
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$NoWindow,
    [int]$WaitSeconds = 120
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    $launcher = Join-Path $r 'AIMS.exe'
    $port = Get-AimsApiPort -Root $r

    if (Wait-AimsHttp -Port $port -TimeoutSeconds 2 -Root $r) {
        Write-AimsLog "AIMS 已在运行（端口 $port），不重复启动。" -Root $r
        if (-not $NoWindow) { Start-Process -FilePath $launcher -WorkingDirectory $r | Out-Null }
    }
    elseif (Test-Path -LiteralPath $launcher) {
        $launchArgs = @()
        if ($NoWindow) { $launchArgs += '--no-browser' }
        Write-AimsLog "启动 $launcher $($launchArgs -join ' ')" -Root $r
        $null = Start-Process -FilePath $launcher -ArgumentList $launchArgs -WorkingDirectory $r -PassThru
        if (-not (Wait-AimsHttp -Port $port -TimeoutSeconds $WaitSeconds -Root $r)) {
            Write-AimsLog "启动失败：后端未在 $WaitSeconds 秒内就绪。请运行 Get-AimsStatus.ps1 查看诊断。" -Level ERROR -Root $r
            $exit = 3
        }
    }
    else {
        Write-AimsLog "没有找到 AIMS.exe，改用 deploy start 单独拉起数据库与后端。" -Level WARN -Root $r
        Invoke-AimsDeploy -DeployArgs @('start') -Root $r
        $exit = 0
    }
}
catch {
    Write-AimsLog ("启动失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
