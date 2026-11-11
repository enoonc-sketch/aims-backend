<#
.SYNOPSIS
    覆盖升级 AIMS：备份 → 停服 → 更新结构 → 自检 → 失败自动回滚。

.DESCRIPTION
    设计原则是"数据不动、配置不动、能退回去"：

      1. 先做一次 pre-upgrade 备份（含 SHA-256），失败则中止升级；
      2. 停掉启动器/后端，数据库保持运行（迁移需要它）；
      3. `deploy upgrade` 只做加法：建缺表、补缺列、装新扩展，不删除任何数据；
      4. `deploy doctor` 自检；不通过时从 rollback\<旧版本> 还原程序文件，
         再用第 1 步的备份恢复数据库，回到升级前状态并明确告知失败原因。

    程序文件本身的替换由安装包完成（Setup 会在复制前调用本脚本的 -PrepareOnly）。

.EXAMPLE
    .\upgrade.ps1
    .\upgrade.ps1 -PrepareOnly          # 安装程序在覆盖文件前调用：只备份与停服
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$PrepareOnly,
    [int]$ApiPort = 8000
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
$r = $null
$backupName = $null
try {
    $r = Get-AimsRoot -Root $Root
    $dataRoot = Get-AimsDataRoot -Root $r

    # 1) 备份（数据库没开就先开）
    Invoke-AimsDeploy -DeployArgs @('start') -Root $r -AllowFailure | Out-Null
    Invoke-AimsDeploy -DeployArgs @('backup', '--reason', 'pre-upgrade') -Root $r | Out-Null
    $latest = Get-ChildItem -LiteralPath (Join-Path $dataRoot 'Backup') -Filter '*.dump' -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($latest) { $backupName = $latest.Name }
    Write-AimsLog "升级前备份：$backupName" -Root $r

    # 2) 停程序（数据库保留，迁移要用）
    & (Join-Path $PSScriptRoot 'Stop-Aims.ps1') -Root $r -Quiet | Out-Null
    if ($PrepareOnly) {
        Write-AimsLog "准备工作完成，安装程序可以安全替换程序文件。" -Root $r
        exit 0
    }

    # 3) 结构升级 + 4) 自检
    Invoke-AimsDeploy -DeployArgs @('upgrade', '--keep-config', '--api-port', "$ApiPort") -Root $r | Out-Null
    $doctorCode = Invoke-AimsDeploy -DeployArgs @('doctor', '--api-port', "$ApiPort") -Root $r -AllowFailure
    if ($doctorCode -eq 0) {
        Write-AimsLog "升级完成，自检通过。" -Root $r
        exit 0
    }

    # 5) 回滚
    Write-AimsLog "自检未通过（退出码 $doctorCode），开始回滚。" -Level WARN -Root $r
    $rollbackRoot = Join-Path $r 'rollback'
    $copies = @()
    if (Test-Path -LiteralPath $rollbackRoot) {
        $copies = Get-ChildItem -LiteralPath $rollbackRoot -Directory | Sort-Object Name -Descending
    }
    if ($copies.Count -gt 0) {
        $src = Join-Path $copies[0].FullName 'AIMS-Backend'
        if (Test-Path -LiteralPath $src) {
            $dst = Join-Path $r 'AIMS-Backend'
            Write-AimsLog "还原程序文件：$src -> $dst" -Root $r
            Copy-Item -LiteralPath $src -Destination $dst -Recurse -Force
        }
    } else {
        Write-AimsLog "没有找到可回滚的程序副本（rollback 目录为空）。" -Level WARN -Root $r
    }
    if ($backupName) {
        Write-AimsLog "还原数据库：$backupName" -Root $r
        Invoke-AimsDeploy -DeployArgs @('restore', $backupName, '--yes') -Root $r -AllowFailure | Out-Null
    }
    Write-AimsLog "已回滚到升级前状态。请把上面的 [FAIL] 项和日志发给技术支持。" -Level ERROR -Root $r
    $exit = 4
}
catch {
    Write-AimsLog ("升级失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
