<# 
    One-click build script for Group Post Automator.

    Produces (names carry the version so you always know which build you have):
        dist\GroupPostAutomator-v<ver>\GroupPostAutomator-v<ver>.exe  (portable onedir)
        installer\GroupPostAutomatorSetup-v<ver>.exe                   (Inno Setup installer, if ISCC is available)

    -Version <ver>  Sets the version encoded into the artifact/file names and the
                    installer's AppVersion. When omitted, the latest git tag is
                    used (or 0.0.0 outside a repo).

    Requirements (auto-installed into the project venv if missing):
        - Python 3.10+ (venv at project root)
        - pyinstaller >= 6.10
        - pyinstaller-hooks-contrib >= 2024.8
        - pillow (generates the app icon)

    Optional (for the installer):
        - Inno Setup 6 (ISCC.exe). The script installs it silently via winget if
          winget is available; otherwise it prints manual instructions.
#>
param(
    [string]$Seed = "",
    [string]$Version = ""
)

$ErrorActionPreference = "Stop"

$ROOT   = Split-Path -Parent $MyInvocation.MyCommand.Path | Split-Path -Parent
$VENV   = Join-Path $ROOT "venv"
$PYNE   = Join-Path $VENV "Scripts\python.exe"
$PIP    = Join-Path $VENV "Scripts\pip.exe"

# Resolve the interpreter: prefer the project venv, else fall back to a system
# Python (so the one-click script also works on machines without a venv).
if (Test-Path $PYNE) {
    $PYTHON = $PYNE
} else {
    $cand = Get-Command py -ErrorAction SilentlyContinue
    if ($cand) {
        $PYTHON = "py -3.12"   # resolved by the shell's parser below
    } else {
        $PYTHON = "python"
    }
}
# $PYTHON may be a multi-word launcher string; split so the call operator works.
$PYTHON_ARGS = $PYTHON -split ' '
$PYTHON_EXE  = $PYTHON_ARGS[0]
$PYTHON_PRE  = if ($PYTHON_ARGS.Count -gt 1) { $PYTHON_ARGS[1..($PYTHON_ARGS.Count-1)] } else { @() }
$ISCC   = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $ISCC)) {
    # Inno Setup is often installed per-user (winget default) under LOCALAPPDATA.
    $ISCC = Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
}
$MARKER = Join-Path $ROOT "packaging\seed_source.txt"

