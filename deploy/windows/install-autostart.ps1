<#
  ==========================================================================
  WeChat MP Auto Publisher - install the guard into the user's Startup folder
  (Windows only. Linux/macOS do NOT need this - use systemd / launchd.)
  ==========================================================================
  Why a shortcut instead of a generated .bat:
    A .bat file that embeds the project path breaks when the path contains
    non-ASCII characters (Chinese, etc.), because cmd.exe reads .bat bytes with
    the OEM code page. A .lnk shortcut stores the path as UTF-16 and is immune,
    and this script never embeds the path at all - it derives it from its own
    location ($PSScriptRoot), so the file stays pure ASCII.

  Usage (run from a normal PowerShell window):
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 -Interval 60
    powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 -Uninstall

  IMPORTANT - which Python?
    The guard starts the service with the same interpreter that runs it. If the project
    has no .venv\, pin the interpreter that already has the dependencies installed:
      powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 `
                 -Python "C:\path\to\your\venv\Scripts\python.exe"
    (a sibling pythonw.exe is used automatically when present, so no console window pops up)

  What it creates:
    %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\wechat-poster-guard.lnk
    -> <pythonw.exe> "<repo>\scripts\guard.py" --supervise [--interval N]
  ==========================================================================
#>
[CmdletBinding()]
param(
    [int]$Interval = 0,
    [string]$Python = "",
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# Repo root = two levels above this file (deploy\windows\install-autostart.ps1)
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Guard    = Join-Path $RepoRoot "scripts\guard.py"
$Startup  = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Startup"
$LinkPath = Join-Path $Startup "wechat-poster-guard.lnk"

if ($Uninstall) {
    if (Test-Path -LiteralPath $LinkPath) {
        Remove-Item -LiteralPath $LinkPath -Force
        Write-Host "[OK] Removed: $LinkPath"
    } else {
        Write-Host "[--] Nothing to remove (no shortcut found)."
    }
    Write-Host "The service may still be running. Stop it in Task Manager (pythonw / python)."
    exit 0
}

if (-not (Test-Path -LiteralPath $Guard)) {
    Write-Host "[ERROR] Can't find scripts\guard.py under $RepoRoot"
    exit 1
}
if (-not (Test-Path -LiteralPath $Startup)) {
    Write-Host "[ERROR] Startup folder not found: $Startup"
    exit 1
}

# Pick the interpreter the guard will use (and therefore the one that runs the service).
$PythonW = $null
if ($Python) {
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Host "[ERROR] -Python path not found: $Python"
        exit 1
    }
    $PythonW = $Python
    if ([IO.Path]::GetFileName($Python) -ieq "python.exe") {
        $sibling = Join-Path (Split-Path $Python -Parent) "pythonw.exe"
        if (Test-Path -LiteralPath $sibling) { $PythonW = $sibling }   # windowless interpreter
    }
} else {
    # Prefer the project's virtualenv, then any pythonw on PATH.
    $Candidates = @(
        (Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"),
        (Join-Path $RepoRoot "venv\Scripts\pythonw.exe")
    )
    foreach ($c in $Candidates) { if (Test-Path -LiteralPath $c) { $PythonW = $c; break } }
    if (-not $PythonW) {
        $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if ($cmd) { $PythonW = $cmd.Source }
    }
}
if (-not $PythonW) {
    Write-Host "[ERROR] No interpreter found. The project has no .venv\, so please pin one:"
    Write-Host "        -Python `"<path to the python.exe that has the dependencies>`""
    exit 1
}
# Dependency sanity check: the interpreter must be able to import uvicorn/fastapi,
# otherwise the guard would spin without ever managing to start the service.
$pyExe = $PythonW -replace "pythonw\.exe$", "python.exe"
& $pyExe -c "import uvicorn, fastapi" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "[WARN] This interpreter failed to 'import uvicorn, fastapi':"
    Write-Host "       $pyExe"
    Write-Host "       The guard would keep failing to start the service. Install deps first, or pass -Python."
}

$ArgsLine = '"' + $Guard + '" --supervise'
if ($Interval -gt 0) {
    $secs = [Math]::Max(30, $Interval)
    $ArgsLine = $ArgsLine + " --interval $secs"
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($LinkPath)
$lnk.TargetPath       = $PythonW
$lnk.Arguments        = $ArgsLine
$lnk.WorkingDirectory = $RepoRoot
$lnk.Description      = "WeChat MP Auto Publisher - guard (keeps the service on :8765 alive)"
$lnk.WindowStyle      = 7     # minimized: pythonw shows no console anyway
$lnk.Save()

Write-Host "[OK] Installed autostart shortcut:"
Write-Host "     $LinkPath"
Write-Host "     target : $PythonW"
Write-Host "     args   : $ArgsLine"
Write-Host ""
Write-Host "It starts at next sign-in. To start it right now:"
Write-Host "     & '$PythonW' '$Guard' --supervise"
Write-Host ""
Write-Host "Verify:  python `"$Guard`" --dry-run"
Write-Host "Undo  :  powershell -ExecutionPolicy Bypass -File deploy\windows\install-autostart.ps1 -Uninstall"
