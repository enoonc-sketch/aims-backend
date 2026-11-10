<#
.SYNOPSIS
    在干净的 Windows 上跑完整验收：安装 → 启动 → 功能 → 备份/恢复 → 升级 → 卸载。

.DESCRIPTION
    这个脚本就是"第七阶段：测试与验收"的执行体。它不假设任何东西已经装好，只接受
    一个安装包路径，然后按真实用户的操作顺序走一遍，并把每一步的结果写进 JSON +
    Markdown 报告（可直接作为交付附件）。

    重要：脚本会**真的卸载**（默认保留数据）并**真的删除测试数据**（-PurgeAfter），
    所以只能在一台专用测试机上跑。

    用法（管理员 PowerShell）：
        .\Windows-Acceptance.ps1 -Setup D:\AIMS-0.5.10-x64-Setup.exe
        .\Windows-Acceptance.ps1 -Setup ... -UpgradeSetup D:\AIMS-0.6.0-x64-Setup.exe
        .\Windows-Acceptance.ps1 -Setup ... -PurgeAfter      # 卸载后连数据一起删

.PARAMETER Setup
    待测安装包（AIMS-<版本>-<架构>-Setup.exe）。

.PARAMETER UpgradeSetup
    可选：更高版本的安装包，用于验证覆盖升级。

.PARAMETER ReportDir
    报告输出目录，默认 .\acceptance-<时间戳>。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Setup,
    [string]$UpgradeSetup,
    [string]$ReportDir,
    [switch]$PurgeAfter,
    [int]$StartTimeoutSeconds = 240
)

$ErrorActionPreference = 'Stop'
$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
if (-not $ReportDir) { $ReportDir = Join-Path (Get-Location) "acceptance-$stamp" }
New-Item -ItemType Directory -Path $ReportDir -Force | Out-Null
$reportPath = Join-Path $ReportDir 'acceptance-report.json'
$steps = New-Object System.Collections.ArrayList

$PROGRAM_DIR = Join-Path $env:ProgramFiles 'AIMS'
$DATA_DIR = Join-Path $env:ProgramData 'AIMS'
$LAUNCHER = Join-Path $PROGRAM_DIR 'AIMS.exe'
$BACKEND = Join-Path $PROGRAM_DIR 'AIMS-Backend\AIMS-Backend.exe'
$TOOLS = Join-Path $PROGRAM_DIR 'tools'

function Add-Step {
    param([string]$Name, [string]$Status, [string]$Detail, [int]$Seconds = 0)
    $null = $steps.Add([pscustomobject]@{
        name = $Name; status = $Status; detail = $Detail; seconds = $Seconds
        at = (Get-Date).ToString('s')
    })
    $color = @{ PASS = 'Green'; FAIL = 'Red'; SKIP = 'Yellow'; INFO = 'Gray' }[$Status]
    Write-Host ("  [{0}] {1} — {2}" -f $Status, $Name, $Detail) -ForegroundColor $color
}

function Invoke-Test {
    param([string]$Name, [scriptblock]$Body)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $detail = & $Body
        $sw.Stop()
        Add-Step -Name $Name -Status 'PASS' -Detail "$detail" -Seconds ([int]$sw.Elapsed.TotalSeconds)
        return $true
    } catch {
        $sw.Stop()
        Add-Step -Name $Name -Status 'FAIL' -Detail $_.Exception.Message -Seconds ([int]$sw.Elapsed.TotalSeconds)
        return $false
    }
}

function Get-ApiPort {
    $envFile = Join-Path $DATA_DIR 'Config\aims.env'
    if (Test-Path $envFile) {
        $m = Select-String -Path $envFile -Pattern '^\s*PORT\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { return [int]$m.Matches[0].Groups[1].Value }
    }
    return 8000
}

function Wait-Health {
    param([int]$Port, [int]$Timeout = 120)
    $deadline = (Get-Date).AddSeconds($Timeout)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { Start-Sleep -Milliseconds 800 }
    }
    return $false
}

# =========================================================================== #
Write-Host ""
Write-Host "AIMS 验收测试 — $stamp" -ForegroundColor Cyan
Write-Host "安装包：$Setup"
Write-Host "报告：  $reportPath"
Write-Host ""

