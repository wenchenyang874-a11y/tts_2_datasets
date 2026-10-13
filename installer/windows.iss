; Version/paths are supplied by build_release.ps1. AppId must stay stable:
; changing it creates a second installation instead of upgrading the first.
#ifndef AppId
  #define AppId "{C8EF7556-B9EA-44AF-96C4-5982E4212033}"
#endif
#ifndef AppMutexName
  #define AppMutexName "Local\TTS_Dataset_Studio_Installer_Guard"
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
AppMutex={#AppMutexName}
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

[Run]
Filename: "{app}\TTS Dataset Studio.exe"; Description: "{cm:LaunchProgram,TTS Dataset Studio}"; Flags: nowait postinstall skipifsilent

[Code]
procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    // Inno rewrites its uninstall key AFTER processing [Registry]. Writing our
    // version in that section silently loses it, disabling downgrade checks.
    // ssPostInstall runs after registration is complete; uninstall deletes the
    // same owned key without needing a separate persistent registry location.
    if not RegWriteStringValue(HKCU64, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1', 'TDSNumericVersion', '{#NumericVersion}') then
      RaiseException('Unable to save installed version information.');
  end;
end;

function CompareUnsignedVersion(First, Second: Int64): Integer;
begin
  // A packed Windows version uses four UNSIGNED 16-bit components, but Inno
  // 6.7.3 ComparePackedVersion compares signed Int64. A major >= 32768 sets
  // the sign bit and otherwise looks older than 2.x. Preserve the full PE
  // range by comparing sign groups first, then the remaining packed order.
  if (First < 0) and (Second >= 0) then
    Result := 1
  else if (First >= 0) and (Second < 0) then
    Result := -1
  else
    Result := ComparePackedVersion(First, Second);
end;

function InitializeSetup(): Boolean;
var
  PreviousVersion: String;
  PreviousPacked, CurrentPacked: Int64;
begin
  Result := True;
  // Compare numeric resources, not lexicographic beta strings. A downgrade
  // could open data written by a newer app; require explicit manual removal.
  if RegQueryStringValue(HKCU64, 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#AppId}_is1', 'TDSNumericVersion', PreviousVersion) then
  begin
    Log('Previous installed numeric version: ' + PreviousVersion);
    if not StrToVersion(PreviousVersion, PreviousPacked) then
      RaiseException('Invalid installed version information.');
    if not StrToVersion('{#NumericVersion}', CurrentPacked) then
      RaiseException('Invalid installer version information.');
    Log('Version comparison: ' + IntToStr(CompareUnsignedVersion(PreviousPacked, CurrentPacked)));
    if CompareUnsignedVersion(PreviousPacked, CurrentPacked) > 0 then
    begin
      SuppressibleMsgBox('A newer version is installed. Uninstall it first to install this older version. Your datasets will be preserved.', mbError, MB_OK, IDOK);
      Result := False;
    end;
  end;
end;
