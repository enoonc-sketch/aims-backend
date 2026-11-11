<#
.SYNOPSIS
    停止 AIMS：先让启动器正常退出，再收后端进程，最后（可选）停数据库。

.DESCRIPTION
    顺序不能反：后端还连着数据库时直接停库，会让 PostgreSQL 做崩溃恢复。
    因此这里是"启动器 → 后端 → 数据库"的逆序收尾。

    只停本机 AIMS 自己的进程：通过进程可执行文件路径 + 监听端口双重确认，
    绝不按名字"杀所有 python.exe"（那会误伤用户其它程序）。

.EXAMPLE
    .\Stop-Aims.ps1                 # 停止界面与后端，数据库继续跑（下次启动更快）
    .\Stop-Aims.ps1 -StopDatabase   # 连数据库一起停（卸载/维护前）
    .\Stop-Aims.ps1 -Force          # 后端 15 秒没退出就强制结束进程树
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$StopDatabase,
    [switch]$Force,
    [switch]$Quiet
)

. (Join-Path $PSScriptRoot '_AimsCommon.ps1')

$exit = 0
try {
    $r = Get-AimsRoot -Root $Root
    $port = Get-AimsApiPort -Root $r

    # 1) 启动器（托盘）—— 它会在退出时收掉自己拉起的后端
    $launchers = Get-Process -Name 'AIMS' -ErrorAction SilentlyContinue
    foreach ($p in $launchers) {
        try {
            if (-not $p.Path -or $p.Path -like "$r*") {
                if (-not $Quiet) { Write-AimsLog "关闭启动器 PID $($p.Id)" -Root $r }
                $null = $p.CloseMainWindow()
                if (-not $p.WaitForExit(15000) -and $Force) { $p.Kill() }
            }
        } catch { }
    }

    # 2) 后端：按"监听该端口的进程"定位，比进程名可靠
    $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($conn) {
        $procId = $conn.OwningProcess
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            $path = ''
            try { $path = $proc.Path } catch { }
            $owned = ($path -and ($path -like "$r*")) -or ($proc.ProcessName -like 'AIMS-Backend*')
            if ($owned) {
                if (-not $Quiet) { Write-AimsLog "停止后端 PID $procId（端口 $port）" -Root $r }
                if ($Force) {
                    & taskkill.exe /PID $procId /T /F 2>&1 | Out-Null
                } else {
                    $null = $proc.CloseMainWindow()
                    if (-not $proc.WaitForExit(15000)) {
                        Write-AimsLog "后端未在 15 秒内退出，强制结束进程树。" -Level WARN -Root $r
                        & taskkill.exe /PID $procId /T /F 2>&1 | Out-Null
                    }
                }
            } else {
                Write-AimsLog "端口 $port 被其它程序占用（$path），不动它。请手工确认。" -Level WARN -Root $r
                $exit = 2
            }
        }
    }

    # 3) 数据库
    if ($StopDatabase) {
        Invoke-AimsDeploy -DeployArgs @('stop') -Root $r -AllowFailure | Out-Null
    }
    if (-not $Quiet) { Write-AimsLog "AIMS 已停止。" -Root $r }
}
catch {
    Write-AimsLog ("停止过程中出错：" + $_.Exception.Message) -Level ERROR -Root $Root
    $exit = 1
}
exit $exit
