@echo off
setlocal
pushd "%~dp0"
powershell.exe -NoProfile -Command "$ErrorActionPreference = 'Stop'; try { $app = Get-ChildItem -LiteralPath . -File -Filter '*.exe' | Where-Object { ($_.BaseName -split '-').Length -eq 2 -and $_.BaseName -match '-v\d+\.\d+\.\d+$' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1; if (-not $app) { exit 1 }; Start-Process -FilePath $app.FullName; exit 0 } catch { exit 2 }"
set "launchResult=%ERRORLEVEL%"
popd
if not "%launchResult%"=="0" (
    echo No versioned portable application executable could be launched.
    pause
    exit /b %launchResult%
)
exit /b 0