# ---- Version resolution -------------------------------------------------
# Used in artifact/file names and the installer's AppVersion. Prefer the
# explicit -Version; fall back to the latest git tag; last resort 0.0.0.
if ($Version) {
    $VER = $Version.TrimStart("v")
} else {
    $tag = & git describe --tags --abbrev=0 2>$null
    if ($LASTEXITCODE -eq 0 -and $tag) { $VER = $tag.TrimStart("v") } else { $VER = "0.0.0" }
}
if ($VER -eq "" -or $VER -match '[^0-9a-zA-Z.\-]') {
    Write-Host "Invalid version string: '$Version'" -ForegroundColor Red
    exit 1
}
$APPEXE = "GroupPostAutomator-v$VER.exe"
$INSTALLER_NAME = "GroupPostAutomatorSetup-v$VER.exe"

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Group Post Automator  —  Build  (v$VER)"
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ---- 0. Optional source-workspace seed -----------------------------------------------
# When -Seed <workspace> is given, the built app adopts that workspace's existing
# data/ + profiles/ on first launch (see app/config.py migrate_source_setup()).
# Omit -Seed to build a fresh installer that starts with a one-time login.
if ($Seed) {
    if (-not (Test-Path (Join-Path $Seed "data\bot.db"))) {
        Write-Host "Seed workspace has no data\bot.db — aborting." -ForegroundColor Red
        exit 1
    }
    $SeedResolved = [System.IO.Path]::GetFullPath($Seed).TrimEnd("\")
    # Use UTF8 without BOM so config._load_seed_root() reads a clean path.
    [System.IO.File]::WriteAllText($MARKER, $SeedResolved, (New-Object System.Text.UTF8Encoding $false))
    Write-Host "[Seed] Installed app will import setup from: $SeedResolved" -ForegroundColor Cyan
} elseif (Test-Path $MARKER) {
    Remove-Item $MARKER -Force
    Write-Host "[Seed] Removed old seed marker — builds are now fresh." -ForegroundColor Cyan
}

# ---- 1. Ensure build deps in the venv ------------------------------------------------
Write-Host "[1/4] Installing build dependencies..."
& $PYTHON_EXE @($PYTHON_PRE; "-m"; "pip"; "install"; "-q"; "-r"; (Join-Path $ROOT "packaging\requirements-build.txt"); "--upgrade")

# ---- 2. Generate icon if packaging\app.ico is missing --------------------------------
$ICO = Join-Path $ROOT "packaging\app.ico"
if (-not (Test-Path $ICO)) {
    Write-Host "[2/4] Generating application icon..."
    $ICOScript = @'
from PIL import Image, ImageDraw
S = 256
img = Image.new("RGBA", (S, S), (0,0,0,0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([8,8,S-8,S-8], radius=56, fill=(24,119,242,255))
try:
    font = ImageFont.load_default(90)
except Exception:
    font = None
if font:
    d.text((S/2, S/2), "G", font=font, fill=(255,255,255,255), anchor="mm", stroke_width=4, stroke_fill=(0,0,0,120))
img.save("packaging/app.ico", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
'@
    Set-Content -Path "$env:TEMP\mkicon.ps1.py" -Value $ICOScript -Encoding UTF8
    Push-Location $ROOT
    & $PYTHON_EXE @($PYTHON_PRE; "$env:TEMP\mkicon.ps1.py")
    Pop-Location
} else {
    Write-Host "[2/4] Icon exists."
}

# ---- 3. Run PyInstaller ---------------------------------------------------------------
Write-Host "[3/4] Running PyInstaller..."
& $PYTHON_EXE @($PYTHON_PRE; "-m"; "PyInstaller"; "--noconfirm"; (Join-Path $ROOT "packaging\app.spec"); "--distpath"; (Join-Path $ROOT "dist"); "--workpath"; (Join-Path $ROOT "build"))
if ($LASTEXITCODE -ne 0) {
    Write-Host "PyInstaller failed (exit code $LASTEXITCODE)." -ForegroundColor Red
    exit 1
}
$EXE = Join-Path $ROOT "dist\GroupPostAutomator\GroupPostAutomator.exe"
if (Test-Path $EXE) {
    # Rename the portable build so its folder + launcher carry the version.
    $PORTABLE_DIR = Join-Path $ROOT "dist\GroupPostAutomator-v$VER"
    if (Test-Path $PORTABLE_DIR) { Remove-Item $PORTABLE_DIR -Recurse -Force }
    Rename-Item (Join-Path $ROOT "dist\GroupPostAutomator") "GroupPostAutomator-v$VER"
    Rename-Item (Join-Path $PORTABLE_DIR "GroupPostAutomator.exe") $APPEXE
    $sizeMB = [math]::Round((Get-Item (Join-Path $PORTABLE_DIR $APPEXE)).Length / 1MB, 1)
    Write-Host "Portable build ready: dist\GroupPostAutomator-v$VER\  ($sizeMB MB exe)" -ForegroundColor Green
} else {
    Write-Host "Build output missing — $EXE" -ForegroundColor Red
    exit 1
}

# ---- 4. Build installer (Inno Setup) if available ------------------------------------
Write-Host "[4/4] Building installer..."
if (-not (Test-Path $ISCC)) {
    Write-Host "Inno Setup 6 not found at: $ISCC" -ForegroundColor Yellow
    Write-Host "Attempting silent install via winget..."
    try {
        & winget install -e --id JRSoftware.InnoSetup --accept-package-agreements --accept-source-agreements --silent
        # Refresh the path in this session
        $env:PATH = [System.Environment]::GetEnvironmentVariable("PATH","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("PATH","User")
        if (Test-Path $ISCC) {
            Write-Host "Inno Setup installed successfully." -ForegroundColor Green
        }
    } catch {
        Write-Host "Winget install failed." -ForegroundColor Yellow
    }
}

if (Test-Path $ISCC) {
    & $ISCC (Join-Path $ROOT "packaging\installer.iss") "/DMyVer=$VER" "/DAppExe=$APPEXE"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Inno Setup build failed (exit code $LASTEXITCODE)." -ForegroundColor Yellow
    } else {
        $INSTALLER = Join-Path $ROOT "installer\$INSTALLER_NAME"
        if (Test-Path $INSTALLER) {
            $iMB = [math]::Round((Get-Item $INSTALLER).Length / 1MB, 1)
            Write-Host "Installer ready: installer\$INSTALLER_NAME  ($iMB MB)" -ForegroundColor Green
        }
    }
} else {
    Write-Host ""
    Write-Host "-------------------------------------------------------------"
    Write-Host "Inno Setup not available — installer was NOT built." -ForegroundColor Yellow
    Write-Host "To build the installer manually:"
    Write-Host "  1. Install Inno Setup 6: winget install JRSoftware.InnoSetup"
    Write-Host "  2. Open packaging\installer.iss in the Inno Setup IDE"
    Write-Host "  3. Press Build > Compile"
    Write-Host "  4. The installer appears in: installer\$INSTALLER_NAME"
    Write-Host "-------------------------------------------------------------"
}

Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Build complete!"
Write-Host ""
Write-Host "  Portable (no install):  dist\GroupPostAutomator-v$VER\$APPEXE"
Write-Host "  Installer (if built):   installer\$INSTALLER_NAME"
Write-Host "============================================" -ForegroundColor Cyan
