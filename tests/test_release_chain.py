"""发布链一致性审计（阶段 8/9）。

安装包、构建脚本和运维脚本之间靠"文件名"耦合：`AIMS.iss` 里写
`{app}\\tools\\backup.ps1`，仓库里就必须真的有 `tools\\backup.ps1`。
这类错误不会被单元测试发现，只有用户装完点快捷方式才会暴露
（曾经就发生过：快捷方式指向不存在的 `Backup-Aims.ps1`）。

本模块用静态解析把这条链上的引用逐个对上，防的是"改了文件名忘了改安装器"。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ISS = ROOT / "installer" / "AIMS.iss"


def _lines(section_name: str) -> list[str]:
    """取出 .iss 里某个 section 的有效行（跳过注释与空行）。"""
    text = ISS.read_text(encoding="utf-8-sig", errors="replace")
    out: list[str] = []
    active = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            active = line[1:-1].lower() == section_name.lower()
            continue
        if not active or not line or line.startswith(";"):
            continue
        out.append(line)
    return out


def _installed_files() -> dict[str, str]:
    """模拟安装后的 {app} 文件表：安装后相对路径(小写) -> 来源说明。

    [Files] 的 `DestDir`/`Source` 决定了"点快捷方式时这个文件在不在"。
    """
    table: dict[str, str] = {}
    for line in _lines("Files"):
        src = re.search(r'Source:\s*"([^"]+)"', line)
        dest = re.search(r'DestDir:\s*"([^"]+)"', line)
        if not src or not dest:
            continue
        source = src.group(1)
        destdir = dest.group(1)
        if not destdir.lower().startswith("{app}"):
            continue
        base = destdir[len("{app}"):].strip("\\/").replace("/", "\\")
        rel_src = source.replace("/", "\\").lstrip("\\")
        # '..\\tools\\*.ps1' -> 仓库内 tools\\*.ps1
        repo_rel = re.sub(r"^(\.\.\\)+", "", rel_src)
        repo_path = ROOT / repo_rel.replace("\\", "/")
        matches = sorted(repo_path.parent.glob(repo_path.name)) if any(
            ch in repo_path.name for ch in "*?"
        ) else ([repo_path] if repo_path.exists() else [])
        if not matches:
            # 构建产物（dist\...）在源码树里本来就不存在，由 build 脚本产出
            if repo_rel.lower().startswith(("dist\\", "vendor\\")):
                table[(base + "\\" + repo_rel.split("\\")[-1]).lower()] = "构建产物"
            continue
        for m in matches:
            if m.is_file():
                name = m.name
                key = (base + "\\" + name) if base else name
                table[key.lower()] = str(m.relative_to(ROOT))
    return table


def test_every_app_path_reference_exists() -> None:
    """[Icons]/[Run]/[UninstallRun] 里引用的 {app}\\... 文件必须真的会被装上。"""
    installed = _installed_files()
    missing: list[str] = []
    checked = 0
    for section in ("Icons", "Run", "UninstallRun"):
        for line in _lines(section):
            for ref in re.findall(r"\{app\}\\([^\"';]+?\.(?:ps1|exe|html|ico|bat|cmd|vbs))", line):
                checked += 1
                key = ref.replace("/", "\\").lower()
                # 常量形式（{#LauncherExe}/{#BackendExe}）已在上面的正则里被 # 排除
                if key not in installed:
                    missing.append(f"[{section}] {ref}")
    assert checked > 0, "没有解析到任何 {app} 文件引用，解析逻辑可能失效"
    assert not missing, "安装器引用了不会被安装的文件（装完后快捷方式/步骤会失败）：" + "；".join(missing)


def test_shortcut_targets_are_not_the_old_backup_name() -> None:
    """回归：快捷方式曾指向并不存在的 Backup-Aims.ps1，真实文件是 backup.ps1。"""
    text = ISS.read_text(encoding="utf-8-sig", errors="replace")
    assert "Backup-Aims.ps1" not in text, "installer 仍引用旧文件名 Backup-Aims.ps1"
    assert (ROOT / "tools" / "backup.ps1").is_file()


def test_tools_scripts_reference_existing_files() -> None:
    """运维脚本之间互相调用时，被调用的 .ps1 必须存在。"""
    missing: list[str] = []
    for ps1 in sorted((ROOT / "tools").glob("*.ps1")):
        text = ps1.read_text(encoding="utf-8-sig", errors="replace")
        for name in re.findall(r"Join-Path\s+\$PSScriptRoot\s+'([^']+\.ps1)'", text):
            if not (ROOT / "tools" / name).is_file():
                missing.append(f"{ps1.name} -> {name}")
    assert not missing, "运维脚本引用了不存在的脚本：" + "；".join(missing)


def test_vendor_hash_baseline_cannot_be_auto_trusted() -> None:
    """下载校验不能"没有基线就自动记录"，否则第一次下载什么就信任什么。"""
    text = (ROOT / "build" / "build_common.ps1").read_text(encoding="utf-8-sig", errors="replace")
    body = text.split("function Assert-FileHash")[1].split("function ")[0]
    assert "if ($Update -or -not $expected)" not in body, "退回到自动记录基线，供应链校验失效"
    assert "缺少校验基线" in body, "缺少'没有基线必须报错'的分支"
    assert "-UpdateLock" in body, "报错信息应引导用户使用 -UpdateLock 建立基线"


def test_release_bundle_docs_exist() -> None:
    """发行包要带的文档必须在 docs/ 里真实存在（阶段 9 交付物）。"""
    text = (ROOT / "build" / "package_release.ps1").read_text(encoding="utf-8-sig", errors="replace")
    block = text.split("foreach ($f in @(")[1].split("))")[0]
    names = re.findall(r"'([^']+\.md)'", block)
    assert names, "package_release.ps1 的文档清单解析失败"
    missing = [n for n in names if not (ROOT / "docs" / n).is_file()]
    assert not missing, "发行包清单里的文档不存在：" + "、".join(missing)


def test_arm64_build_is_honest() -> None:
    """ARM64 必须如实标注仿真模式，不允许把仿真包冒充原生支持。"""
    text = (ROOT / "build" / "build_arm64.ps1").read_text(encoding="utf-8-sig", errors="replace")
    assert "emulation" in text.lower(), "ARM64 构建脚本没有 emulation 模式标注"
    assert "仿真" in text, "ARM64 缺少'数据库以 x64 仿真运行'的说明"


def test_uninstaller_keeps_user_data_by_default() -> None:
    """卸载默认保留数据：iss 的 purge 分支必须由用户显式确认。"""
    text = ISS.read_text(encoding="utf-8-sig", errors="replace")
    assert "PurgeData := False" in text, "卸载默认值被改成删除数据"
    assert "是否同时删除本机保存的业务数据？" in text, "缺少删除数据的二次确认"
    # 数据目录必须标 uninsneveruninstall，卸载程序不允许直接删
    dirs = "\n".join(_lines("Dirs"))
    for d in ("Database", "Backup", "Models", "Logs", "Uploads", "Config"):
        assert re.search(r"\{commonappdata\}\\AIMS\\" + d + r'"[^\n]*uninsneveruninstall', dirs), (
            f"数据子目录 {d} 未标记 uninsneveruninstall，卸载会删用户数据"
        )


def test_version_single_source_of_truth() -> None:
    """版本号唯一来源是 app/deploy/__init__.py，其他文件不得各写一套。"""
    init = (ROOT / "app" / "deploy" / "__init__.py").read_text(encoding="utf-8-sig")
    m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', init)
    assert m, "找不到 APP_VERSION"
    version = m.group(1)
    iss = ISS.read_text(encoding="utf-8-sig", errors="replace")
    assert f'#define AppVersion "{version}"' in iss, f"安装器版本与 APP_VERSION({version}) 不一致"
    assert "Get-AimsVersion" in (ROOT / "build" / "build_common.ps1").read_text(encoding="utf-8-sig"), (
        "构建脚本必须从 APP_VERSION 取版本，而不是硬编码"
    )


@pytest.mark.parametrize("script", ["build_x64.ps1", "build_arm64.ps1", "package_release.ps1"])
def test_build_scripts_have_no_hardcoded_version(script: str) -> None:
    """构建脚本里不该出现写死的 x.y.z 版本号（版本从 APP_VERSION 读）。"""
    text = (ROOT / "build" / script).read_text(encoding="utf-8-sig", errors="replace")
    hard = re.findall(r"AIMS-\d+\.\d+\.\d+", text)
    assert not hard, f"{script} 里硬编码了版本号：{hard}"


def test_scripts_have_utf8_bom_for_windows_hosts() -> None:
    """Windows PowerShell 5.1 与 ISCC 按系统 ANSI 代码页读取无 BOM 文件。

    脚本里全是中文（日志、安装向导文案、错误提示），没有 BOM 就会变成乱码——
    这是装完机才暴露的问题，必须在发布前用测试锁住。
    """
    targets = (
        sorted((ROOT / "tools").glob("*.ps1"))
        + sorted((ROOT / "build").glob("*.ps1"))
        + sorted((ROOT / "installer").glob("*.iss"))
    )
    assert targets, "没有找到需要检查的脚本"
    bad = [str(p.relative_to(ROOT)) for p in targets
           if p.read_bytes()[:3] != b"\xef\xbb\xbf"]
    assert not bad, "以下文件缺少 UTF-8 BOM，Windows 上中文会乱码：" + "、".join(bad)


def test_vbs_wrapper_is_pure_ascii_and_hidden() -> None:
    """VBS 包装器必须纯 ASCII（无 BOM），且以隐藏窗口启动并透传退出码。"""
    vbs = (ROOT / "tools" / "run-hidden.vbs").read_bytes()
    assert vbs[:3] != b"\xef\xbb\xbf", "VBS 带 UTF-8 BOM 会被 WSH 判为非法字符"
    assert all(b < 128 for b in vbs), "VBS 包装器只允许 ASCII，中文交给 PowerShell 脚本输出"
    text = vbs.decode("ascii")
    assert "shell.Run(cmd, 0, True)" in text, "必须用窗口样式 0（完全隐藏）并等待退出码"
    assert "WScript.Quit rc" in text, "必须透传退出码，否则失败会被当成成功"


def test_backup_shortcut_never_flashes_a_console() -> None:
    """「备份与恢复」快捷方式不得直连 powershell.exe（会闪控制台）。"""
    icons = "\n".join(_lines("Icons"))
    backup_lines = [l for l in icons.splitlines() if "备份与恢复" in l]
    assert backup_lines, "找不到备份快捷方式定义"
    line = backup_lines[0]
    assert "run-hidden.vbs" in line, "备份快捷方式必须经 run-hidden.vbs 启动"
    assert "powershell.exe" not in line, "直连 powershell.exe 会闪黑窗"
    assert "-Notify" in line, "隐藏执行必须有结果反馈，否则用户不知道是否备份成功"


def test_notify_popup_only_in_interactive_path() -> None:
    """-Notify 只在快捷方式里传，计划任务/命令行不能弹窗卡住自动化。"""
    tasks = (ROOT / "tools" / "Register-AimsTasks.ps1").read_text(encoding="utf-8-sig", errors="replace")
    assert "-Notify" not in tasks, "计划任务不应弹 Modal 窗口"


# --------------------------------------------------------------------------- #
# 代码签名（阶段 8/9 遗留 P0：签名自动化）
#
# 签名有两个"顺序"陷阱，出错时都不会被功能测试发现：
#   1. 主程序必须在 ISCC 打包之前签，否则安装出来的 EXE 是未签名的；
#   2. 安装包必须在生成 SHA256SUMS 之前签，否则签完哈希就变了，校验和永远对不上。
# 下面用"文本位置比较"把这两个顺序钉死。
# --------------------------------------------------------------------------- #
def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")


def test_signing_helpers_exist_and_verify() -> None:
    common = _read("build/build_common.ps1")
    for fn in ("Resolve-SignTool", "Get-SignConfig", "Sign-AimsFile", "Get-SignatureInfo"):
        assert f"function {fn}" in common, f"build_common.ps1 缺少 {fn}"
    # 签名后必须验签，否则"签名失败"会被当成成功
    assert "'verify', '/pa', '/v'" in common
    assert "Get-AuthenticodeSignature" in common
    # 证书只从环境变量读，不能写死在脚本/仓库里
    assert "AIMS_SIGN_CERT" in common and "AIMS_SIGN_PASSWORD" in common


def test_build_signs_binaries_before_compiling_installer() -> None:
    build = _read("build/build_x64.ps1")
    sign_pos = build.index("Sign-AimsFile -Path $backendExe")
    iscc_pos = build.index("Build-Installer -Version $version -Arch x64")
    assert sign_pos < iscc_pos, "主程序必须在编译安装包之前签名，否则安装包内是未签名 EXE"
    # 安装包本身也要签，且签在冒烟/打包之前算出的哈希之后重新计算
    assert build.count("Sign-AimsFile -Path $setup") == 1
    assert "code_signing" in build


def test_package_signs_before_writing_checksums() -> None:
    pkg = _read("build/package_release.ps1")
    sign_pos = pkg.index("Sign-AimsFile -Path $installerCopy")
    sums_pos = pkg.index("Set-Content -LiteralPath (Join-Path $OutDir 'SHA256SUMS.txt')")
    assert sign_pos < sums_pos, "签名必须早于校验和生成，否则签完哈希失真"
    assert "code_signing" in pkg


def test_unsigned_build_is_loud_not_silent() -> None:
    """未配置证书时允许构建，但必须明确告警，不能静默发出未签名包。"""
    pkg = _read("build/package_release.ps1")
    common = _read("build/build_common.ps1")
    assert "本次发行包未签名" in pkg
    assert "未配置代码签名" in common
    # 强制签名模式：缺配置直接失败
    assert "AIMS_SIGN_REQUIRED" in common
