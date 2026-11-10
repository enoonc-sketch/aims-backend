<#
.SYNOPSIS
    把构建产物打成可交付的发行包（安装包 + 文档 + 运维脚本 + 校验和 + 清单）。

.DESCRIPTION
    交付物不应该只是"一个 exe"。这个脚本把客户/实施需要的东西放在一起：

        release\AIMS-<版本>\
            AIMS-<版本>-x64-Setup.exe
            docs\                    部署说明、用户手册、故障排查、功能对照、许可证、测试报告
            tools\                   运维脚本（备份/恢复/诊断/卸载…）
            LICENSES.txt
            SHA256SUMS.txt           所有文件的校验和
            release-manifest.json    版本、构建时间、各文件大小与哈希

    同时做一致性检查：安装包里的版本号必须等于 app/deploy/__init__.py 的版本号，
    避免"包是 0.5.8、代码是 0.5.9"这种交付事故。

.PARAMETER Installer
    installer_output 下的安装包路径（x64）。

.PARAMETER Arm64Installer
    可选：ARM64 安装包，一并纳入发行包。

.PARAMETER Version
    默认从 app/deploy/__init__.py 读取，一般不用手动指定。

.EXAMPLE
    .\package_release.ps1 -Installer ..\installer_output\AIMS-0.5.10-x64-Setup.exe
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Installer,
    [string]$Arm64Installer,
    [string]$Version,
    [string]$OutDir
)

. (Join-Path $PSScriptRoot 'build_common.ps1')

Write-Step "打包发行版" 'Green'

if (-not $Version) { $Version = Get-AimsVersion }
if (-not (Test-Path -LiteralPath $Installer)) { throw "安装包不存在：$Installer" }

# 一致性检查：文件名里的版本 vs 代码里的版本
$fileName = [System.IO.Path]::GetFileName($Installer)
if ($fileName -notmatch [regex]::Escape($Version)) {
    throw "版本不一致：代码是 $Version，安装包是 $fileName。请用 build_x64.ps1 重新构建，不要手工改名。"
}

if (-not $OutDir) {
    $OutDir = Join-Path $script:RepoRoot ("release\AIMS-" + $Version)
}
if (Test-Path -LiteralPath $OutDir) { Remove-Item -LiteralPath $OutDir -Recurse -Force }
New-Item -ItemType Directory -Path $OutDir -Force | Out-Null

# ---- 安装包 ----
Copy-Item -LiteralPath $Installer -Destination $OutDir -Force
if ($Arm64Installer) {
    if (-not (Test-Path -LiteralPath $Arm64Installer)) { throw "ARM64 安装包不存在：$Arm64Installer" }
    Copy-Item -LiteralPath $Arm64Installer -Destination $OutDir -Force
}

# ---- 文档与脚本 ----
$docs = Join-Path $OutDir 'docs'
New-Item -ItemType Directory -Path $docs -Force | Out-Null
foreach ($f in @('部署说明.md', '用户手册.md', '故障排查.md', '功能对照表.md',
                 '依赖与许可证清单.md', '测试报告.md')) {
    $src = Join-Path $script:RepoRoot "docs\$f"
    if (Test-Path -LiteralPath $src) { Copy-Item -LiteralPath $src -Destination $docs -Force }
    else { Write-Host "  ! 缺少文档 $f" -ForegroundColor Yellow }
}
$lic = Join-Path $script:RepoRoot 'LICENSES.txt'
if (Test-Path -LiteralPath $lic) { Copy-Item -LiteralPath $lic -Destination $OutDir -Force }

$toolsOut = Join-Path $OutDir 'tools'
New-Item -ItemType Directory -Path $toolsOut -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $script:RepoRoot 'tools') -Filter '*.ps1' -File |
    Copy-Item -Destination $toolsOut -Force

# ---- 校验和与清单 ----
Write-Step "生成校验和"
$files = Get-ChildItem -LiteralPath $OutDir -Recurse -File | Sort-Object FullName
$sums = foreach ($f in $files) {
    $rel = $f.FullName.Substring($OutDir.Length).TrimStart('\', '/')
    $hash = (Get-FileHash -LiteralPath $f.FullName -Algorithm SHA256).Hash.ToLower()
    "$hash  $rel"
}
$sums -join "`r`n" | Set-Content -LiteralPath (Join-Path $OutDir 'SHA256SUMS.txt') -Encoding ASCII

$manifest = [ordered]@{
    product = 'AIMS 设备智能运维系统'
    version = $Version
    built_at = (Get-Date).ToString('s')
    installer = @{
        file = [System.IO.Path]::GetFileName($Installer)
        size_mb = [math]::Round((Get-Item -LiteralPath $Installer).Length / 1MB, 1)
        sha256 = (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLower()
    }
    arm64_installer = if ($Arm64Installer) {
        @{
            file = [System.IO.Path]::GetFileName($Arm64Installer)
            size_mb = [math]::Round((Get-Item -LiteralPath $Arm64Installer).Length / 1MB, 1)
            sha256 = (Get-FileHash -LiteralPath $Arm64Installer -Algorithm SHA256).Hash.ToLower()
        }
    } else { $null }
    docs = @(Get-ChildItem -LiteralPath $docs -File | ForEach-Object { $_.Name })
    tools = @(Get-ChildItem -LiteralPath $toolsOut -File | ForEach-Object { $_.Name })
    total_size_mb = [math]::Round((Get-ChildItem -LiteralPath $OutDir -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
    acceptance_script = 'tools\Windows-Acceptance.ps1（在干净 Windows 上执行）'
}
($manifest | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath (Join-Path $OutDir 'release-manifest.json') -Encoding UTF8

# ---- 可选：整体打 zip，便于传递 ----
$zip = Join-Path (Split-Path -Parent $OutDir) ("AIMS-" + $Version + "-release.zip")
if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip -Force }
Compress-Archive -Path (Join-Path $OutDir '*') -DestinationPath $zip -CompressionLevel Optimal

Write-Host ""
Write-Host "发行包目录：$OutDir" -ForegroundColor Green
Write-Host "压缩包    ：$zip（$([math]::Round((Get-Item $zip).Length/1MB,1)) MB）" -ForegroundColor Green
Write-Host ""
Write-Host "交付前最后三步（缺一不可）：" -ForegroundColor Cyan
Write-Host "  1. 在干净的 Windows 11 x64（断网）上安装本包"
Write-Host "  2. 运行 tools\Windows-Acceptance.ps1 产出验收报告"
Write-Host "  3. 把验收报告放进 docs\测试报告.md 的附录，再对外发布"
exit 0
