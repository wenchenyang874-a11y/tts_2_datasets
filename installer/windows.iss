; Version/paths are supplied by build_release.ps1. AppId must stay stable:
; changing it creates a second installation instead of upgrading the first.
#ifndef AppId
  #define AppId "{C8EF7556-B9EA-44AF-96C4-5982E4212033}"
#endif
#ifndef AppVersion
  #error AppVersion must be supplied
#endif

[Setup]
AppId={{#AppId}
AppName=TTS Dataset Studio
AppVersion={#AppVersion}
AppPublisher=TTS Dataset Studio
AppPublisherURL=https://github.com/wenchenyang874-a11y/tts_2_datasets
AppSupportURL=https://github.com/wenchenyang874-a11y/tts_2_datasets/issues
AppUpdatesURL=https://github.com/wenchenyang874-a11y/tts_2_datasets/releases
DefaultDirName={localappdata}\Programs\TTS Dataset Studio
DefaultGroupName=TTS Dataset Studio
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible and not arm64
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
UninstallDisplayIcon={app}\TTS Dataset Studio.exe
SetupIconFile=..\build\app.ico
LicenseFile=..\LICENSE
VersionInfoVersion={#NumericVersion}
VersionInfoProductVersion={#NumericVersion}
VersionInfoProductTextVersion={#AppVersion}
VersionInfoTextVersion={#AppVersion}
OutputDir={#OutputDir}
OutputBaseFilename=TTS-Dataset-Studio-{#AppVersion}-windows-x64-setup
Compression=lzma2
SolidCompression=yes
AppMutex=Local\TTS_Dataset_Studio_Installer_Guard
; Never force close a database/audio writer via Restart Manager. The app's
; named mutex asks the user to close it cleanly before upgrade or uninstall.
CloseApplications=no
RestartApplications=no
Uninstallable=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Only the runtime is installed. No wildcard UninstallDelete is allowed:
; datasets, credentials and files created by users must survive removal.
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\TTS Dataset Studio"; Filename: "{app}\TTS Dataset Studio.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\TTS Dataset Studio"; Filename: "{app}\TTS Dataset Studio.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Uninstall\{{#AppId}_is1"; ValueType: string; ValueName: "TDSNumericVersion"; ValueData: "{#NumericVersion}"; Flags: uninsdeletevalue

[Run]
Filename: "{app}\TTS Dataset Studio.exe"; Description: "{cm:LaunchProgram,TTS Dataset Studio}"; Flags: nowait postinstall skipifsilent

[Code]
function InitializeSetup(): Boolean;
var
  PreviousVersion: String;
  PreviousPacked, CurrentPacked: Int64;
begin
  Result := True;
  // Compare numeric resources, not lexicographic beta strings. A downgrade
  // could open data written by a newer app; require explicit manual removal.
  if RegQueryStringValue(HKCU64, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1', 'TDSNumericVersion', PreviousVersion) then
    if StrToVersion(PreviousVersion, PreviousPacked) and StrToVersion('{#NumericVersion}', CurrentPacked) then
    if ComparePackedVersion(PreviousPacked, CurrentPacked) > 0 then
    begin
      SuppressibleMsgBox('A newer version is installed. Uninstall it first to install this older version. Your datasets will be preserved.', mbError, MB_OK, IDOK);
      Result := False;
    end;
end;
