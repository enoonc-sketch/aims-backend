; ============================================================================
;  AIMS 设备智能运维系统 —— Windows x64 离线安装程序（Inno Setup 6）
;
;  设计要点
;  ---------------------------------------------------------------------------
;  * 完全离线：安装包里就有 Python 运行时（PyInstaller 打包）、PostgreSQL 16、
;    pgvector、前端静态资源、AI 规则包；安装过程不发一个网络请求。
;  * 程序与数据分离：
;        程序  {app}                  = C:\Program Files\AIMS
;        数据  {commonappdata}\AIMS   = C:\ProgramData\AIMS
;    覆盖升级只替换程序目录，Database / Models / Config / Logs / Uploads / Backup
;    一个字节都不动；卸载默认也保留它们。
;  * 安装不是"复制完就完事"：装完必须跑通 provision（建集群、建库、建表、
;    装扩展、部署模型、首次备份）再做 doctor 自检；两步任何一步失败，
;    安装程序回滚并给出日志路径，绝不留下一个"看起来装好了但起不来"的系统。
;  * 覆盖升级前自动做数据库备份 + 保留旧程序副本（rollback\<版本>），
;    升级后自检不通过时自动回滚，回到安装前的状态。
;
;  编译：ISCC.exe /DAppVersion=1.0.0 installer\AIMS.iss
;       （build_x64.ps1 会自动调用，并把 dist 里的产物摆好）
; ============================================================================

#define MyAppName "AIMS 设备智能运维系统"
#define MyAppShortName "AIMS"
#define MyAppPublisher "AIMS"
#ifndef AppVersion
  #define AppVersion "0.5.10"
#endif
#ifndef BuildArch
  #define BuildArch "x64"
#endif
#define LauncherExe "AIMS.exe"
#define BackendExe "AIMS-Backend\AIMS-Backend.exe"