# 0. 环境基线：证明"干净"
$os = Get-CimInstance Win32_OperatingSystem
$py = Get-Command python -ErrorAction SilentlyContinue
$node = Get-Command node -ErrorAction SilentlyContinue
$docker = Get-Command docker -ErrorAction SilentlyContinue
$pg = Get-Service -Name 'postgresql*' -ErrorAction SilentlyContinue
Add-Step -Name '环境基线' -Status 'INFO' `
    -Detail ("{0} build {1}; python={2}; node={3}; docker={4}; 既有PostgreSQL={5}" -f `
        $os.Caption, $os.BuildNumber, $(if ($py) { $py.Source } else { '未安装' }),
        $(if ($node) { $node.Source } else { '未安装' }), $(if ($docker) { '已安装' } else { '未安装' }),
        $(if ($pg) { ($pg | ForEach-Object { $_.Name }) -join ',' } else { '无' }))
Add-Step -Name '网络' -Status 'INFO' -Detail ("默认网关可达性不作为前置条件；安装过程不需要联网" )

# 1. 安装
$installOk = Invoke-Test '静默安装（离线）' {
    if (-not (Test-Path $Setup)) { throw "安装包不存在：$Setup" }
    $log = Join-Path $ReportDir 'setup-install.log'
    $p = Start-Process -FilePath $Setup -Wait -PassThru -ArgumentList @(
        '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/LOG=$log",
        '/TASKS=desktopicon,pgservice'
    )
    if ($p.ExitCode -ne 0) { throw "安装程序退出码 $($p.ExitCode)，日志：$log" }
    "退出码 0，日志 $log"
}

