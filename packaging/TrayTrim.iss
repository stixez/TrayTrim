; Windows installer for TrayTrim (Inno Setup 6). Built by packaging/build.py, which passes the
; version: ISCC /DAppVersion=0.1.0 packaging\TrayTrim.iss

#define AppName "TrayTrim"
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{9B1C2E4A-7F3D-4C55-9A61-3E2B8D7C1F20}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=TrayTrim contributors
AppPublisherURL=https://github.com/stixez/TrayTrim
AppSupportURL=https://github.com/stixez/TrayTrim/issues
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
DisableDirPage=auto
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=TrayTrim-{#AppVersion}-setup
SetupIconFile=assets\TrayTrim.ico
UninstallDisplayIcon={app}\TrayTrim.exe
UninstallDisplayName={#AppName}
WizardStyle=modern
WizardSizePercent=100
WizardImageFile=assets\wizard-100.bmp,assets\wizard-150.bmp,assets\wizard-200.bmp
WizardSmallImageFile=assets\wizard-small-100.bmp,assets\wizard-small-150.bmp,assets\wizard-small-200.bmp
WizardImageStretch=no
WizardImageBackColor=$171717
LicenseFile=..\LICENSE
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Install TrayTrim
WelcomeLabel2=TrayTrim exports a Sims 4 household with only the custom content its Sims use, as one small package, and checks the result.%n%nIt only reads your Sims 4 folders; it never changes your Mods or Tray.
FinishedHeadingLabel=TrayTrim is installed
FinishedLabelNoIcons=TrayTrim is installed on your computer.
FinishedLabel=TrayTrim is installed on your computer. You can open it from the Start menu.

[Tasks]
Name: "desktopicon"; Description: "Add a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
Source: "..\dist\TrayTrim\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; The licenses also next to TrayTrim.exe, where people look for them (the build has them in _internal).
Source: "..\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\LICENSES\*"; DestDir: "{app}\LICENSES"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\TrayTrim.exe"; AppUserModelID: "TrayTrim.TrayTrim"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\TrayTrim.exe"; Tasks: desktopicon; AppUserModelID: "TrayTrim.TrayTrim"

[Run]
Filename: "{app}\TrayTrim.exe"; Description: "Open TrayTrim now"; Flags: nowait postinstall skipifsilent
