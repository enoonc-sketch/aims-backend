<#
.SYNOPSIS
    从备份恢复 AIMS 数据。

.DESCRIPTION
    流程：确认应用已退出 → 校验归档（pg_restore -l + SHA-256）→ 自动做一次
    "恢复前安全备份" → 恢复 → 结构自检。任何一步失败都不会把现有数据改坏。

    不给 -File 时恢复"最新一份"备份。

.EXAMPLE
    .\restore.ps1                        # 恢复最新备份（会二次确认）
    .\restore.ps1 -File aims-20260101-030000.dump -Yes
#>
[CmdletBinding()]
param(
    [string]$Root,
    [string]$File = 'latest',
    [switch]$Yes
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    $port = Get-AimsApiPort -Root $r
    if (Wait-AimsHttp -Port $port -TimeoutSeconds 2 -Root $r) {
        throw "AIMS 正在运行，请先执行 Stop-Aims.ps1 关闭程序再恢复（恢复会覆盖当前数据库）。"
    }

    $dataRoot = Get-AimsDataRoot -Root $r
    $candidate = $File
    if ($File -eq 'latest' -or -not $File) {
        $latest = Get-ChildItem -LiteralPath (Join-Path $dataRoot 'Backup') -Filter '*.dump' -ErrorAction SilentlyContinue |
                  Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $latest) { throw "备份目录里没有可用归档：$(Join-Path $dataRoot 'Backup')" }
        $candidate = $latest.Name
    }
    if (-not $Yes) {
        $answer = Read-Host "将用 $candidate 覆盖当前数据库（会先自动安全备份）。输入 YES 继续"
        if ($answer -ne 'YES') { Write-AimsLog "用户取消恢复。" -Root $r; exit 2 }
    }

    Invoke-AimsDeploy -DeployArgs @('start') -Root $r | Out-Null
    Invoke-AimsDeploy -DeployArgs @('restore', $candidate, '--yes') -Root $r | Out-Null
    Invoke-AimsDeploy -DeployArgs @('doctor') -Root $r -AllowFailure | Out-Null
    Write-AimsLog "恢复完成：$candidate。请重新启动 AIMS 验证业务数据。" -Root $r
}
catch {
    Write-AimsLog ("恢复失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
