#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif
#ifndef NumericVersion
  #define NumericVersion "0.0.0.0"
#endif
#ifndef SourceExe
  #define SourceExe "..\..\dist\OpenAI-Free-Credit-Tracker.exe"
#endif
#ifndef OutputDirectory
  #define OutputDirectory "..\..\dist"
#endif

[Setup]
AppId={{9C47963B-F3AD-49A1-B2F2-4D427980A018}
AppName=OpenAI Free Credit Tracker
AppVersion={#AppVersion}
AppPublisher=kejamestw
AppPublisherURL=https://github.com/kejamestw/OpenAI-Free-Credit-Tracker
AppSupportURL=https://github.com/kejamestw/OpenAI-Free-Credit-Tracker/issues
DefaultDirName={localappdata}\Programs\OpenAI Free Credit Tracker
DefaultGroupName=OpenAI Free Credit Tracker
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir={#OutputDirectory}
OutputBaseFilename=OpenAI-Free-Credit-Tracker-{#AppVersion}-windows-x86_64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\OpenAI-Free-Credit-Tracker.exe
VersionInfoVersion={#NumericVersion}
VersionInfoCompany=kejamestw
VersionInfoDescription=OpenAI Free Credit Tracker per-user installer
VersionInfoProductName=OpenAI Free Credit Tracker
VersionInfoProductVersion={#NumericVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoTextVersion={#AppVersion}

[Files]
Source: "{#SourceExe}"; DestDir: "{app}"; DestName: "OpenAI-Free-Credit-Tracker.exe"; Flags: ignoreversion

[Icons]
Name: "{group}\OpenAI Free Credit Tracker"; Filename: "{app}\OpenAI-Free-Credit-Tracker.exe"
Name: "{userdesktop}\OpenAI Free Credit Tracker"; Filename: "{app}\OpenAI-Free-Credit-Tracker.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\OpenAI-Free-Credit-Tracker.exe"; Description: "Launch OpenAI Free Credit Tracker"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Deliberately empty. Configuration, history, exports, and OS credentials are user
; data and survive uninstall. The application provides separately confirmed cleanup.