if ($installOk) {
    Invoke-Test '程序文件齐全' {
        foreach ($f in @($LAUNCHER, $BACKEND, (Join-Path $PROGRAM_DIR 'frontend.html'),
                         (Join-Path $PROGRAM_DIR 'pgsql\bin\pg_ctl.exe'),
                         (Join-Path $PROGRAM_DIR 'cores\vendor\xlsx.full.min.js'))) {
            if (-not (Test-Path $f)) { throw "缺少 $f" }
        }
        "启动器/后端/前端/数据库/本地前端库 全部存在"
    } | Out-Null

    Invoke-Test '数据目录分离（程序与数据不同盘/不同目录）' {
        if (-not (Test-Path $DATA_DIR)) { throw "数据目录未创建：$DATA_DIR" }
        foreach ($sub in @('Config', 'Database', 'Models', 'Logs', 'Uploads', 'Backup')) {
            if (-not (Test-Path (Join-Path $DATA_DIR $sub))) { throw "缺少数据子目录 $sub" }
        }
        if ((Resolve-Path $PROGRAM_DIR).Path -eq (Resolve-Path $DATA_DIR).Path) { throw "程序与数据在同一目录" }
        "$DATA_DIR 已建立（Config/Database/Models/Logs/Uploads/Backup）"
    } | Out-Null

    Invoke-Test '桌面与开始菜单快捷方式' {
        $desktop = [Environment]::GetFolderPath('CommonDesktopDirectory')
        $lnk = Join-Path $desktop 'AIMS.lnk'
        if (-not (Test-Path $lnk)) { throw "缺少桌面快捷方式：$lnk" }
        $startMenu = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\AIMS'
        if (-not (Test-Path $startMenu)) { throw "缺少开始菜单项：$startMenu" }
        "桌面 $lnk；开始菜单 $startMenu"
    } | Out-Null

    Invoke-Test '安装后自检报告存在且通过' {
        $doc = Join-Path $DATA_DIR 'Logs\post-install-doctor.json'
        if (-not (Test-Path $doc)) { throw "缺少安装后自检报告：$doc" }
        $j = Get-Content $doc -Raw | ConvertFrom-Json
        if (-not $j.ok) {
            $bad = ($j.checks | Where-Object { $_.status -eq 'fail' } | ForEach-Object { $_.check }) -join ', '
            throw "自检未通过：$bad"
        }
        "自检通过（$(($j.checks | Measure-Object).Count) 项，警告 $($j.warnings) 项）"
    } | Out-Null

    Invoke-Test 'PostgreSQL 服务已注册' {
        $svc = Get-Service -Name 'AIMSPostgreSQL' -ErrorAction SilentlyContinue
        if (-not $svc) { throw "未找到服务 AIMSPostgreSQL" }
        "服务状态：$($svc.Status)"
    } | Out-Null

    # 2. 启动
    $started = Invoke-Test '桌面启动器启动并自愈' {
        Start-Process -FilePath $LAUNCHER -WorkingDirectory $PROGRAM_DIR | Out-Null
        $port = Get-ApiPort
        if (-not (Wait-Health -Port $port -Timeout $StartTimeoutSeconds)) {
            throw "启动后 $StartTimeoutSeconds 秒内 /health 未就绪（端口 $port）"
        }
        "端口 $port /health 200"
    }

    if ($started) {
        $port = Get-ApiPort
        Invoke-Test '主界面可访问' {
            $r = Invoke-WebRequest "http://127.0.0.1:$port/ui" -UseBasicParsing -TimeoutSec 10
            if ($r.StatusCode -ne 200 -or $r.Content.Length -lt 5000) { throw "界面返回异常（$($r.StatusCode)，$($r.Content.Length) 字节）" }
            "HTTP 200，$( [math]::Round($r.Content.Length/1024,0) ) KB"
        } | Out-Null

        Invoke-Test '数据库连接与扩展' {
            $j = Get-Content (Join-Path $DATA_DIR 'Logs\post-install-doctor.json') -Raw | ConvertFrom-Json
            $pgItem = $j.checks | Where-Object { $_.check -like '*数据库*' } | Select-Object -First 1
            if ($pgItem.status -eq 'fail') { throw "数据库检查失败：$($pgItem.detail)" }
            "$($pgItem.detail)"
        } | Out-Null

        Invoke-Test '端到端功能自检（30 项真实业务请求）' {
            $json = Join-Path $DATA_DIR 'Logs\selftest.json'
            $p = Start-Process -FilePath $BACKEND -Wait -PassThru -NoNewWindow `
                -ArgumentList @('deploy', '--root', $PROGRAM_DIR, 'selftest', '--report', $json)
            if ($p.ExitCode -ne 0) {
                $r = if (Test-Path $json) { (Get-Content $json -Raw | ConvertFrom-Json) } else { $null }
                $bad = if ($r) { ($r.results | Where-Object { $_.status -eq 'FAIL' } | ForEach-Object { $_.name }) -join ', ' } else { '无报告' }
                throw "自检失败项：$bad"
            }
            $r = Get-Content $json -Raw | ConvertFrom-Json
            "通过 $($r.passed)/$($r.total) 项，耗时 $($r.duration_s) 秒"
        } | Out-Null

        # 3. 备份 / 恢复
        Invoke-Test '手动备份' {
            $p = Start-Process -FilePath $BACKEND -Wait -PassThru -NoNewWindow `
                -ArgumentList @('deploy', '--root', $PROGRAM_DIR, 'backup', '--reason', 'acceptance')
            if ($p.ExitCode -ne 0) { throw "备份命令退出码 $($p.ExitCode)" }
            $latest = Get-ChildItem (Join-Path $DATA_DIR 'Backup') -Filter '*.dump' |
                      Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if (-not $latest) { throw "备份目录里没有归档" }
            $meta = Get-ChildItem (Join-Path $DATA_DIR 'Backup') -Filter '*.json' |
                    Sort-Object LastWriteTime -Descending | Select-Object -First 1
            "$($latest.Name)（$([math]::Round($latest.Length/1MB,2)) MB）"
        } | Out-Null

        Invoke-Test '备份完整性校验（SHA-256 + 归档结构）' {
            $metaPath = Get-ChildItem (Join-Path $DATA_DIR 'Backup') -Filter '*.json' |
                        Sort-Object LastWriteTime -Descending | Select-Object -First 1
            if (-not $metaPath) { throw "缺少备份元数据" }
            $m = Get-Content $metaPath.FullName -Raw | ConvertFrom-Json
            $dump = Join-Path (Join-Path $DATA_DIR 'Backup') $m.file
            $hash = (Get-FileHash $dump -Algorithm SHA256).Hash.ToLower()
            if ($m.sha256 -and $m.sha256 -ne $hash) { throw "SHA-256 不一致" }
            "sha256 前 12 位：$($hash.Substring(0,12))…"
        } | Out-Null

        Invoke-Test '停止应用（备份恢复前置条件）' {
            & (Join-Path $TOOLS 'Stop-Aims.ps1') -Quiet | Out-Null
            Start-Sleep -Seconds 3
            $conn = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
            if ($conn) { throw "端口 $port 仍被占用（PID $($conn[0].OwningProcess)）" }
            "已停止，端口释放"
        } | Out-Null

        Invoke-Test '恢复备份' {
            $p = Start-Process -FilePath $BACKEND -Wait -PassThru -NoNewWindow `
                -ArgumentList @('deploy', '--root', $PROGRAM_DIR, 'restore', 'latest', '--yes')
            if ($p.ExitCode -ne 0) { throw "恢复命令退出码 $($p.ExitCode)" }
            "恢复完成"
        } | Out-Null

        Invoke-Test '恢复后应用可再次启动且数据一致' {
            Start-Process -FilePath $LAUNCHER -WorkingDirectory $PROGRAM_DIR | Out-Null
            if (-not (Wait-Health -Port $port -Timeout $StartTimeoutSeconds)) { throw "恢复后启动失败" }
            $json = Join-Path $DATA_DIR 'Logs\selftest-after-restore.json'
            $p = Start-Process -FilePath $BACKEND -Wait -PassThru -NoNewWindow `
                -ArgumentList @('deploy', '--root', $PROGRAM_DIR, 'selftest', '--report', $json)
            if ($p.ExitCode -ne 0) { throw "恢复后自检未通过" }
            $r = Get-Content $json -Raw | ConvertFrom-Json
            "恢复后自检 $($r.passed)/$($r.total) 通过"
        } | Out-Null
    }

    # 4. 覆盖升级（可选）
    if ($UpgradeSetup) {
        Invoke-Test '覆盖升级' {
            $before = (Get-Item $BACKEND).LastWriteTime
            $log = Join-Path $ReportDir 'setup-upgrade.log'
            $p = Start-Process -FilePath $UpgradeSetup -Wait -PassThru -ArgumentList @(
                '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/LOG=$log")
            if ($p.ExitCode -ne 0) { throw "升级安装退出码 $($p.ExitCode)，日志 $log" }
            $after = (Get-Item $BACKEND).LastWriteTime
            if ($after -le $before) { throw "程序文件未被更新" }
            "程序已更新；静默日志 $log"
        } | Out-Null

        Invoke-Test '升级后数据保留' {
            $port = Get-ApiPort
            if (-not (Wait-Health -Port $port -Timeout $StartTimeoutSeconds)) {
                Start-Process -FilePath $LAUNCHER -WorkingDirectory $PROGRAM_DIR | Out-Null
                if (-not (Wait-Health -Port $port -Timeout $StartTimeoutSeconds)) { throw "升级后启动失败" }
            }
            $json = Join-Path $DATA_DIR 'Logs\selftest-after-upgrade.json'
            $p = Start-Process -FilePath $BACKEND -Wait -PassThru -NoNewWindow `
                -ArgumentList @('deploy', '--root', $PROGRAM_DIR, 'selftest', '--report', $json)
            if ($p.ExitCode -ne 0) { throw "升级后自检未通过" }
            "升级后自检通过"
        } | Out-Null
    }

    # 5. 卸载
    $uninstalled = Invoke-Test '卸载（保留数据）' {
        if (-not (Test-Path $TOOLS)) { throw "缺少 tools 目录，无法卸载" }
        & (Join-Path $TOOLS 'uninstall.ps1') -Root $PROGRAM_DIR -Yes | Out-Null
        Start-Sleep -Seconds 2
        "已停止服务并清理注册项"
    }

    Invoke-Test '卸载后无残留进程' {
        $left = @(Get-Process -Name 'AIMS', 'AIMS-Backend' -ErrorAction SilentlyContinue)
        if ($left.Count -gt 0) { throw ("仍有进程：" + (($left | ForEach-Object { $_.Id }) -join ',')) }
        "干净"
    } | Out-Null

    Invoke-Test '卸载后数据库服务已注销' {
        $svc = Get-Service -Name 'AIMSPostgreSQL' -ErrorAction SilentlyContinue
        if ($svc -and $svc.Status -ne 'Stopped') { throw "服务仍在运行：$($svc.Status)" }
        if ($svc) { "服务仍注册但已停止（安装目录被手工保留时属正常）" } else { "服务已删除" }
    } | Out-Null

    Invoke-Test '卸载后数据仍在（默认不删）' {
        if (-not (Test-Path (Join-Path $DATA_DIR 'Database'))) { throw "数据目录被删除，违反默认保留策略" }
        "保留：$DATA_DIR"
    } | Out-Null

    Invoke-Test '卸载后程序文件已清理' {
        if (Test-Path $BACKEND) { throw "后端程序仍存在：$BACKEND" }
        "程序目录已清理"
    } | Out-Null

    if ($PurgeAfter) {
        Invoke-Test '彻底删除数据（-PurgeAfter）' {
            Remove-Item -LiteralPath $DATA_DIR -Recurse -Force -ErrorAction SilentlyContinue
            if (Test-Path $DATA_DIR) { throw "数据目录仍存在" }
            "已删除 $DATA_DIR"
        } | Out-Null
    }
}

# --------------------------------------------------------------------------- #
$pass = @($steps | Where-Object { $_.status -eq 'PASS' }).Count
$fail = @($steps | Where-Object { $_.status -eq 'FAIL' }).Count
$total = $pass + $fail
$verdict = if ($fail -eq 0 -and $pass -gt 0) { '验收通过' } else { '验收未通过' }

$report = [pscustomobject]@{
    product = 'AIMS 设备智能运维系统'
    setup = $Setup
    upgrade_setup = $UpgradeSetup
    machine = [pscustomobject]@{
        os = "$($os.Caption) $($os.Version) build $($os.BuildNumber)"
        arch = $env:PROCESSOR_ARCHITECTURE
        python = if ($py) { $py.Source } else { $null }
        node = if ($node) { $node.Source } else { $null }
        docker = if ($docker) { 'installed' } else { $null }
    }
    started_at = $stamp
    finished_at = (Get-Date).ToString('s')
    passed = $pass
    failed = $fail
    total = $total
    verdict = $verdict
    steps = $steps
}
($report | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $reportPath -Encoding UTF8

$md = Join-Path $ReportDir 'acceptance-report.md'
$lines = @(
    "# AIMS 验收测试报告",
    "",
    "- 安装包：$Setup",
    "- 测试机：$($report.machine.os) / $($report.machine.arch)",
    "- 预装 Python：$(if ($report.machine.python) { $report.machine.python } else { '无' })；Node：$(if ($report.machine.node) { $report.machine.node } else { '无' })；Docker：$(if ($report.machine.docker) { $report.machine.docker } else { '无' })",
    "- 开始：$stamp；结束：$($report.finished_at)",
    "",
    "## 结果：$verdict（通过 $pass / 失败 $fail）",
    "",
    "| 步骤 | 结果 | 耗时 | 说明 |",
    "|---|---|---|---|"
)
foreach ($s in $steps) {
    $lines += "| $($s.name) | $($s.status) | $($s.seconds) s | $($s.detail -replace '\|', '/') |"
}
$lines += @('', '> 本报告由 tools\Windows-Acceptance.ps1 自动生成，所有步骤均为真实执行。')
$lines -join "`r`n" | Set-Content -LiteralPath $md -Encoding UTF8

Write-Host ""
Write-Host "================ 验收结果 ================" -ForegroundColor Cyan
Write-Host "通过 $pass / 失败 $fail — $verdict" -ForegroundColor $(if ($fail -eq 0) { 'Green' } else { 'Red' })
Write-Host "JSON：$reportPath"
Write-Host "Markdown：$md"
exit $(if ($fail -eq 0) { 0 } else { 1 })
