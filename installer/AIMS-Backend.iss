#define MyAppName "AIMS Backend"
#define MyAppVersion "0.5.4"
#define MyAppExeName "AIMS-Backend.exe"

[Setup]
AppId={{2B5C694D-8468-45C9-9B97-E586A493AF76}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
; The script lives in installer\, but Source / OutputDir relative paths
; resolve against SourceDir (default: the script's own directory). Point it
; at the repo root so dist\AIMS-Backend and installer_output\ land where
; build_installer.bat and the workflow expect them.
SourceDir=..
PrivilegesRequired=lowest
; Install under the user profile so the EXE can write aims_data beside itself.
DefaultDirName={localappdata}\AIMS Backend
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=AIMS-Backend-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
; AIMS uses onedir: include the full build output, runtime files and cores.
Source: "dist\AIMS-Backend\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "安装完成后立即运行"; Flags: nowait postinstall skipifsilent

[Code]
{ WebView2 is needed for the desktop window. Its absence only causes browser fallback. }
function WebView2Available(): Boolean;
begin
  Result := RegKeyExists(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
  if not Result then
    Result := RegKeyExists(HKLM, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
  if not Result then
    { Per-user installs of the Runtime register under HKCU, not HKLM. }
    Result := RegKeyExists(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorCode: Integer;
begin
  if (CurStep = ssPostInstall) and (not WizardSilent()) then
  begin
    if not WebView2Available() then
    begin
      if MsgBox('没有检测到 WebView2 Runtime。' + #13#10#13#10 +
                '程序可以正常安装；没有它时界面会在系统浏览器里打开，' +
                '而不是桌面窗口。' + #13#10 +
                '需要桌面窗口的话，请安装 WebView2 Runtime（免费，微软官方提供），' +
                '装好后重启本程序。' + #13#10#13#10 +
                '现在打开下载页面吗？', mbInformation, MB_YESNO) = IDYES then
        ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/', '', '', SW_SHOWNORMAL, ewNoWait, ErrorCode);
    end;
  end;
end;