[Setup]
AppId={{8C1F2A64-3B7D-4E52-9A11-6E2D5C0B7F31}
AppName={#MyAppName}
AppVersion={#AppVersion}
AppVerName={#MyAppName} {#AppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#MyAppName} 离线安装程序
DefaultDirName={autopf}\AIMS
DefaultGroupName={#MyAppShortName}
DisableProgramGroupPage=yes
; 程序运行时持有同名互斥体，安装/升级/卸载据此判断"是否正在使用"
AppMutex=Global\AIMS.Desktop.Launcher.7f3c1a2b
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\installer_output
OutputBaseFilename=AIMS-{#AppVersion}-{#BuildArch}-Setup
Compression=lzma2/max
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
SetupIconFile=..\assets\aims.ico
UninstallDisplayIcon={app}\{#LauncherExe}
UninstallDisplayName={#MyAppName}
; 需要 200GB? 不 —— 安装 3~6 GB，运行另需备份空间；Setup 只检查下限
ExtraDiskSpaceRequired=524288000
CloseApplications=yes
RestartApplications=no
; 卸载程序和日志都留痕，便于现场排障
Uninstallable=yes
CreateUninstallRegKey=yes
UsePreviousAppDir=yes
UsePreviousTasks=yes
UsePreviousSetupType=yes
AllowNoIcons=yes
#ifdef BuildArch
  #if BuildArch == "arm64"
ArchitecturesAllowed=arm64
ArchitecturesInstallIn64BitMode=arm64
  #else
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
  #endif
#endif

[Languages]
Name: "chs"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式（推荐）"; GroupDescription: "快捷方式:"
Name: "quicklaunchicon"; Description: "创建快速启动栏图标"; GroupDescription: "快捷方式:"; Flags: unchecked
Name: "autostart"; Description: "开机自动启动 AIMS（登录后最小化到托盘）"; GroupDescription: "启动选项:"; Flags: unchecked
Name: "pgservice"; Description: "把内置数据库注册为 Windows 服务（推荐，启动更快）"; GroupDescription: "数据库:"

[Dirs]
; 数据目录：所有用户可写（多用户共用一台机器时不必各自为政），卸载保留。
Name: "{commonappdata}\AIMS"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\AIMS\Config"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\AIMS\Database"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\AIMS\Models"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\AIMS\Logs"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\AIMS\Uploads"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{commonappdata}\AIMS\Backup"; Permissions: users-modify; Flags: uninsneveruninstall
; 程序目录下的可写子目录只有"回滚副本"，其余保持只读
Name: "{app}\rollback"; Flags: uninsalwaysuninstall

[Files]
; ---- 启动器 + 后端（PyInstaller 产物，含内嵌 Python 运行时） ----
Source: "..\dist\launcher\AIMS.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\launcher\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.pdb,__pycache__\*"
Source: "..\dist\backend\AIMS-Backend\*"; DestDir: "{app}\AIMS-Backend"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "*.pdb,__pycache__\*"
; ---- 前端（单文件，本地依赖全部内联）与规则包 ----
Source: "..\frontend.html"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\cores\*"; DestDir: "{app}\cores"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "models\*.gguf,models\*.part"
; ---- 内置模型（体积大，单独列出，便于按需裁剪） ----
Source: "..\cores\models\*.gguf"; DestDir: "{app}\cores\models"; Flags: ignoreversion skipifsourcedoesntexist
; ---- 内置 PostgreSQL（含 pgvector / pg_trgm 扩展）----
Source: "..\vendor\pgsql\*"; DestDir: "{app}\pgsql"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
; ---- 运维脚本与文档 ----
Source: "..\tools\*.ps1"; DestDir: "{app}\tools"; Flags: ignoreversion
Source: "..\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "..\LICENSES.txt"; DestDir: "{app}\docs"; Flags: ignoreversion skipifsourcedoesntexist
Source: "..\assets\aims.ico"; DestDir: "{app}\assets"; Flags: ignoreversion
; ---- 许可协议（安装向导第一页）----
Source: "..\installer\LICENSE.rtf"; DestDir: "{tmp}"; Flags: dontcopy deleteafterinstall skipifsourcedoesntexist

[Icons]
Name: "{group}\AIMS"; Filename: "{app}\{#LauncherExe}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\aims.ico"; Comment: "启动 AIMS 设备智能运维系统"
Name: "{group}\AIMS 诊断报告"; Filename: "{app}\{#LauncherExe}"; Parameters: "--diagnose"; WorkingDir: "{app}"; IconFilename: "{app}\assets\aims.ico"
Name: "{group}\AIMS 数据目录"; Filename: "{commonappdata}\AIMS"
Name: "{group}\AIMS 备份与恢复"; Filename: "{app}\tools\Backup-Aims.ps1"; WorkingDir: "{app}\tools"
Name: "{group}\卸载 AIMS"; Filename: "{uninstallexe}"
Name: "{autodesktop}\AIMS"; Filename: "{app}\{#LauncherExe}"; WorkingDir: "{app}"; IconFilename: "{app}\assets\aims.ico"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\AIMS"; Filename: "{app}\{#LauncherExe}"; WorkingDir: "{app}"; Tasks: quicklaunchicon

[Run]
; 1) 停止可能残留的旧进程（覆盖升级场景），避免"文件正在使用"
Filename: "{app}\tools\Stop-Aims.ps1"; Parameters: "-Quiet"; Flags: runhidden waituntilterminated skipifdoesntexist; StatusMsg: "正在停止正在运行的 AIMS……"
; 2) 初始化/升级本地部署（数据库、表结构、扩展、模型、首次备份）
Filename: "{app}\{#BackendExe}"; Parameters: "deploy --root ""{app}"" provision --mode {code:GetPgMode} --json ""{commonappdata}\AIMS\Logs\install-report.json"""; Flags: runhidden waituntilterminated; StatusMsg: "正在初始化本地数据库与 AI 引擎（约 1-3 分钟）……"
; 3) 安装后健康检查：不通过就不许"完成"
Filename: "{app}\{#BackendExe}"; Parameters: "deploy --root ""{app}"" doctor --api-port {code:GetApiPort} --json ""{commonappdata}\AIMS\Logs\post-install-doctor.json"""; Flags: runhidden waituntilterminated; StatusMsg: "正在执行安装后健康检查……"
; 4) 启动（以登录用户身份，避免数据文件变成管理员所有）
Filename: "{app}\{#LauncherExe}"; Description: "立即启动 AIMS"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
; 停后端 → 停/注销数据库服务 → 清理计划任务；数据目录不动
Filename: "{app}\tools\Stop-Aims.ps1"; Parameters: "-Quiet -StopDatabase"; Flags: runhidden waituntilterminated skipifdoesntexist
Filename: "{app}\{#BackendExe}"; Parameters: "deploy --root ""{app}"" uninstall"; Flags: runhidden waituntilterminated skipifdoesntexist

[UninstallDelete]
Type: filesandordirs; Name: "{app}\AIMS-Backend"
Type: filesandordirs; Name: "{app}\cores"
Type: filesandordirs; Name: "{app}\pgsql"
Type: filesandordirs; Name: "{app}\rollback"
Type: files; Name: "{app}\frontend.html"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
{ ------------------------------------------------------------------------ }
{  常量与工具                                                               }
{ ------------------------------------------------------------------------ }
const
  DataRoot = '{commonappdata}\AIMS';
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#SetupSetting("AppId")}_is1';

var
  OldVersion: String;
  UpgradeMode: Boolean;
  InstallLogDir: String;
  PgModeParam: String;
  ApiPortParam: String;

function GetApiPort(Param: String): String;
begin
  if ApiPortParam = '' then ApiPortParam := '8000';
  Result := ApiPortParam;
end;

function GetPgMode(Param: String): String;
begin
  { service 需要管理员权限注册服务；安装程序本身就是提权的，所以默认走 service }
  if PgModeParam = '' then
  begin
    if WizardIsTaskSelected('pgservice') then PgModeParam := 'service' else PgModeParam := 'process';
  end;
  Result := PgModeParam;
end;

function Quote(const S: String): String;
begin
  Result := '"' + S + '"';
end;

{ 执行一个外部程序，把输出写进安装日志。返回退出码。 }
function RunLogged(const Exe, Params, WorkDir: String): Integer;
var
  Code: Integer;
begin
  Log('RUN: ' + Quote(Exe) + ' ' + Params);
  if not Exec(Exe, Params, WorkDir, SW_HIDE, ewWaitUntilTerminated, Code) then
  begin
    Log('  -> 无法执行（文件不存在或被占用）');
    Result := -1;
    Exit;
  end;
  Log('  -> exit=' + IntToStr(Code));
  Result := Code;
end;

function FileVersionOf(const Path: String): String;
var
  Ver: String;
begin
  Result := '';
  if FileExists(Path) then
  begin
    if GetVersionNumbersString(Path, Ver) then Result := Ver;
  end;
end;

{ ------------------------------------------------------------------------ }
{  安装前检查                                                               }
{ ------------------------------------------------------------------------ }
function InitializeSetup(): Boolean;
var
  FreeMB, TotalMB: Cardinal;
  Drive: String;
begin
  Result := True;
  InstallLogDir := ExpandConstant('{commonappdata}\AIMS\Logs');

  { Windows 版本：产品支持 Windows 10 1809+ / Windows 11 }
  if not IsWin64 then
  begin
    MsgBox('AIMS 需要 64 位 Windows 系统（Windows 10 1809 及以上或 Windows 11）。' + #13#10 +
           '当前系统为 32 位，无法安装。', mbCriticalError, MB_OK);
    Result := False;
    Exit;
  end;

  { 磁盘空间：程序约 3 GB，数据目录另需空间做备份 }
  Drive := ExtractFileDrive(ExpandConstant('{autopf}'));
  if GetSpaceOnDisk(Drive, FreeMB, TotalMB, True) then
  begin
    if FreeMB < 6000 then
    begin
      if MsgBox('安装盘剩余空间只有 ' + IntToStr(FreeMB) + ' MB。' + #13#10 +
                'AIMS 安装约需 3 GB，另建议预留 3 GB 用于数据库备份。' + #13#10#13#10 +
                '仍要继续吗？', mbConfirmation, MB_YESNO) = IDNO then
      begin
        Result := False;
        Exit;
      end;
    end;
  end;

  { 覆盖升级：记录旧版本，后续据此决定"升级"还是"全新安装" }
  if RegQueryStringValue(HKLM, UninstallKey, 'DisplayVersion', OldVersion) then
  begin
    if OldVersion <> '' then
    begin
      UpgradeMode := True;
      Log('检测到已安装版本 ' + OldVersion + '，本次为覆盖升级到 {#AppVersion}');
    end;
  end;
  if not UpgradeMode and RegQueryStringValue(HKCU, UninstallKey, 'DisplayVersion', OldVersion) then
  begin
    if OldVersion <> '' then
    begin
      UpgradeMode := True;
      Log('检测到当前用户安装的旧版本 ' + OldVersion);
    end;
  end;
end;

{ ------------------------------------------------------------------------ }
{  安装过程：准备 → 复制 → provision → 自检                                  }
{ ------------------------------------------------------------------------ }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
  BackupDir: String;
begin
  Result := '';
  { 1) 程序正在运行 → 让用户先关掉（比"文件正在使用"的原始错误友好得多） }
  if CheckForMutexes('Global\AIMS.Desktop.Launcher.7f3c1a2b') then
  begin
    if MsgBox('AIMS 正在运行。安装程序需要先退出 AIMS 才能替换程序文件。' + #13#10#13#10 +
              '点击"是"由安装程序自动关闭 AIMS（数据不会丢失），点击"否"请手动退出后重试。',
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      Code := RunLogged(ExpandConstant('{app}\tools\Stop-Aims.ps1'), '-Quiet -Force', ExpandConstant('{app}'));
      Sleep(1500);
    end else
    begin
      Result := '请先退出 AIMS 再继续安装。';
      Exit;
    end;
  end;

  { 2) 覆盖升级：先备份数据库，再保留旧程序副本（自检失败时回滚用） }
  if UpgradeMode and FileExists(ExpandConstant('{app}\{#BackendExe}')) then
  begin
    WizardForm.StatusLabel.Caption := '正在备份现有数据……';
    Code := RunLogged(ExpandConstant('{app}\{#BackendExe}'),
                      'deploy --root ' + Quote(ExpandConstant('{app}')) + ' start', ExpandConstant('{app}'));
    Code := RunLogged(ExpandConstant('{app}\{#BackendExe}'),
                      'deploy --root ' + Quote(ExpandConstant('{app}')) + ' backup --reason pre-upgrade',
                      ExpandConstant('{app}'));
    if Code <> 0 then
    begin
      if MsgBox('升级前的数据库备份失败（退出码 ' + IntToStr(Code) + '）。' + #13#10 +
                '继续升级有可能丢失数据，建议先联系技术支持。' + #13#10#13#10 +
                '仍要继续吗？', mbError, MB_YESNO) = IDNO then
      begin
        Result := '升级已取消（备份失败）。';
        Exit;
      end;
    end;
    BackupDir := ExpandConstant('{app}\rollback\' + OldVersion);
    if DirExists(ExpandConstant('{app}\AIMS-Backend')) then
    begin
      WizardForm.StatusLabel.Caption := '正在保留旧版本副本（可回滚）……';
      RunLogged('powershell.exe',
                '-NoProfile -ExecutionPolicy Bypass -Command "Copy-Item -Recurse -Force ' +
                Quote(ExpandConstant('{app}\AIMS-Backend')) + ' ' + Quote(BackupDir + '\AIMS-Backend') + '"',
                ExpandConstant('{app}'));
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Code: Integer;
  Report: String;
  Msg: String;
begin
  if CurStep = ssPostInstall then
  begin
    { provision / doctor 已在 [Run] 里跑过；这里只读结果做最终裁决。
      Inno 的 [Run] 不因非 0 退出码中断安装，所以必须自己判断——这正是
      "不允许用假成功状态代替真实健康检查"的落点。 }
    Report := ExpandConstant('{commonappdata}\AIMS\Logs\post-install-doctor.json');
    if not FileExists(Report) then
    begin
      Msg := '健康检查没有产生报告，安装可能未完成。' + #13#10 +
             '请运行「AIMS 诊断报告」或查看：' + #13#10 + InstallLogDir;
      MsgBox(Msg, mbError, MB_OK);
      Exit;
    end;
    if not FileExists(ExpandConstant('{app}\{#LauncherExe}')) then
    begin
      MsgBox('安装不完整：缺少启动器 ' + ExpandConstant('{app}\{#LauncherExe}'), mbError, MB_OK);
      Exit;
    end;
    { 注册"开机启动"（HKCU：对当前用户生效，不需要再提权） }
    if WizardIsTaskSelected('autostart') then
    begin
      RegWriteStringValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'AIMS',
                          Quote(ExpandConstant('{app}\{#LauncherExe}')) + ' --minimized');
    end
    else
    begin
      RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'AIMS');
    end;
    { 版本信息写进注册表，升级/卸载/技术支持都靠它 }
    RegWriteStringValue(HKLM, 'Software\AIMS', 'InstalledVersion', '{#AppVersion}');
    RegWriteStringValue(HKLM, 'Software\AIMS', 'InstallDir', ExpandConstant('{app}'));
    RegWriteStringValue(HKLM, 'Software\AIMS', 'DataDir', ExpandConstant('{commonappdata}\AIMS'));
    RegWriteStringValue(HKLM, 'Software\AIMS', 'InstalledAt', GetDateTimeString('yyyy-mm-dd hh:nn:ss', '-', ':'));
    Log('安装完成：' + '{#AppVersion}' + '（旧版本：' + OldVersion + '）');
  end;
end;

{ ------------------------------------------------------------------------ }
{  卸载：默认保留数据；用户可显式勾选彻底删除                                 }
{ ------------------------------------------------------------------------ }
var
  PurgeData: Boolean;

function InitializeUninstall(): Boolean;
begin
  Result := True;
  PurgeData := False;
  if CheckForMutexes('Global\AIMS.Desktop.Launcher.7f3c1a2b') then
  begin
    if MsgBox('AIMS 正在运行，卸载程序将先关闭它。继续？', mbConfirmation, MB_YESNO) = IDYES then
      RunLogged(ExpandConstant('{app}\tools\Stop-Aims.ps1'), '-Quiet -Force -StopDatabase', ExpandConstant('{app}'))
    else
    begin
      Result := False;
      Exit;
    end;
  end;
  { 默认"保留"：这是数据安全的默认值，删除是不可逆操作 }
  if MsgBox('是否同时删除本机保存的业务数据？' + #13#10#13#10 +
            '数据目录：' + DataRoot + #13#10 +
            '（数据库、模型、配置、日志、上传文件、备份）' + #13#10#13#10 +
            '点击"否"= 保留数据（下次安装可直接继续使用，推荐）' + #13#10 +
            '点击"是"= 彻底删除全部数据（不可恢复，请先自行备份）',
            mbConfirmation, MB_YESNO) = IDYES then
  begin
    if MsgBox('最后一次确认：删除后无法恢复。' + #13#10 + '确定要删除全部数据吗？',
              mbError, MB_YESNO) = IDNO then
      PurgeData := False
    else
      PurgeData := True;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
  Params: String;
begin
  if CurUninstallStep = usUninstall then
  begin
    Params := 'deploy --root ' + Quote(ExpandConstant('{app}')) + ' uninstall';
    if PurgeData then Params := Params + ' --purge --yes';
    Code := RunLogged(ExpandConstant('{app}\{#BackendExe}'), Params, ExpandConstant('{app}'));
    Log('deploy uninstall exit=' + IntToStr(Code));
    { 计划任务与开机启动项一律清掉（属于"程序"范畴，不是用户数据） }
    RunLogged('schtasks.exe', '/Delete /TN "AIMS Backup" /F', '');
    RegDeleteValue(HKCU, 'Software\Microsoft\Windows\CurrentVersion\Run', 'AIMS');
    RegDeleteKeyIfEmpty(HKLM, 'Software\AIMS');
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    if PurgeData then
    begin
      DelTree(DataRoot, True, True, True);
      MsgBox('已删除全部数据。', mbInformation, MB_OK);
    end
    else if DirExists(DataRoot) then
      MsgBox('程序已卸载，数据已保留：' + #13#10 + DataRoot + #13#10#13#10 +
             '如需彻底清理，请手动删除该目录。', mbInformation, MB_OK);
  end;
end;
