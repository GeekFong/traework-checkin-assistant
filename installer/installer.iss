; ─────────────────────────────────────────────────────────────
; TraeWork签到助手 Inno Setup 安装脚本
; 编译：ISCC.exe installer\installer.iss  （需 Inno Setup 6+）
; 产物：installer\Output\TraeWorkCheckin-Setup-v1.1.0.exe
;
; 设计原则：
;   · 仅安装程序本体到 {autopf}，开始菜单/桌面可选快捷方式
;   · 用户数据（账号/设置/历史/日志）位于 exe 旁或
;     %LOCALAPPDATA%\TraeCheckinApp，安装与卸载均不触碰
;   · 绿色单文件 exe 与安装版共用同一份用户数据
; ─────────────────────────────────────────────────────────────

#define MyAppName "TraeWork签到助手"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "个人开源"
#define MyAppExeName "TraeWorkCheckin.exe"
#define RepoRoot ".."

[Setup]
AppId={{B7F3A2E1-6C4D-4E8B-9F21-CA0211000000}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=TraeWork CN / WorkBuddy 每日签到助手（个人学习用途免费工具）
DefaultDirName={autopf}\TraeCheckinApp
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=TraeWorkCheckin-Setup-v{#MyAppVersion}
SetupIconFile={#RepoRoot}\assets\app.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; 单实例：与程序自身的互斥锁同名，安装/卸载时若程序在运行则提示关闭
AppMutex=Local\TraeWorkCheckinApp_SingleInstance_v1

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加选项："
Name: "startupicon"; Description: "开机自动启动（也可在程序内设置自动签到任务）"; GroupDescription: "附加选项："

[Files]
Source: "{#RepoRoot}\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#RepoRoot}\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startupicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('卸载完成。' #13#10 #13#10 +
      '你的账号、设置、签到历史等数据保留在：' #13#10 +
      ExpandConstant('{localappdata}\TraeCheckinApp') + ' 或程序原安装目录' #13#10#13#10 +
      '如需彻底删除，请手动删除对应数据文件（可在程序「关于」里打开数据目录）。',
      mbInformation, MB_OK);
end;
