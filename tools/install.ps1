<#
.SYNOPSIS
    无人值守安装 / 修复 AIMS 本地部署（安装包内部也用它，便于现场重跑）。

.DESCRIPTION
    只做"部署"这件事：建集群 → 建库建表 → 装扩展 → 部署模型 → 首次备份 →
    健康检查。程序文件由安装程序（或解压发行包）负责，本脚本假定它们已就位。

    幂等：可以反复运行；已有数据、已有配置不会被破坏（--keep-config）。

.EXAMPLE
    .\install.ps1 -Service
    .\install.ps1 -NoSeed -PgPort 55440 -ApiPort 8080
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$Service,          # 注册 PostgreSQL 为 Windows 服务（需管理员）
    [switch]$NoSeed,           # 不写入演示数据（生产首次安装建议加上）
    [int]$PgPort = 0,
    [int]$ApiPort = 8000,
    [string]$AdminUser = 'admin',
    [string]$AdminPassword
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    if ($Service -and -not (Test-AimsElevated)) {
        throw "注册 Windows 服务需要管理员权限。请右键“以管理员身份运行” PowerShell 后重试。"
    }

    $mode = 'process'
    if ($Service) { $mode = 'service' }

    $deployArgs = @('provision', '--mode', $mode, '--api-port', "$ApiPort", '--admin-user', $AdminUser)
    if ($PgPort -gt 0)     { $deployArgs += @('--pg-port', "$PgPort") }
    if ($NoSeed)           { $deployArgs += '--no-seed' }
    if ($AdminPassword)    { $deployArgs += @('--admin-password', $AdminPassword) }
    if (-not $Service)     { $deployArgs += '--no-service' }

    Invoke-AimsDeploy -DeployArgs $deployArgs -Root $r | Out-Null

    # 健康检查：不通过就以非 0 退出，安装程序据此判定"装失败"
    $doctorCode = Invoke-AimsDeploy -DeployArgs @('doctor', '--api-port', "$ApiPort") -Root $r -AllowFailure
    if ($doctorCode -ne 0) {
        Write-AimsLog "部署完成但自检未全部通过（退出码 $doctorCode）。请看上面的 [FAIL] 项。" -Level WARN -Root $r
        $exit = $doctorCode
    } else {
        Write-AimsLog "安装完成，自检通过。数据目录：$(Get-AimsDataRoot -Root $r)" -Root $r
    }
}
catch {
    Write-AimsLog ("安装失败：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
