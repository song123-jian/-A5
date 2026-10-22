param([string]$CompilerPath, [string]$ExecutablePath, [string]$OutputDirectory)

$ErrorActionPreference = 'Stop'
$Root = [IO.Path]::GetFullPath($PSScriptRoot)
$AppBaseName = -join @([char]0x6253, [char]0x5370, [char]0x5FEB, [char]0x6377, [char]0x5DE5, [char]0x5177)
$VersionText = [IO.File]::ReadAllText((Join-Path $Root '_version.py'), [Text.Encoding]::UTF8)
$VersionMatch = [regex]::Match($VersionText, '(?m)^VERSION\s*=\s*"(\d+\.\d+\.\d+)"')
if (-not $VersionMatch.Success) { throw 'Invalid VERSION in _version.py' }
$Version = $VersionMatch.Groups[1].Value

if (-not $ExecutablePath) { $ExecutablePath = Join-Path $Root "$AppBaseName-v$Version.exe" }
if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) { throw "Build the application EXE first: $ExecutablePath" }
$ExecutablePath = (Resolve-Path -LiteralPath $ExecutablePath).Path

if (-not $CompilerPath) {
    $CompilerCommand = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if ($CompilerCommand) { $CompilerPath = $CompilerCommand.Source }
}
if (-not $CompilerPath) {
    $CompilerCandidates = @(
        (Join-Path $Root 'work\toolchain\nsis-portable\nsis-3.11\makensis.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'NSIS\makensis.exe'),
        (Join-Path $env:ProgramFiles 'NSIS\makensis.exe')
    )
    $CompilerPath = $CompilerCandidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
}
if (-not $CompilerPath -or -not (Test-Path -LiteralPath $CompilerPath -PathType Leaf)) {
    throw 'NSIS 3 Unicode compiler is required. Pass -CompilerPath to an official portable or installed makensis.exe.'
}
$CompilerPath = (Resolve-Path -LiteralPath $CompilerPath).Path

if (-not $OutputDirectory) { $OutputDirectory = $Root }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$InstallerSuffix = -join @([char]0x5B89, [char]0x88C5, [char]0x5305)
$OutputFile = Join-Path $OutputDirectory "$AppBaseName-$InstallerSuffix-v$Version.exe"
$IconPath = Join-Path $Root 'app_icon.ico'
$ScriptPath = Join-Path $Root 'installer\setup.nsi'

& $CompilerPath '/INPUTCHARSET' 'UTF8' '/V3' "/DAPP_VERSION=$Version" "/DSOURCE_EXE=$ExecutablePath" "/DAPP_ICON=$IconPath" "/DOUTPUT_FILE=$OutputFile" $ScriptPath
if ($LASTEXITCODE -ne 0) { throw "NSIS build failed with exit code $LASTEXITCODE" }
if (-not (Test-Path -LiteralPath $OutputFile -PathType Leaf)) { throw 'Installer output was not created' }
Get-Item -LiteralPath $OutputFile | Select-Object FullName, Length
Get-FileHash -LiteralPath $OutputFile -Algorithm SHA256 | Select-Object Path, Hash
