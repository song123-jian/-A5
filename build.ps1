param([string]$OutputName, [string]$PythonPath, [switch]$SkipTests)

$ErrorActionPreference = "Stop"

$Root = [IO.Path]::GetFullPath($PSScriptRoot)
$Venv = Join-Path $Root ".build-venv"
$VenvPython = Join-Path $Venv "Scripts\python.exe"
$AppBaseName = -join @([char]0x6253, [char]0x5370, [char]0x5FEB, [char]0x6377, [char]0x5DE5, [char]0x5177)
$ExeName = $AppBaseName + ".exe"

function Resolve-PythonCandidate([string]$Candidate) {
    if (-not $Candidate) { return $null }
    $Path = $Candidate
    if (Test-Path -LiteralPath $Path -PathType Container) {
        $Path = Join-Path $Path "python.exe"
    }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return [IO.Path]::GetFullPath($Path)
}

function Test-PythonCandidate([string]$Candidate) {
    $Resolved = Resolve-PythonCandidate $Candidate
    if (-not $Resolved) { return $false }
    & $Resolved -c "import sys, tkinter; sys.exit(0 if (3,12) <= sys.version_info[:2] < (3,15) else 1)" 2>$null | Out-Null
    return [bool]($LASTEXITCODE -eq 0)
}

if ($PythonPath) {
    $ResolvedPython = Resolve-PythonCandidate $PythonPath
    if (-not $ResolvedPython -or -not (Test-PythonCandidate $ResolvedPython)) { throw "PythonPath must be a Python 3.12-3.14 executable or installation directory with Tkinter." }
    $BasePython = $ResolvedPython
} else {
    $Candidates = @()
    $Launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($Launcher) {
        $FromLauncher = & $Launcher.Source -3.12 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0) { $Candidates += $FromLauncher }
    }
    $FromPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($FromPath) { $Candidates += $FromPath.Source }
    $Candidates += Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
    $BasePython = $Candidates | ForEach-Object {
        $Resolved = Resolve-PythonCandidate $_
        if ($Resolved -and (Test-PythonCandidate $Resolved)) { $Resolved }
    } | Select-Object -First 1
    if (-not $BasePython) { throw "Install Python 3.12-3.14 with Tkinter, or pass -PythonPath." }
}

$Version = & $BasePython -X utf8 -c "import runpy; print(runpy.run_path(r'$Root\_version.py')['VERSION'])"
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+$') { throw "Invalid release version in _version.py" }
if (-not $OutputName) { $OutputName = "$AppBaseName-v$Version.exe" }
if ([IO.Path]::GetFileName($OutputName) -ne $OutputName -or [IO.Path]::GetExtension($OutputName) -ne ".exe") {
    throw "OutputName must be an EXE filename without a directory."
}
$OutputExe = Join-Path $Root $OutputName

function Remove-ProjectPath([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $Resolved = [IO.Path]::GetFullPath($Path)
    if (-not $Resolved.StartsWith($Root + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside project: $Resolved"
    }
    Remove-Item -LiteralPath $Resolved -Recurse -Force
}

Remove-ProjectPath $Venv
Remove-ProjectPath (Join-Path $Root "build")
Remove-ProjectPath (Join-Path $Root "dist")
Remove-ProjectPath (Join-Path $Root ($AppBaseName + ".spec"))

Write-Host "Creating isolated build environment..."
& $BasePython -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }

Write-Host "Installing build dependencies..."
& $VenvPython -m pip install --disable-pip-version-check --no-input -r (Join-Path $Root "requirements-build.txt")
if ($LASTEXITCODE -ne 0) { throw "Build dependencies installation failed" }

if (-not $SkipTests) {
    Write-Host "Running tests..."
    Push-Location $Root
    try {
        & $VenvPython -X utf8 -m unittest discover -s tests -v
        if ($LASTEXITCODE -ne 0) { throw "Tests failed" }
    } finally {
        Pop-Location
    }
}

Write-Host "Building single-file Windows application..."
$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name", $AppBaseName,
    "--icon", (Join-Path $Root "app_icon.ico"),
    "--collect-all", "pypdfium2",
    "--collect-all", "tkinterdnd2",
    "--hidden-import", "tkinter.filedialog",
    "--hidden-import", "tkinter.messagebox",
    "--hidden-import", "pystray._win32",
    "app.py"
)
Push-Location $Root
try {
    & $VenvPython -m PyInstaller @PyInstallerArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed" }
} finally {
    Pop-Location
}

Copy-Item -LiteralPath (Join-Path $Root "dist\$ExeName") -Destination $OutputExe -Force
if (-not (Test-Path -LiteralPath $OutputExe)) {
    throw "Output EXE was not created"
}

Remove-ProjectPath $Venv
Remove-ProjectPath (Join-Path $Root "build")
Remove-ProjectPath (Join-Path $Root "dist")
Remove-ProjectPath (Join-Path $Root ($AppBaseName + ".spec"))

Write-Host "Build completed: $OutputExe"
