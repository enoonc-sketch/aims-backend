<#
.SYNOPSIS
    修复安装：数据库起不来、配置丢了、升级后应用打不开时先跑这个。

.DESCRIPTION
    修复 = 重新推导一遍部署，但**不改动业务数据**：
      · 停全部进程 → 保留现有配置与数据重新 provision
      · 补齐缺失的 Windows 服务注册、计划任务、快捷方式
      · 最后跑 doctor，把结果写进 Logs\doctor.json 并回显

    这是"重装"之前应该先试的一步：多数现场故障（服务没注册、端口被改、
    配置文件被手工编辑坏）都能在不碰数据的前提下修好。

.EXAMPLE
    .\Repair-Aims.ps1
#>
[CmdletBinding()]
param(
    [string]$Root,
    [int]$ApiPort = 0,
    [switch]$KeepService
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    if (-not (Test-AimsElevated)) {
        Write-AimsLog "提示：未以管理员身份运行，若需要修复 Windows 服务注册会失败。" -Level WARN -Root $r
    }
    & (Join-Path $PSScriptRoot 'Stop-Aims.ps1') -Root $r -Quiet -StopDatabase -Force | Out-Null

    if ($ApiPort -eq 0) { $ApiPort = Get-AimsApiPort -Root $r }
    $mode = 'process'
    if ($KeepService -or (Get-Service -Name 'AIMSPostgreSQL' -ErrorAction SilentlyContinue)) { $mode = 'service' }

    Invoke-AimsDeploy -DeployArgs @('provision', '--mode', $mode, '--keep-config', '--api-port', "$ApiPort") -Root $r | Out-Null
    $code = Invoke-AimsDeploy -DeployArgs @('doctor', '--api-port', "$ApiPort") -Root $r -AllowFailure
    if ($code -ne 0) {
        Write-AimsLog "修复后自检仍有问题（退出码 $code），请看 Logs\doctor.json。" -Level WARN -Root $r
        $exit = $code
    } else {
        Write-AimsLog "修复完成，自检通过。可双击桌面图标启动。" -Root $r
    }
}
catch {
    Write-AimsLog ("修复失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
