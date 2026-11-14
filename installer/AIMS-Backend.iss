; Inno Setup 脚本：把 PyInstaller 打出来的 dist\AIMS-Backend 文件夹
; 打包成一个标准的 Windows 安装程序（AIMS-Backend-Setup.exe）。
;
; 双击安装包之后，用户会看到熟悉的"下一步、下一步、安装、完成"向导，
; 装完在开始菜单/桌面创建快捷方式，也会自动生成卸载程序
; （在"控制面板 > 卸载程序"里能看到、能正常卸载）。
;
; 这个文件本身不需要手动编译——GitHub Actions 工作流
; （.github/workflows/build-exe.yml）会在云端 Windows 环境里
; 自动用 ISCC.exe 编译它；本地用 build_installer.bat 也一样。
;
; runasoriginaluser：安装结束后以当前登录用户（而不是提权后的管理员）启动程序，
; 否则数据文件会变成管理员所有、窗口也以管理员身份运行。
;
; 注意：本文件含中文，必须保存为 UTF-8 带 BOM，否则 ISCC 会按 ANSI 读出乱码。
; 升版时 MyAppVersion 要和 app/main.py 里的版本号一起改。

#define MyAppName "AIMS Backend"
#define MyAppVersion "1.0.11"
#define MyAppExeName "AIMS-Backend.exe"

[Setup]
AppId={{2B5C694D-8468-45C9-9B97-E586A493AF76}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
; 程序运行时创建同名互斥体（run_standalone.py 的 _hold_app_mutex）：升级或卸载时安装程序据此
; 提示用户先关闭 AIMS，避免"文件正在使用"导致覆盖失败。
AppMutex=AIMS-Backend-Mutex
; 按电脑安装（所有用户可用）：需要管理员权限（会弹 UAC），装到 Program Files。
; Program Files 对普通用户只读，而程序运行时要写数据，所以下面 [Dirs] 只给
; 这几个数据子目录授予"用户可修改"，程序目录本身保持只读（防止被任意替换 exe）。
; 安装目录需要写的地方（已核对代码）：aims_data（数据库/密钥）、data（外部数据下载）、
; cores\models（本地模型）。.env 由管理员维护；崩溃日志会自动回退到 aims_data。
PrivilegesRequired=admin
DefaultDirName={autopf}\AIMS Backend
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 相对路径按本 .iss 所在目录（installer\）解析，回退一级才是仓库根目录的 installer_output\。
OutputDir=..\installer_output
OutputBaseFilename=AIMS-Backend-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
SetupIconFile=..\assets\aims.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Dirs]
; uninsneveruninstall：卸载时保留这些目录里的用户数据。
Name: "{app}\aims_data"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{app}\data"; Permissions: users-modify; Flags: uninsneveruninstall
Name: "{app}\cores\models"; Permissions: users-modify; Flags: uninsneveruninstall

[Files]
; AIMS 是 onedir 打包（整个 dist\AIMS-Backend 文件夹），不是单个 exe，
; 所以整个文件夹都要拷过去（exe + _internal + frontend.html + cores）。
; Source 相对本 .iss 所在目录（installer\）解析，所以要回退一级到仓库根目录。
Source: "..\dist\AIMS-Backend\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "安装完成后立即运行"; Flags: nowait postinstall skipifsilent runasoriginaluser
