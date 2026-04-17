#define MyAppName "MTProxy AutoSwitch"
#define MyAppExeName "MTProxyAutoSwitch.exe"
#define MyAppVersion "1.2"
#define MyAppPublisher "pengvench"
#define MyAppURL "https://github.com/pengvench/MTProxyAutoSwitch"
#define MyAppId "MTProxyAutoSwitch"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={localappdata}\Programs\MTProxy AutoSwitch
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=no
AllowNoIcons=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma
SolidCompression=yes
WizardStyle=modern
OutputDir=release-public
OutputBaseFilename=MTProxyAutoSwitch-Setup
UninstallDisplayIcon={app}\{#MyAppExeName}
SetupIconFile=img\icon.ico

[Languages]
Name: "russian"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "startmenuicon"; Description: "Создать ярлык в меню ""Пуск"""; Flags: checkedonce
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; Flags: unchecked

[Files]
Source: "dist\MTProxyAutoSwitch\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; DestName: "README.txt"; Flags: ignoreversion
Source: "config.template.json"; DestDir: "{app}"; DestName: "config.template.json"; Flags: ignoreversion
Source: "list\proxy_list.txt"; DestDir: "{app}\list"; Flags: ignoreversion skipifsourcedoesntexist
Source: "list\report.json"; DestDir: "{app}\list"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: startmenuicon
Name: "{group}\Удалить {#MyAppName}"; Filename: "{uninstallexe}"; Tasks: startmenuicon
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить {#MyAppName}"; Flags: nowait postinstall skipifsilent
