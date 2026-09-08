; Inno Setup script for Group Post Automator.
; Produces a per-user installer that requires no admin rights.
; Build:
;   1. venv\Scripts\pyinstaller packaging\app.spec --noconfirm
;   2. "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss

[Setup]
AppId={{C8D5D0A0-B2C4-4D8B-9F0B-3A2A8C0E4F0C}
AppName=Group Post Automator
AppVersion=0.1.0
AppPublisher=Jovesh
DefaultDirName={localappdata}\Programs\GroupPostAutomator
DefaultGroupName=Group Post Automator
; Output is written to the project-root installer\ folder so the caller
; (packaging/build.ps1) can find GroupPostAutomatorSetup.exe at a stable path.
OutputDir=..\installer
OutputBaseFilename=GroupPostAutomatorSetup
SetupIconFile=app.ico
Compression=lzma2/ultra64
SolidCompression=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DisableProgramGroupPage=yes
DisableDirPage=no
ChangesEnvironment=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &Desktop shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce
Name: "startmenuicon"; Description: "Create a &Start Menu shortcut"; GroupDescription: "Additional icons:"; Flags: checkedonce

[Files]
; Copy the entire onedir build output into the install folder.
Source: "..\dist\GroupPostAutomator\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Group Post Automator"; Filename: "{app}\GroupPostAutomator.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\Group Post Automator"; Filename: "{app}\GroupPostAutomator.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\GroupPostAutomator.exe"; Description: "Launch Group Post Automator now"; Flags: nowait postinstall skipifsilent

[Code]
// ---- Chrome / Edge presence check ------------------------------------------------
function ChromeOrEdgeInstalled: Boolean;
var
  s: String;
begin
  s := '';
  if RegQueryStringValue(HKCU,
     'Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', s) then
    Result := True
  else if RegQueryStringValue(HKLM,
     'Software\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe', '', s) then
    Result := True
  else if FileExists(ExpandConstant('{localappdata}\Google\Chrome\Application\chrome.exe')) then
    Result := True
  else if FileExists(ExpandConstant('{pf32}\Google\Chrome\Application\chrome.exe')) then
    Result := True
  else if FileExists(ExpandConstant('{cf32}\Google\Chrome\Application\chrome.exe')) then
    Result := True
  else
    Result := False;
end;

function InitializeSetup: Boolean;
begin
  if not ChromeOrEdgeInstalled then
  begin
    MsgBox('Google Chrome or Microsoft Edge is required but does not appear to be '
           + 'installed. The app will not work without a supported browser.'
           + ' You can still install, but please install Chrome or Edge afterwards.',
           mbInformation, MB_OK);
  end;
  Result := True;
end;

// ---- Kill running instances before install / uninstall ----------------------------
procedure KillGroupPostAutomator;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{cmd}'), '/c taskkill /F /IM GroupPostAutomator.exe',
       '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

function InitializeUninstall: Boolean;
begin
  KillGroupPostAutomator;
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
    KillGroupPostAutomator;
end;
